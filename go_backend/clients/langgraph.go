// Package clients provides HTTP clients for calling external services.
package clients

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"sre-copilot/models"
)

// LangGraphClient wraps HTTP calls to the LangGraph analysis service.
type LangGraphClient struct {
	BaseURL    string
	Token      string
	HTTPClient *http.Client
}

// NewLangGraphClient creates a new client for the LangGraph service.
func NewLangGraphClient(baseURL, token string) *LangGraphClient {
	return &LangGraphClient{
		BaseURL: baseURL,
		Token:   token,
		HTTPClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// TriggerAnalysisRequest is the payload for POST /analyses.
type TriggerAnalysisRequest struct {
	AlertID     string        `json:"alert_id"`
	Alert       models.Alert  `json:"alert"`
	TriggeredAt time.Time     `json:"triggered_at"`
	Context     *AnalysisContext `json:"context,omitempty"`
}

// AnalysisContext provides extra context at trigger time.
type AnalysisContext struct {
	RecentDeployments []DeploymentInfo `json:"recent_deployments,omitempty"`
	OngoingIncidents  []string         `json:"ongoing_incidents,omitempty"`
}

// DeploymentInfo describes a recent deployment.
type DeploymentInfo struct {
	Service    string    `json:"service"`
	Version    string    `json:"version"`
	DeployedAt time.Time `json:"deployed_at"`
}

// TriggerAnalysisResponse is the response from POST /analyses.
type TriggerAnalysisResponse struct {
	AnalysisID string `json:"analysis_id"`
	Status     string `json:"status"`
	Message    string `json:"message"`
}

// TriggerAnalysis calls POST /api/v1/analyses on the LangGraph service.
func (c *LangGraphClient) TriggerAnalysis(ctx context.Context, req TriggerAnalysisRequest) (*TriggerAnalysisResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("langgraph: marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/api/v1/analyses", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("langgraph: create request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.Token)

	resp, err := c.HTTPClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("langgraph: request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusAccepted && resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("langgraph: unexpected status %d", resp.StatusCode)
	}

	var result TriggerAnalysisResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("langgraph: decode response: %w", err)
	}
	return &result, nil
}

// HealthResponse is the response from GET /api/v1/health.
type HealthResponse struct {
	Status         string `json:"status"`
	ActiveAnalyses int    `json:"active_analyses"`
	QueueDepth     int    `json:"queue_depth"`
}

// CheckHealth calls GET /api/v1/health on the LangGraph service.
func (c *LangGraphClient) CheckHealth(ctx context.Context) (*HealthResponse, error) {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/api/v1/health", nil)
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Authorization", "Bearer "+c.Token)

	resp, err := c.HTTPClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("langgraph health: %w", err)
	}
	defer resp.Body.Close()

	var result HealthResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	return &result, nil
}

// SendInterrupt forwards a human decision to LangGraph POST /api/v1/analyses/:id/interrupt.
// Matches the contract payload: interrupt_type, payload (object), provided_by.
func (c *LangGraphClient) SendInterrupt(ctx context.Context, analysisID, interruptType string, responsePayload map[string]interface{}, providedBy string) error {
	if responsePayload == nil {
		responsePayload = map[string]interface{}{}
	}
	body, _ := json.Marshal(map[string]interface{}{
		"interrupt_type":  interruptType,
		"payload":         responsePayload,
		"provided_by":     providedBy,
	})

	url := fmt.Sprintf("%s/api/v1/analyses/%s/interrupt", c.BaseURL, analysisID)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("langgraph: SendInterrupt build request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+c.Token)

	resp, err := c.HTTPClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("langgraph: SendInterrupt request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("langgraph: SendInterrupt status %d for analysis %s", resp.StatusCode, analysisID)
	}
	return nil
}

