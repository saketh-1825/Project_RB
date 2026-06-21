package main

import (
	"context"
	"fmt"

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
	LangGraph *clients.LangGraphClient
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

	// Initialize WebSocket hub
	wsHub := ws.NewHub()

	// Initialize external clients
	langGraphClient := clients.NewLangGraphClient(cfg.LangGraphURL, cfg.SREInternalToken)

	// Initialize handlers
	alertHandler := handlers.NewAlertHandler(alertStore)
	logHandler := handlers.NewLogHandler(logStore)
	metricHandler := handlers.NewMetricHandler(metricStore)
	traceHandler := handlers.NewTraceHandler(traceStore)
	serviceHandler := handlers.NewServiceHandler(serviceStore)
	runbookHandler := handlers.NewRunbookHandler(runbookStore)
	incidentHandler := handlers.NewIncidentHandler(incidentStore)
	systemHandler := handlers.NewSystemHandler(database.Pool)
	webhookHandler := handlers.NewWebhookHandler(alertStore, langGraphClient)

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
	}

	return app, nil
}

// Close gracefully closes all database connections and releases resources.
func (app *App) Close() {
	if app.DB != nil {
		app.DB.Close()
	}
}
