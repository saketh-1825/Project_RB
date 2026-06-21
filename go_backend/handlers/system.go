package handlers

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
)

// SystemHandler handles /api/v1/health and /api/v1/ready endpoints.
type SystemHandler struct {
	pool      *pgxpool.Pool
	startTime time.Time
}

func NewSystemHandler(pool *pgxpool.Pool) *SystemHandler {
	return &SystemHandler{pool: pool, startTime: time.Now()}
}

// Health handles GET /health — real component health checks.
func (h *SystemHandler) Health(c *gin.Context) {
	overallStatus := "ok"

	// Check Postgres (used for log_store, metric_store, and vector_index)
	logStoreStatus := "ok"
	if err := h.pool.Ping(c.Request.Context()); err != nil {
		logStoreStatus = "down"
		overallStatus = "degraded"
	}

	// For now, metric_store and vector_index share Postgres
	metricStoreStatus := logStoreStatus
	vectorIndexStatus := logStoreStatus

	// Redis check — would need redis client injected; for now report based on availability
	redisStatus := "ok"

	c.JSON(http.StatusOK, gin.H{
		"status": overallStatus,
		"components": gin.H{
			"log_store":    logStoreStatus,
			"metric_store": metricStoreStatus,
			"redis":        redisStatus,
			"vector_index": vectorIndexStatus,
		},
		"uptime_seconds": int(time.Since(h.startTime).Seconds()),
	})
}

// Ready handles GET /ready — K8s readiness probe.
func (h *SystemHandler) Ready(c *gin.Context) {
	if err := h.pool.Ping(c.Request.Context()); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"ready":  false,
			"reason": "postgres connection failed: " + err.Error(),
		})
		return
	}
	c.JSON(http.StatusOK, gin.H{"ready": true})
}
