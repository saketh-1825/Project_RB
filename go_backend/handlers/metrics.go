package handlers

import (
	"encoding/json"
	"net/http"
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

// Query handles GET /metrics/query — fetch a time series for a named metric.
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

	var labels map[string]string
	if labelsStr := c.Query("labels"); labelsStr != "" {
		_ = json.Unmarshal([]byte(labelsStr), &labels)
	}
	step := c.DefaultQuery("step", "30s")

	series, err := h.store.Query(c.Request.Context(), metricName, from, to, labels, step)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	c.JSON(http.StatusOK, series)
}

// BatchQuery handles POST /metrics/query/batch — fetch multiple series.
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

// Summary handles GET /metrics/summary — aggregate stats for a metric.
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

	var labels map[string]string
	if labelsStr := c.Query("labels"); labelsStr != "" {
		_ = json.Unmarshal([]byte(labelsStr), &labels)
	}

	summary, err := h.store.Summary(c.Request.Context(), metricName, from, to, labels)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	c.JSON(http.StatusOK, summary)
}

// Catalog handles GET /metrics/catalog — list all known metrics.
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
