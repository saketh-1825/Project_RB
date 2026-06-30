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
	"sre-copilot/ws"
)

// WebhookHandler handles /webhooks/* endpoints.
type WebhookHandler struct {
	alertStore db.AlertStore
	langGraph  *clients.LangGraphClient
	retryQueue *clients.RetryQueue
	hub        *ws.Hub
}

func NewWebhookHandler(
	alertStore db.AlertStore,
	langGraph *clients.LangGraphClient,
	retryQueue *clients.RetryQueue,
	hub *ws.Hub,
) *WebhookHandler {
	return &WebhookHandler{
		alertStore: alertStore,
		langGraph:  langGraph,
		retryQueue: retryQueue,
		hub:        hub,
	}
}

// triggerAnalysis calls LangGraph and broadcasts WS events. Falls back to
// the retry queue when LangGraph is unreachable.
func (h *WebhookHandler) triggerAnalysis(ctx context.Context, a *models.Alert) {
	req := clients.TriggerAnalysisRequest{
		AlertID:     a.AlertID,
		Alert:       *a,
		TriggeredAt: time.Now(),
	}

	resp, err := h.langGraph.TriggerAnalysis(ctx, req)
	if err != nil {
		log.Error().Err(err).Str("alert_id", a.AlertID).Msg("LangGraph unreachable, enqueueing for retry")
		if h.retryQueue != nil {
			if qErr := h.retryQueue.Enqueue(ctx, req, 0); qErr != nil {
				log.Error().Err(qErr).Msg("retryQueue.Enqueue failed")
			}
		}
		return
	}

	log.Info().Str("alert_id", a.AlertID).Str("analysis_id", resp.AnalysisID).Msg("LangGraph analysis triggered")

	if h.hub != nil {
		h.hub.BroadcastEvent("analysis.started", gin.H{
			"alert_id":    a.AlertID,
			"analysis_id": resp.AnalysisID,
		})
	}
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
		firedAt, _ := time.Parse(time.RFC3339, promAlert.StartsAt)
		if firedAt.IsZero() {
			firedAt = time.Now()
		}

		name := "unknown"
		if v, ok := promAlert.Labels["alertname"]; ok {
			name = v.(string)
		}

		severity := models.SeverityMedium
		if v, ok := promAlert.Labels["severity"]; ok {
			severity = models.Severity(v.(string))
		}

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

		// Broadcast alert.fired to all WS subscribers
		if h.hub != nil {
			h.hub.BroadcastEvent("alert.fired", alert)
		}

		// Trigger LangGraph analysis for firing alerts
		if promAlert.Status == "firing" {
			go h.triggerAnalysis(context.Background(), alert)
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

	// Broadcast alert.fired
	if h.hub != nil {
		h.hub.BroadcastEvent("alert.fired", alert)
	}

	if status == "firing" {
		go h.triggerAnalysis(context.Background(), alert)
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

	// Broadcast alert.fired
	if h.hub != nil {
		h.hub.BroadcastEvent("alert.fired", &alert)
	}

	if alert.Status == "firing" {
		go h.triggerAnalysis(context.Background(), &alert)
	}

	c.JSON(http.StatusOK, gin.H{
		"received": true,
		"alert_id": alert.AlertID,
	})
}
