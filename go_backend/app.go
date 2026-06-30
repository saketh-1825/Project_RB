package main

import (
	"context"
	"fmt"

	"github.com/rs/zerolog/log"

	"sre-copilot/clients"
	"sre-copilot/db"
	"sre-copilot/handlers"
	"sre-copilot/ws"
)

// App manages the application context, state, stores, and handlers.
type App struct {
	Config *Config
	DB     *db.DB

	// Stores
	Alerts    db.AlertStore
	Incidents db.IncidentStore
	Logs      db.LogStore
	Metrics   db.MetricStore
	Traces    db.TraceStore
	Services  db.ServiceStore
	Runbooks  db.RunbookStore
	Analyses  db.AnalysisStore

	// Handlers
	AlertHandler    *handlers.AlertHandler
	LogHandler      *handlers.LogHandler
	MetricHandler   *handlers.MetricHandler
	TraceHandler    *handlers.TraceHandler
	ServiceHandler  *handlers.ServiceHandler
	RunbookHandler  *handlers.RunbookHandler
	IncidentHandler *handlers.IncidentHandler
	SystemHandler   *handlers.SystemHandler
	WebhookHandler  *handlers.WebhookHandler

	// WebSocket Hub
	WSHub *ws.Hub

	// Clients
	LangGraph  *clients.LangGraphClient
	Redis      *clients.RedisClient
	RetryQueue *clients.RetryQueue
	Embedder   *clients.EmbedderClient
}

// NewApp initializes all database stores, API handlers, clients, and services.
func NewApp(ctx context.Context, cfg *Config) (*App, error) {
	// Initialize database connection
	database, err := db.New(ctx, cfg.PostgresDSN)
	if err != nil {
		return nil, fmt.Errorf("app: db init: %w", err)
	}

	// Initialize stores
	alertStore := db.NewAlertStore(database.Pool)
	incidentStore := db.NewIncidentStore(database.Pool)
	logStore := db.NewLogStore(database.Pool)
	metricStore := db.NewMetricStore(database.Pool)
	traceStore := db.NewTraceStore(database.Pool)
	serviceStore := db.NewServiceStore(database.Pool)
	runbookStore := db.NewRunbookStore(database.Pool)
	analysisStore := db.NewAnalysisStore(database.Pool)

	// Initialize external clients
	langGraphClient := clients.NewLangGraphClient(cfg.LangGraphURL, cfg.SREInternalToken)
	embedder := clients.NewEmbedderClient(cfg.EmbedderURL)

	// Initialize Redis + retry queue (best-effort: log error but don't fail startup)
	var redisClient *clients.RedisClient
	var retryQueue *clients.RetryQueue
	rc, err := clients.NewRedisClient(cfg.RedisURL)
	if err != nil {
		log.Warn().Err(err).Msg("app: Redis unavailable, LangGraph retry queue disabled")
	} else {
		redisClient = rc
		retryQueue = clients.NewRetryQueue(rc.Client())
		log.Info().Msg("app: Redis connected, retry queue active")
	}

	// Initialize WebSocket hub (requires LangGraph for human_input forwarding)
	wsHub := ws.NewHub(langGraphClient)

	// Initialize handlers — inject WSHub, RetryQueue, and Embedder
	alertHandler := handlers.NewAlertHandler(alertStore)
	logHandler := handlers.NewLogHandler(logStore)
	metricHandler := handlers.NewMetricHandler(metricStore)
	traceHandler := handlers.NewTraceHandler(traceStore)
	serviceHandler := handlers.NewServiceHandler(serviceStore)
	runbookHandler := handlers.NewRunbookHandler(runbookStore, embedder)
	incidentHandler := handlers.NewIncidentHandler(incidentStore, wsHub)
	systemHandler := handlers.NewSystemHandler(database.Pool)
	webhookHandler := handlers.NewWebhookHandler(alertStore, langGraphClient, retryQueue, wsHub)

	app := &App{
		Config:          cfg,
		DB:              database,
		Alerts:          alertStore,
		Incidents:       incidentStore,
		Logs:            logStore,
		Metrics:         metricStore,
		Traces:          traceStore,
		Services:        serviceStore,
		Runbooks:        runbookStore,
		Analyses:        analysisStore,
		AlertHandler:    alertHandler,
		LogHandler:      logHandler,
		MetricHandler:   metricHandler,
		TraceHandler:    traceHandler,
		ServiceHandler:  serviceHandler,
		RunbookHandler:  runbookHandler,
		IncidentHandler: incidentHandler,
		SystemHandler:   systemHandler,
		WebhookHandler:  webhookHandler,
		WSHub:           wsHub,
		LangGraph:       langGraphClient,
		Redis:           redisClient,
		RetryQueue:      retryQueue,
		Embedder:        embedder,
	}

	return app, nil
}

// StartBackgroundWorkers launches long-running goroutines. Call after NewApp.
func (app *App) StartBackgroundWorkers(ctx context.Context) {
	// WebSocket hub event loop
	go app.WSHub.Run()

	// LangGraph retry worker (only when Redis is available)
	if app.RetryQueue != nil {
		go app.RetryQueue.StartWorker(ctx, app.LangGraph)
		log.Info().Msg("app: LangGraph retry worker started")
	}
}

// Close gracefully closes all connections and releases resources.
func (app *App) Close() {
	if app.DB != nil {
		app.DB.Close()
	}
	if app.Redis != nil {
		if err := app.Redis.Close(); err != nil {
			log.Warn().Err(err).Msg("app: Redis close error")
		}
	}
}
