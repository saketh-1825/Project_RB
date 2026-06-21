package handlers

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"

	"sre-copilot/db"
	"sre-copilot/models"
)

// TraceHandler handles /api/v1/traces endpoints.
type TraceHandler struct {
	store db.TraceStore
}

func NewTraceHandler(store db.TraceStore) *TraceHandler {
	return &TraceHandler{store: store}
}

// GetByTraceID handles GET /traces/:trace_id — fetch all spans for a trace.
func (h *TraceHandler) GetByTraceID(c *gin.Context) {
	traceID := c.Param("trace_id")
	spans, err := h.store.GetByTraceID(c.Request.Context(), traceID)
	if err != nil {
		if err == pgx.ErrNoRows {
			c.JSON(http.StatusNotFound, errResp(c, "TRACE_NOT_FOUND", "no trace found with given ID"))
			return
		}
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Compute total duration and root service
	var rootService string
	var totalDuration float64
	if len(spans) > 0 {
		rootService = spans[0].Service
		minStart := spans[0].StartTime
		var maxEnd time.Time
		for _, sp := range spans {
			end := sp.StartTime.Add(time.Duration(sp.DurationMs * float64(time.Millisecond)))
			if end.After(maxEnd) {
				maxEnd = end
			}
			if sp.ParentSpanID == nil {
				rootService = sp.Service
			}
		}
		totalDuration = float64(maxEnd.Sub(minStart).Milliseconds())
	}

	c.JSON(http.StatusOK, gin.H{
		"trace_id":          traceID,
		"root_service":      rootService,
		"total_duration_ms": totalDuration,
		"spans":             spans,
	})
}

// Search handles GET /traces — search traces by service, time range, status.
func (h *TraceHandler) Search(c *gin.Context) {
	fromStr := c.Query("from")
	toStr := c.Query("to")
	if fromStr == "" || toStr == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "from and to are required"))
		return
	}

	from, _ := time.Parse(time.RFC3339, fromStr)
	to, _ := time.Parse(time.RFC3339, toStr)

	f := db.TraceFilter{
		From:     from,
		To:       to,
		Service:  c.Query("service"),
		Status:   c.Query("status"),
		Cursor:   c.Query("cursor"),
	}
	f.PageSize, _ = strconv.Atoi(c.DefaultQuery("page_size", "50"))

	if minDur := c.Query("min_duration_ms"); minDur != "" {
		if v, err := strconv.Atoi(minDur); err == nil {
			f.MinDurationMs = &v
		}
	}

	traces, nextCursor, err := h.store.Search(c.Request.Context(), f)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if traces == nil {
		traces = []models.TraceSummary{}
	}

	c.JSON(http.StatusOK, gin.H{
		"traces":      traces,
		"next_cursor": nextCursor,
	})
}
