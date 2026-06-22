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

	"sre-copilot/db"
	"sre-copilot/middleware"
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

	// Load configuration
	cfg, err := LoadConfig()
	if err != nil {
		log.Fatal().Err(err).Msg("failed to load configuration")
	}

	// ── Database Migrations ───────────────────────────────────────────────────
	if err := db.RunMigrations(cfg.PostgresDSN); err != nil {
		log.Fatal().Err(err).Msg("database migrations failed")
	}

	// ── App Init ──────────────────────────────────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	app, err := NewApp(ctx, cfg)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to initialize application")
	}
	defer app.Close()

	// Start WebSocket Hub in background
	go app.WSHub.Run()

	// ── Router ────────────────────────────────────────────────────────────────
	if cfg.Env != "development" {
		gin.SetMode(gin.ReleaseMode)
	}
	r := gin.New()
	r.Use(gin.Recovery())

	// Standard Global Middleware
	r.Use(middleware.RequestID())
	r.Use(middleware.StructuredLogger())

	// ── Public Routes (Unauthenticated) ───────────────────────────────────────
	r.GET("/api/v1/ready", app.SystemHandler.Ready)
	r.GET("/api/v1/health", app.SystemHandler.Health)

	// WebSocket Endpoint
	r.GET("/ws", app.WSHub.HandleWebSocket)

	// ── Authenticated API Routes ──────────────────────────────────────────────
	api := r.Group("/api/v1")
	api.Use(middleware.BearerAuth(cfg.SREInternalToken))
	{
		// Alerts
		api.GET("/alerts", app.AlertHandler.List)
		api.GET("/alerts/:alert_id", app.AlertHandler.GetByID)
		api.POST("/alerts/:alert_id/acknowledge", app.AlertHandler.Acknowledge)
		api.POST("/alerts/:alert_id/suppress", app.AlertHandler.Suppress)

		// Logs
		api.GET("/logs", app.LogHandler.Query)
		api.GET("/logs/anomalies", app.LogHandler.Anomalies)
		api.GET("/logs/:log_id", app.LogHandler.GetByID)

		// Metrics
		api.GET("/metrics/query", app.MetricHandler.Query)
		api.POST("/metrics/query/batch", app.MetricHandler.BatchQuery)
		api.GET("/metrics/summary", app.MetricHandler.Summary)
		api.GET("/metrics/catalog", app.MetricHandler.Catalog)

		// Traces
		api.GET("/traces/:trace_id", app.TraceHandler.GetByTraceID)
		api.GET("/traces", app.TraceHandler.Search)

		// Services
		api.GET("/services", app.ServiceHandler.List)
		api.GET("/services/:service_id/health", app.ServiceHandler.GetHealth)

		// Runbooks
		api.GET("/runbooks", app.RunbookHandler.List)
		api.GET("/runbooks/search", app.RunbookHandler.Search)
		api.GET("/runbooks/:runbook_id", app.RunbookHandler.GetByID)
		api.POST("/runbooks", app.RunbookHandler.Create)

		// Incidents
		api.GET("/incidents", app.IncidentHandler.List)
		api.POST("/incidents", app.IncidentHandler.Create)
		api.GET("/incidents/:incident_id", app.IncidentHandler.GetByID)
		api.PATCH("/incidents/:incident_id", app.IncidentHandler.Update)
		api.POST("/incidents/:incident_id/events", app.IncidentHandler.AddEvent)
		api.POST("/incidents/:incident_id/report", app.IncidentHandler.AddReport)
	}

	// ── Internal Endpoints (Service-to-Service, Bearer Authenticated) ─────
	internal := r.Group("/internal")
	internal.Use(middleware.BearerAuth(cfg.SREInternalToken))
	{
		internal.POST("/logs/ingest", app.LogHandler.Ingest)
	}

	// ── Webhooks (Signature HMAC Authenticated) ──────────────────────────────
	webhooks := r.Group("/webhooks")
	{
		webhooks.POST("/prometheus", middleware.HMACAuth(cfg.PrometheusWebhookSecret), app.WebhookHandler.Prometheus)
		webhooks.POST("/datadog", middleware.HMACAuth(cfg.DatadogWebhookSecret), app.WebhookHandler.Datadog)
		webhooks.POST("/custom", middleware.BearerAuth(cfg.SREInternalToken), app.WebhookHandler.Custom)
	}

	// ── Server lifecycle ──────────────────────────────────────────────────────
	srv := &http.Server{Addr: ":" + cfg.Port, Handler: r}

	go func() {
		log.Info().Str("port", cfg.Port).Msg("go-backend starting")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal().Err(err).Msg("server error")
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatal().Err(err).Msg("forced shutdown")
	}
	log.Info().Msg("go-backend stopped")
}
