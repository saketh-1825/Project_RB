package handlers

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// IncidentHandler handles /api/v1/incidents endpoints.
type IncidentHandler struct {
	store db.IncidentStore
}

func NewIncidentHandler(store db.IncidentStore) *IncidentHandler {
	return &IncidentHandler{store: store}
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
func (h *IncidentHandler) Update(c *gin.Context) {
	var req map[string]interface{}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	// Only allow specific fields to be updated
	allowed := map[string]bool{"title": true, "severity": true, "status": true, "resolved_at": true, "affected_services": true}
	fields := make(map[string]interface{})
	for k, v := range req {
		if allowed[k] {
			fields[k] = v
		}
	}

	if len(fields) == 0 {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "no valid fields to update"))
		return
	}

	if err := h.store.Update(c.Request.Context(), c.Param("incident_id"), fields); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"incident_id": c.Param("incident_id"),
		"updated_at":  time.Now().UTC().Format(time.RFC3339),
	})
}

// AddEvent handles POST /incidents/:incident_id/events — stream findings.
func (h *IncidentHandler) AddEvent(c *gin.Context) {
	var finding models.Finding
	if err := c.ShouldBindJSON(&finding); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	if err := h.store.AddEvent(c.Request.Context(), c.Param("incident_id"), &finding); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
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

	report.IncidentID = c.Param("incident_id")
	if err := h.store.AddReport(c.Request.Context(), c.Param("incident_id"), &report); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	c.JSON(http.StatusCreated, gin.H{
		"report_id": report.ReportID,
		"stored_at": report.GeneratedAt.Format(time.RFC3339),
	})
}
