package handlers

import (
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
	"sre-copilot/ws"
)

// validAgents is the set of accepted agent names from LangGraph.
var validAgents = map[string]bool{
	"supervisor":          true,
	"log_query_agent":     true,
	"rag_agent":           true,
	"correlation_agent":   true,
	"report_agent":        true,
}

// validSeverities is the set of accepted finding severity values.
var validSeverities = map[string]bool{
	"critical": true,
	"high":     true,
	"medium":   true,
	"low":      true,
	"info":     true,
}

// IncidentHandler handles /api/v1/incidents endpoints.
type IncidentHandler struct {
	store db.IncidentStore
	hub   *ws.Hub
}

func NewIncidentHandler(store db.IncidentStore, hub *ws.Hub) *IncidentHandler {
	return &IncidentHandler{store: store, hub: hub}
}

// List handles GET /incidents — list historical incidents.
func (h *IncidentHandler) List(c *gin.Context) {
	f := db.IncidentFilter{
		Service:  c.Query("service"),
		Severity: c.Query("severity"),
		Status:   c.Query("status"),
	}
	if v := c.Query("from"); v != "" {
		t, _ := time.Parse(time.RFC3339, v)
		f.From = &t
	}
	if v := c.Query("to"); v != "" {
		t, _ := time.Parse(time.RFC3339, v)
		f.To = &t
	}
	f.Page, _ = strconv.Atoi(c.DefaultQuery("page", "1"))
	f.PageSize, _ = strconv.Atoi(c.DefaultQuery("page_size", "20"))

	incidents, total, err := h.store.List(c.Request.Context(), f)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if incidents == nil {
		incidents = []models.IncidentSummary{}
	}

	c.JSON(http.StatusOK, gin.H{
		"incidents": incidents,
		"pagination": models.Pagination{
			Page:     f.Page,
			PageSize: f.PageSize,
			Total:    total,
		},
	})
}

// Create handles POST /incidents — create a new incident record.
func (h *IncidentHandler) Create(c *gin.Context) {
	var req struct {
		AlertID          string          `json:"alert_id" binding:"required"`
		Title            string          `json:"title" binding:"required"`
		Severity         models.Severity `json:"severity" binding:"required"`
		AffectedServices []string        `json:"affected_services"`
		OpenedBy         string          `json:"opened_by" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}
	if req.AffectedServices == nil {
		req.AffectedServices = []string{}
	}

	inc, err := h.store.Create(c.Request.Context(), req.AlertID, req.Title, req.Severity, req.AffectedServices, req.OpenedBy)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	c.JSON(http.StatusCreated, gin.H{
		"incident_id": inc.IncidentID,
		"title":       inc.Title,
		"status":      inc.Status,
		"opened_at":   inc.OpenedAt.Format(time.RFC3339),
	})
}

// GetByID handles GET /incidents/:incident_id — full incident with report + events.
func (h *IncidentHandler) GetByID(c *gin.Context) {
	inc, err := h.store.GetByID(c.Request.Context(), c.Param("incident_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if inc == nil {
		c.JSON(http.StatusNotFound, errResp(c, "INCIDENT_NOT_FOUND", "no incident found with given ID"))
		return
	}
	c.JSON(http.StatusOK, inc)
}

// Update handles PATCH /incidents/:incident_id — update incident fields.
// If the body contains an "analysis" key, broadcasts analysis.agent_switched.
func (h *IncidentHandler) Update(c *gin.Context) {
	var req map[string]interface{}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	allowed := map[string]bool{"title": true, "severity": true, "status": true, "resolved_at": true, "affected_services": true, "analysis": true}
	fields := make(map[string]interface{})
	for k, v := range req {
		if allowed[k] {
			fields[k] = v
		}
	}

	// analysis is metadata only — don't write it to the DB column (not a real column)
	delete(fields, "analysis")

	// Write actual DB fields
	dbFields := make(map[string]interface{})
	for k, v := range fields {
		dbFields[k] = v
	}

	if len(dbFields) > 0 {
		if err := h.store.Update(c.Request.Context(), c.Param("incident_id"), dbFields); err != nil {
			c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
			return
		}
	}

	incidentID := c.Param("incident_id")

	// Broadcast analysis.agent_switched when LangGraph sends an agent transition
	if analysisData, ok := req["analysis"]; ok {
		if h.hub != nil {
			h.hub.BroadcastToIncident(incidentID, "analysis.agent_switched", gin.H{
				"incident_id": incidentID,
				"analysis":    analysisData,
			})
		}
	}

	c.JSON(http.StatusOK, gin.H{
		"incident_id": incidentID,
		"updated_at":  time.Now().UTC().Format(time.RFC3339),
	})
}

// AddEvent handles POST /incidents/:incident_id/events — stream findings from LangGraph.
// Validates the finding payload schema before persisting.
func (h *IncidentHandler) AddEvent(c *gin.Context) {
	var finding models.Finding
	if err := c.ShouldBindJSON(&finding); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	// ── Schema validation ─────────────────────────────────────────────────────
	var validationErrors []string

	if finding.Agent == "" {
		validationErrors = append(validationErrors, "agent is required")
	} else if !validAgents[strings.ToLower(string(finding.Agent))] {
		validationErrors = append(validationErrors, "agent must be one of: supervisor, log_query_agent, rag_agent, correlation_agent, report_agent")
	}

	if finding.Type == "" {
		validationErrors = append(validationErrors, "type is required")
	}
	if finding.Title == "" {
		validationErrors = append(validationErrors, "title is required")
	}
	if finding.Summary == "" {
		validationErrors = append(validationErrors, "summary is required")
	}

	if string(finding.Severity) != "" && !validSeverities[strings.ToLower(string(finding.Severity))] {
		validationErrors = append(validationErrors, "severity must be one of: critical, high, medium, low, info")
	}

	if finding.Confidence < 0.0 || finding.Confidence > 1.0 {
		validationErrors = append(validationErrors, "confidence must be between 0.0 and 1.0")
	}

	if len(validationErrors) > 0 {
		c.JSON(http.StatusBadRequest, gin.H{
			"error": gin.H{
				"code":    "FINDING_INVALID",
				"message": "finding payload failed schema validation",
				"fields":  validationErrors,
			},
			"request_id": c.GetString("request_id"),
		})
		return
	}

	incidentID := c.Param("incident_id")
	if err := h.store.AddEvent(c.Request.Context(), incidentID, &finding); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Broadcast analysis.finding to incident room subscribers
	if h.hub != nil {
		h.hub.BroadcastToIncident(incidentID, "analysis.finding", finding)
	}

	c.JSON(http.StatusCreated, gin.H{
		"finding_id": finding.FindingID,
		"stored_at":  finding.CreatedAt.Format(time.RFC3339),
	})
}

// AddReport handles POST /incidents/:incident_id/report — submit final report.
func (h *IncidentHandler) AddReport(c *gin.Context) {
	var report models.IncidentReport
	if err := c.ShouldBindJSON(&report); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	incidentID := c.Param("incident_id")
	report.IncidentID = incidentID
	if err := h.store.AddReport(c.Request.Context(), incidentID, &report); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Broadcast analysis.completed to incident room subscribers
	if h.hub != nil {
		h.hub.BroadcastToIncident(incidentID, "analysis.completed", gin.H{
			"incident_id": incidentID,
			"report_id":   report.ReportID,
		})
	}

	c.JSON(http.StatusCreated, gin.H{
		"report_id": report.ReportID,
		"stored_at": report.GeneratedAt.Format(time.RFC3339),
	})
}
