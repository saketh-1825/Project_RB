package handlers

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// AlertHandler handles all /api/v1/alerts endpoints.
type AlertHandler struct {
	store db.AlertStore
}

func NewAlertHandler(store db.AlertStore) *AlertHandler {
	return &AlertHandler{store: store}
}

// List handles GET /alerts — list all alerts with optional filters.
func (h *AlertHandler) List(c *gin.Context) {
	f := db.AlertFilter{
		Status:   c.Query("status"),
		Severity: c.Query("severity"),
		Service:  c.Query("service"),
	}

	if v := c.Query("from"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err == nil {
			f.From = &t
		}
	}
	if v := c.Query("to"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err == nil {
			f.To = &t
		}
	}
	f.Page, _ = strconv.Atoi(c.DefaultQuery("page", "1"))
	f.PageSize, _ = strconv.Atoi(c.DefaultQuery("page_size", "50"))

	alerts, total, err := h.store.List(c.Request.Context(), f)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if alerts == nil {
		alerts = []models.Alert{}
	}

	c.JSON(http.StatusOK, gin.H{
		"alerts": alerts,
		"pagination": models.Pagination{
			Page:     f.Page,
			PageSize: f.PageSize,
			Total:    total,
		},
	})
}

// GetByID handles GET /alerts/:alert_id — fetch a single alert.
func (h *AlertHandler) GetByID(c *gin.Context) {
	alert, err := h.store.GetByID(c.Request.Context(), c.Param("alert_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if alert == nil {
		c.JSON(http.StatusNotFound, errResp(c, "ALERT_NOT_FOUND", "no alert found with given ID"))
		return
	}
	c.JSON(http.StatusOK, alert)
}

// Acknowledge handles POST /alerts/:alert_id/acknowledge.
func (h *AlertHandler) Acknowledge(c *gin.Context) {
	var req struct {
		AcknowledgedBy string  `json:"acknowledged_by" binding:"required"`
		Note           *string `json:"note"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	alert, err := h.store.Acknowledge(c.Request.Context(), c.Param("alert_id"), req.AcknowledgedBy, req.Note)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if alert == nil {
		c.JSON(http.StatusNotFound, errResp(c, "ALERT_NOT_FOUND", "no alert found with given ID"))
		return
	}
	c.JSON(http.StatusOK, alert)
}

// Suppress handles POST /alerts/:alert_id/suppress.
func (h *AlertHandler) Suppress(c *gin.Context) {
	var req struct {
		DurationMinutes int    `json:"duration_minutes" binding:"required"`
		Reason          string `json:"reason" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	alert, err := h.store.Suppress(c.Request.Context(), c.Param("alert_id"), req.DurationMinutes, req.Reason)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if alert == nil {
		c.JSON(http.StatusNotFound, errResp(c, "ALERT_NOT_FOUND", "no alert found with given ID"))
		return
	}
	c.JSON(http.StatusOK, alert)
}

// errResp builds a contract-compliant ErrorResponse.
func errResp(c *gin.Context, code, message string) models.ErrorResponse {
	return models.ErrorResponse{
		Error: models.ErrorDetail{
			Code:      code,
			Message:   message,
			RequestID: c.GetString("request_id"),
		},
	}
}
