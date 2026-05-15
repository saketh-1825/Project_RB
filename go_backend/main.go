// sre-copilot/go-backend
// Main entry point — wired up by air for hot-reload in dev.
package main

import (
	"context"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

func main() {
	// ── Logger ────────────────────────────────────────────────────────────────
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnix
	if os.Getenv("LOG_LEVEL") == "debug" {
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	} else {
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}
	log.Logger = log.Output(zerolog.ConsoleWriter{Out: os.Stderr, TimeFormat: time.RFC3339})

	// ── Router ────────────────────────────────────────────────────────────────
	if os.Getenv("ENV") != "development" {
		gin.SetMode(gin.ReleaseMode)
	}
	r := gin.New()
	r.Use(gin.Recovery())

	// ── Health / readiness ────────────────────────────────────────────────────
	r.GET("/api/v1/ready", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"ready": true})
	})
	r.GET("/api/v1/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{
			"status": "ok",
			"components": gin.H{
				"log_store":    "ok",
				"metric_store": "ok",
				"redis":        "ok",
				"vector_index": "ok",
			},
			"uptime_seconds": time.Now().Unix(),
		})
	})

	// ── Alerts ────────────────────────────────────────────────────────────────
	alerts := r.Group("/api/v1/alerts")
	{
		alerts.GET("", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"alerts": []gin.H{}, "pagination": gin.H{"total": 0}})
		})
		alerts.GET("/:id", func(c *gin.Context) {
			c.JSON(http.StatusNotFound, gin.H{"error": gin.H{"code": "ALERT_NOT_FOUND", "message": "not found"}})
		})
		alerts.POST("/:id/acknowledge", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"acknowledged": true, "alert_id": c.Param("id")})
		})
	}

	// ── Logs ─────────────────────────────────────────────────────────────────
	logsGroup := r.Group("/api/v1/logs")
	{
		logsGroup.GET("", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"logs": []gin.H{}, "total_matched": 0})
		})
		logsGroup.GET("/anomalies", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"anomalous_windows": []gin.H{}})
		})
		logsGroup.GET("/:id", func(c *gin.Context) {
			c.JSON(http.StatusNotFound, gin.H{"error": gin.H{"code": "LOG_NOT_FOUND"}})
		})
	}

	// ── Metrics ───────────────────────────────────────────────────────────────
	metrics := r.Group("/api/v1/metrics")
	{
		metrics.GET("/catalog", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"metrics": []gin.H{}})
		})
		metrics.GET("/query", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"data_points": []gin.H{}})
		})
		metrics.POST("/query/batch", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"series": []gin.H{}, "errors": []string{}})
		})
		metrics.GET("/summary", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"min": 0, "max": 0, "avg": 0})
		})
	}

	// ── Traces ────────────────────────────────────────────────────────────────
	traces := r.Group("/api/v1/traces")
	{
		traces.GET("", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"traces": []gin.H{}, "next_cursor": nil})
		})
		traces.GET("/:id", func(c *gin.Context) {
			c.JSON(http.StatusNotFound, gin.H{"error": gin.H{"code": "TRACE_NOT_FOUND"}})
		})
	}

	// ── Services ──────────────────────────────────────────────────────────────
	services := r.Group("/api/v1/services")
	{
		services.GET("", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"services": []gin.H{}})
		})
		services.GET("/:id/health", func(c *gin.Context) {
			c.JSON(http.StatusOK, gin.H{"service_id": c.Param("id"), "health": "unknown"})
		})
	}

	// ── Runbooks & Incidents ──────────────────────────────────────────────────
	r.GET("/api/v1/runbooks", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"runbooks": []gin.H{}})
	})
	r.GET("/api/v1/incidents", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"incidents": []gin.H{}})
	})

	// ── Webhook endpoints (Prometheus / Datadog) ──────────────────────────────
	r.POST("/webhooks/prometheus", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"received": true})
	})
	r.POST("/webhooks/datadog", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"received": true})
	})

	// ── Server lifecycle ──────────────────────────────────────────────────────
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	srv := &http.Server{Addr: ":" + port, Handler: r}

	go func() {
		log.Info().Str("port", port).Msg("go-backend starting")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal().Err(err).Msg("server error")
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal().Err(err).Msg("forced shutdown")
	}
	log.Info().Msg("go-backend stopped")
}
