package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"sre-copilot/clients"
	"sre-copilot/db"
	"sre-copilot/models"
)

// WebhookHandler handles /webhooks/* endpoints.
type WebhookHandler struct {
	alertStore db.AlertStore
	langGraph  *clients.LangGraphClient
}

func NewWebhookHandler(alertStore db.AlertStore, langGraph *clients.LangGraphClient) *WebhookHandler {
	return &WebhookHandler{alertStore: alertStore, langGraph: langGraph}
}

// ─── Prometheus Alertmanager ─────────────────────────────────────────────────

// PrometheusPayload matches the Alertmanager webhook format.
type PrometheusPayload struct {
	Version  string `json:"version"`
	GroupKey string `json:"groupKey"`
	Status   string `json:"status"`
	Alerts   []struct {
		Status       string                 `json:"status"`
		Labels       map[string]interface{} `json:"labels"`
		Annotations  map[string]interface{} `json:"annotations"`
		StartsAt     string                 `json:"startsAt"`
		EndsAt       string                 `json:"endsAt"`
		GeneratorURL string                 `json:"generatorURL"`
	} `json:"alerts"`
}

// Prometheus handles POST /webhooks/prometheus.
func (h *WebhookHandler) Prometheus(c *gin.Context) {
	var payload PrometheusPayload
	if err := c.ShouldBindJSON(&payload); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	processed := 0
	for _, promAlert := range payload.Alerts {
		// Convert Prometheus alert to internal Alert format
		firedAt, _ := time.Parse(time.RFC3339, promAlert.StartsAt)
		if firedAt.IsZero() {
			firedAt = time.Now()
		}

		// Extract alert name from labels
		name := "unknown"
		if v, ok := promAlert.Labels["alertname"]; ok {
			name = v.(string)
		}

		// Map severity from labels or default to "medium"
		severity := models.SeverityMedium
		if v, ok := promAlert.Labels["severity"]; ok {
			severity = models.Severity(v.(string))
		}

		// Extract affected services
		var services []string
		if v, ok := promAlert.Labels["service"]; ok {
			services = []string{v.(string)}
		}

		genURL := promAlert.GeneratorURL
		alert := &models.Alert{
			Source:           "prometheus",
			Name:             name,
			Severity:         severity,
			Status:           promAlert.Status,
			FiredAt:          firedAt,
			Labels:           promAlert.Labels,
			Annotations:      promAlert.Annotations,
			AffectedServices: services,
			GeneratorURL:     &genURL,
		}

		if err := h.alertStore.Save(c.Request.Context(), alert); err != nil {
			log.Error().Err(err).Str("name", name).Msg("failed to save prometheus alert")
			continue
		}
		processed++

		// Trigger LangGraph analysis for firing alerts
		if promAlert.Status == "firing" {
			go func(a *models.Alert) {
				_, err := h.langGraph.TriggerAnalysis(context.Background(), clients.TriggerAnalysisRequest{
					AlertID:     a.AlertID,
					Alert:       *a,
					TriggeredAt: time.Now(),
				})
				if err != nil {
					log.Error().Err(err).Str("alert_id", a.AlertID).Msg("failed to trigger LangGraph analysis")
				} else {
					log.Info().Str("alert_id", a.AlertID).Msg("LangGraph analysis triggered")
				}
			}(alert)
		}
	}

	c.JSON(http.StatusOK, gin.H{
		"received":         true,
		"alerts_processed": processed,
	})
}

// ─── Datadog ─────────────────────────────────────────────────────────────────

// DatadogPayload matches Datadog's webhook format.
type DatadogPayload struct {
	ID           string `json:"id"`
	Title        string `json:"title"`
	Text         string `json:"text"`
	Priority     string `json:"priority"`
	Tags         string `json:"tags"`
	AlertMetric  string `json:"alert_metric"`
	AlertStatus  string `json:"alert_status"`
	AlertType    string `json:"alert_type"`
	DateHappened int64  `json:"date_happened"`
}

// Datadog handles POST /webhooks/datadog.
func (h *WebhookHandler) Datadog(c *gin.Context) {
	var payload DatadogPayload
	if err := c.ShouldBindJSON(&payload); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	// Map Datadog priority to internal severity
	severityMap := map[string]models.Severity{
		"P1": models.SeverityCritical,
		"P2": models.SeverityHigh,
		"P3": models.SeverityMedium,
		"P4": models.SeverityLow,
	}
	severity := severityMap[payload.Priority]
	if severity == "" {
		severity = models.SeverityMedium
	}

	status := "firing"
	if payload.AlertStatus == "Recovered" || payload.AlertStatus == "OK" {
		status = "resolved"
	}

	firedAt := time.Now()
	if payload.DateHappened > 0 {
		firedAt = time.Unix(payload.DateHappened, 0)
	}

	alert := &models.Alert{
		Source:   "datadog",
		Name:     payload.Title,
		Severity: severity,
		Status:   status,
		FiredAt:  firedAt,
		Labels: map[string]interface{}{
			"datadog_id":   payload.ID,
			"alert_metric": payload.AlertMetric,
			"tags":         payload.Tags,
		},
		Annotations: map[string]interface{}{
			"text": payload.Text,
		},
		AffectedServices: []string{},
	}

	if err := h.alertStore.Save(c.Request.Context(), alert); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Trigger analysis for firing alerts
	if status == "firing" {
		go func() {
			_, err := h.langGraph.TriggerAnalysis(context.Background(), clients.TriggerAnalysisRequest{
				AlertID:     alert.AlertID,
				Alert:       *alert,
				TriggeredAt: time.Now(),
			})
			if err != nil {
				log.Error().Err(err).Str("alert_id", alert.AlertID).Msg("failed to trigger LangGraph analysis")
			}
		}()
	}

	c.JSON(http.StatusOK, gin.H{"received": true})
}

// ─── Custom Webhook ──────────────────────────────────────────────────────────

// Custom handles POST /webhooks/custom — generic webhook using internal Alert schema.
func (h *WebhookHandler) Custom(c *gin.Context) {
	var alert models.Alert
	if err := c.ShouldBindJSON(&alert); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	alert.Source = "custom_webhook"
	if alert.Status == "" {
		alert.Status = "firing"
	}
	if alert.FiredAt.IsZero() {
		alert.FiredAt = time.Now()
	}
	if alert.Labels == nil {
		alert.Labels = map[string]interface{}{}
	}
	if alert.Annotations == nil {
		alert.Annotations = map[string]interface{}{}
	}
	if alert.AffectedServices == nil {
		alert.AffectedServices = []string{}
	}

	if err := h.alertStore.Save(c.Request.Context(), &alert); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Trigger LangGraph analysis for firing alerts
	if alert.Status == "firing" {
		go func(a *models.Alert) {
			_, err := h.langGraph.TriggerAnalysis(context.Background(), clients.TriggerAnalysisRequest{
				AlertID:     a.AlertID,
				Alert:       *a,
				TriggeredAt: time.Now(),
			})
			if err != nil {
				log.Error().Err(err).Str("alert_id", a.AlertID).Msg("failed to trigger LangGraph analysis")
			} else {
				log.Info().Str("alert_id", a.AlertID).Msg("LangGraph analysis triggered")
			}
		}(&alert)
	}

	c.JSON(http.StatusOK, gin.H{
		"received": true,
		"alert_id": alert.AlertID,
	})
}
