package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// MetricHandler handles all /api/v1/metrics endpoints.
type MetricHandler struct {
	store db.MetricStore
}

func NewMetricHandler(store db.MetricStore) *MetricHandler {
	return &MetricHandler{store: store}
}

// parseLabels safely parses the optional ?labels=<json-object> query parameter.
func parseLabels(c *gin.Context) map[string]string {
	var labels map[string]string
	if s := c.Query("labels"); s != "" {
		_ = json.Unmarshal([]byte(s), &labels)
	}
	return labels
}

// Query handles GET /metrics/query — fetch a time series for a named metric.
// Supports optional ?labels={"k":"v"} filter and ?step=1m bucketing.
func (h *MetricHandler) Query(c *gin.Context) {
	metricName := c.Query("metric_name")
	fromStr := c.Query("from")
	toStr := c.Query("to")
	if metricName == "" || fromStr == "" || toStr == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "metric_name, from, and to are required"))
		return
	}

	from, _ := time.Parse(time.RFC3339, fromStr)
	to, _ := time.Parse(time.RFC3339, toStr)

	labels := parseLabels(c)
	step := c.DefaultQuery("step", "")

	series, err := h.store.Query(c.Request.Context(), metricName, from, to, labels, step)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	c.JSON(http.StatusOK, series)
}

// BatchQuery handles POST /metrics/query/batch — parallel goroutine fan-out, partial success.
func (h *MetricHandler) BatchQuery(c *gin.Context) {
	var req struct {
		Queries []db.MetricQueryRequest `json:"queries"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}
	if len(req.Queries) > 20 {
		c.JSON(http.StatusBadRequest, errResp(c, "BATCH_TOO_LARGE", "max 20 queries per batch"))
		return
	}

	series, errors, err := h.store.BatchQuery(c.Request.Context(), req.Queries)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if series == nil {
		series = []models.MetricSeries{}
	}
	if errors == nil {
		errors = []db.MetricQueryError{}
	}

	c.JSON(http.StatusOK, gin.H{
		"series": series,
		"errors": errors,
	})
}

// Summary handles GET /metrics/summary — window aggregation (min/max/avg/p50/p95/p99).
func (h *MetricHandler) Summary(c *gin.Context) {
	metricName := c.Query("metric_name")
	fromStr := c.Query("from")
	toStr := c.Query("to")
	if metricName == "" || fromStr == "" || toStr == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "metric_name, from, and to are required"))
		return
	}

	from, _ := time.Parse(time.RFC3339, fromStr)
	to, _ := time.Parse(time.RFC3339, toStr)
	labels := parseLabels(c)

	summary, err := h.store.Summary(c.Request.Context(), metricName, from, to, labels)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	c.JSON(http.StatusOK, summary)
}

// Catalog handles GET /metrics/catalog — introspect distinct metric names from DB.
func (h *MetricHandler) Catalog(c *gin.Context) {
	entries, err := h.store.Catalog(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if entries == nil {
		entries = []models.MetricCatalogEntry{}
	}
	c.JSON(http.StatusOK, gin.H{"metrics": entries})
}

// Ingest handles POST /internal/metrics/ingest — batch metric ingestion called by the simulator.
func (h *MetricHandler) Ingest(c *gin.Context) {
	var req struct {
		Metrics []db.MetricDataPoint `json:"metrics" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}
	if len(req.Metrics) == 0 {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "metrics array must not be empty"))
		return
	}

	const maxBatchSize = 5000
	if len(req.Metrics) > maxBatchSize {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST",
			"batch size exceeds maximum of "+strconv.Itoa(maxBatchSize)))
		return
	}

	// Default zero timestamps to now
	now := time.Now().UTC()
	for i := range req.Metrics {
		if req.Metrics[i].Timestamp.IsZero() {
			req.Metrics[i].Timestamp = now
		}
	}

	inserted, err := h.store.BulkIngest(c.Request.Context(), req.Metrics)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{
			"error":    err.Error(),
			"inserted": inserted,
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"inserted": inserted,
		"total":    len(req.Metrics),
	})
}
