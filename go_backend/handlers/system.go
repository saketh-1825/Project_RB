package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

// SystemHandler handles /api/v1/health and /api/v1/ready endpoints.
type SystemHandler struct {
	pool      *pgxpool.Pool
	redis     *redis.Client // optional; nil if Redis not configured
	startTime time.Time
}

// NewSystemHandler creates a SystemHandler.
// redisClient may be nil when Redis is not configured — the readiness probe
// will then skip the Redis check rather than treating it as a failure.
func NewSystemHandler(pool *pgxpool.Pool, redisClient *redis.Client) *SystemHandler {
	return &SystemHandler{pool: pool, redis: redisClient, startTime: time.Now()}
}

// Health handles GET /api/v1/health — real component health checks.
func (h *SystemHandler) Health(c *gin.Context) {
	ctx := c.Request.Context()
	overallStatus := "ok"

	// ── Postgres (log_store, metric_store, vector_index) ────────────────────
	logStoreStatus := "ok"
	if err := h.pool.Ping(ctx); err != nil {
		logStoreStatus = "down"
		overallStatus = "degraded"
	}
	metricStoreStatus := logStoreStatus
	vectorIndexStatus := logStoreStatus

	// ── Redis ────────────────────────────────────────────────────────────────
	redisStatus := "ok"
	if h.redis != nil {
		if err := h.redis.Ping(ctx).Err(); err != nil {
			redisStatus = "down"
			overallStatus = "degraded"
		}
	}

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

// Ready handles GET /api/v1/ready — Kubernetes readiness probe.
// Returns 200 only when ALL configured critical stores are reachable.
// Returns 503 with reason if any critical store is down.
func (h *SystemHandler) Ready(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 3*time.Second)
	defer cancel()

	// ── Postgres is always required ─────────────────────────────────────────
	if err := h.pool.Ping(ctx); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"ready":  false,
			"reason": "postgres connection failed: " + err.Error(),
		})
		return
	}

	// ── Redis — required only when configured ───────────────────────────────
	if h.redis != nil {
		if err := h.redis.Ping(ctx).Err(); err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{
				"ready":  false,
				"reason": "redis connection failed: " + err.Error(),
			})
			return
		}
	}

	c.JSON(http.StatusOK, gin.H{"ready": true})
}
