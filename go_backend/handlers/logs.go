package handlers

import (
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// LogHandler handles all /api/v1/logs endpoints.
type LogHandler struct {
	store db.LogStore
}

func NewLogHandler(store db.LogStore) *LogHandler {
	return &LogHandler{store: store}
}

// Query handles GET /logs — primary log query endpoint.
func (h *LogHandler) Query(c *gin.Context) {
	fromStr := c.Query("from")
	toStr := c.Query("to")
	if fromStr == "" || toStr == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "from and to query params are required"))
		return
	}

	from, err := time.Parse(time.RFC3339, fromStr)
	if err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "invalid 'from' timestamp"))
		return
	}
	to, err := time.Parse(time.RFC3339, toStr)
	if err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "invalid 'to' timestamp"))
		return
	}

	f := db.LogFilter{
		From:     from,
		To:       to,
		TraceID:  c.Query("trace_id"),
		Search:   c.Query("search"),
		Regex:    c.Query("regex"),
		Cursor:   c.Query("cursor"),
		Sort:     c.DefaultQuery("sort", "desc"),
	}
	f.PageSize, _ = strconv.Atoi(c.DefaultQuery("page_size", "200"))

	if svc := c.Query("services"); svc != "" {
		f.Services = strings.Split(svc, ",")
	}
	if lvl := c.Query("levels"); lvl != "" {
		f.Levels = strings.Split(lvl, ",")
	}
	if hosts := c.Query("hosts"); hosts != "" {
		f.Hosts = strings.Split(hosts, ",")
	}

	start := time.Now()
	logs, total, nextCursor, err := h.store.Query(c.Request.Context(), f)
	queryDuration := time.Since(start).Milliseconds()

	if err != nil {
		if strings.Contains(err.Error(), "invalid regular expression") {
			c.JSON(http.StatusBadRequest, errResp(c, "LOG_INVALID_REGEX", err.Error()))
			return
		}
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if logs == nil {
		logs = []models.LogEntry{}
	}

	c.JSON(http.StatusOK, gin.H{
		"logs":             logs,
		"total_matched":    total,
		"next_cursor":      nextCursor,
		"query_duration_ms": queryDuration,
	})
}

// Anomalies handles GET /logs/anomalies — pre-computed anomalous log clusters.
func (h *LogHandler) Anomalies(c *gin.Context) {
	fromStr := c.Query("from")
	toStr := c.Query("to")
	if fromStr == "" || toStr == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "from and to query params are required"))
		return
	}

	from, _ := time.Parse(time.RFC3339, fromStr)
	to, _ := time.Parse(time.RFC3339, toStr)

	var services []string
	if svc := c.Query("services"); svc != "" {
		services = strings.Split(svc, ",")
	}
	threshold := 3.0
	if t := c.Query("threshold_multiplier"); t != "" {
		if v, err := strconv.ParseFloat(t, 64); err == nil {
			threshold = v
		}
	}

	windows, err := h.store.GetAnomalies(c.Request.Context(), from, to, services, threshold)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if windows == nil {
		windows = []models.AnomalousWindow{}
	}

	c.JSON(http.StatusOK, gin.H{"anomalous_windows": windows})
}

// GetByID handles GET /logs/:log_id — fetch a single log entry.
func (h *LogHandler) GetByID(c *gin.Context) {
	entry, err := h.store.GetByID(c.Request.Context(), c.Param("log_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if entry == nil {
		c.JSON(http.StatusNotFound, errResp(c, "LOG_NOT_FOUND", "no log entry found with given ID"))
		return
	}
	c.JSON(http.StatusOK, entry)
}
