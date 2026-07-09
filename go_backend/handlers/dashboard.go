package handlers

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// DashboardHandler provides aggregate summary data for the dashboard UI.
type DashboardHandler struct {
	incidents db.IncidentStore
	alerts    db.AlertStore
	analyses  db.AnalysisStore
}

func NewDashboardHandler(incidents db.IncidentStore, alerts db.AlertStore, analyses db.AnalysisStore) *DashboardHandler {
	return &DashboardHandler{
		incidents: incidents,
		alerts:    alerts,
		analyses:  analyses,
	}
}

// Summary handles GET /api/v1/dashboard/summary.
//
// Returns:
//   - open_incidents  int    — count of incidents with status=open
//   - firing_alerts   int    — count of alerts with status=firing
//   - recent_analyses []     — last 10 analyses (any status)
//   - generated_at    string
func (h *DashboardHandler) Summary(c *gin.Context) {
	ctx := c.Request.Context()

	// ── Open incidents ───────────────────────────────────────────────────────
	_, openCount, err := h.incidents.List(ctx, db.IncidentFilter{
		Status:   "open",
		Page:     1,
		PageSize: 1, // only need total count
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", "incident count: "+err.Error()))
		return
	}

	// ── Firing alerts ────────────────────────────────────────────────────────
	_, firingCount, err := h.alerts.List(ctx, db.AlertFilter{
		Status:   "firing",
		Page:     1,
		PageSize: 1,
	})
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", "alert count: "+err.Error()))
		return
	}

	// ── Recent analyses (last 10) ────────────────────────────────────────────
	analyses, _, err := h.analyses.List(ctx, "", "", 1, 10)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", "analyses list: "+err.Error()))
		return
	}
	if analyses == nil {
		analyses = []models.AnalysisDetail{}
	}

	c.JSON(http.StatusOK, gin.H{
		"open_incidents":  openCount,
		"firing_alerts":   firingCount,
		"recent_analyses": analyses,
		"generated_at":    time.Now().UTC().Format(time.RFC3339),
	})
}
