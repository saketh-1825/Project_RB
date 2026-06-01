// Package models defines all shared domain types from the SRE Copilot contract.
// These types are used across db stores, handlers, webhooks, and WebSocket events.
package models

import "time"

// ─── Enums ───────────────────────────────────────────────────────────────────

type Severity string

const (
	SeverityCritical Severity = "critical"
	SeverityHigh     Severity = "high"
	SeverityMedium   Severity = "medium"
	SeverityLow      Severity = "low"
	SeverityInfo     Severity = "info"
)

type AnalysisStatus string

const (
	AnalysisPending       AnalysisStatus = "pending"
	AnalysisRunning       AnalysisStatus = "running"
	AnalysisAwaitingHuman AnalysisStatus = "awaiting_human"
	AnalysisCompleted     AnalysisStatus = "completed"
	AnalysisFailed        AnalysisStatus = "failed"
	AnalysisCancelled     AnalysisStatus = "cancelled"
)

type AgentName string

const (
	AgentSupervisor  AgentName = "supervisor"
	AgentLogQuery    AgentName = "log_query_agent"
	AgentRAG         AgentName = "rag_agent"
	AgentCorrelation AgentName = "correlation_agent"
	AgentReport      AgentName = "report_agent"
)

type ServiceHealth string

const (
	HealthHealthy  ServiceHealth = "healthy"
	HealthDegraded ServiceHealth = "degraded"
	HealthDown     ServiceHealth = "down"
	HealthUnknown  ServiceHealth = "unknown"
)

// ─── Core Domain Types ───────────────────────────────────────────────────────

// Alert represents a monitoring alert from Prometheus, Datadog, Grafana, or a custom webhook.
type Alert struct {
	AlertID          string                 `json:"alert_id"`
	Source           string                 `json:"source"`
	Name             string                 `json:"name"`
	Severity         Severity               `json:"severity"`
	Status           string                 `json:"status"`
	FiredAt          time.Time              `json:"fired_at"`
	ResolvedAt       *time.Time             `json:"resolved_at,omitempty"`
	Labels           map[string]interface{} `json:"labels"`
	Annotations      map[string]interface{} `json:"annotations"`
	AffectedServices []string               `json:"affected_services"`
	GeneratorURL     *string                `json:"generator_url,omitempty"`
}

// LogEntry represents a single structured log line.
type LogEntry struct {
	ID         string                 `json:"id"`
	Timestamp  time.Time              `json:"timestamp"`
	Level      string                 `json:"level"`
	Service    string                 `json:"service"`
	Host       string                 `json:"host"`
	Message    string                 `json:"message"`
	TraceID    *string                `json:"trace_id,omitempty"`
	SpanID     *string                `json:"span_id,omitempty"`
	Attributes map[string]interface{} `json:"attributes"`
}

// MetricPoint is a single data point in a time series.
type MetricPoint struct {
	Timestamp time.Time              `json:"timestamp"`
	Value     float64                `json:"value"`
	Labels    map[string]interface{} `json:"labels"`
}

// MetricSeries is a named time series with data points.
type MetricSeries struct {
	MetricName string        `json:"metric_name"`
	Unit       string        `json:"unit"`
	DataPoints []MetricPoint `json:"data_points"`
}

// MetricCatalogEntry describes a known metric.
type MetricCatalogEntry struct {
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Labels      []string `json:"labels"`
	Unit        string   `json:"unit"`
}

// MetricSummary holds aggregate statistics for a metric in a time window.
type MetricSummary struct {
	MetricName string  `json:"metric_name"`
	Min        float64 `json:"min"`
	Max        float64 `json:"max"`
	Avg        float64 `json:"avg"`
	P50        float64 `json:"p50"`
	P95        float64 `json:"p95"`
	P99        float64 `json:"p99"`
	Unit       string  `json:"unit"`
}

// Span represents a single span in a distributed trace.
type Span struct {
	TraceID      string                 `json:"trace_id"`
	SpanID       string                 `json:"span_id"`
	ParentSpanID *string                `json:"parent_span_id,omitempty"`
	Service      string                 `json:"service"`
	Operation    string                 `json:"operation"`
	StartTime    time.Time              `json:"start_time"`
	DurationMs   float64                `json:"duration_ms"`
	Status       string                 `json:"status"`
	Attributes   map[string]interface{} `json:"attributes"`
	ErrorMessage *string                `json:"error_message,omitempty"`
}

// ServiceDependency describes a downstream dependency of a service.
type ServiceDependency struct {
	ServiceID       string  `json:"service_id"`
	CallType        string  `json:"call_type"`
	AvgLatencyMs    float64 `json:"avg_latency_ms"`
	ErrorRatePercent float64 `json:"error_rate_percent"`
}

// ServiceNode represents a service in the dependency graph.
type ServiceNode struct {
	ServiceID    string                 `json:"service_id"`
	Name         string                 `json:"name"`
	Health       ServiceHealth          `json:"health"`
	Version      string                 `json:"version"`
	Dependencies []ServiceDependency    `json:"dependencies"`
	Tags         map[string]interface{} `json:"tags"`
}

// ServiceHealthDetail is the health snapshot for a single service.
type ServiceHealthDetail struct {
	ServiceID       string        `json:"service_id"`
	Health          ServiceHealth `json:"health"`
	ErrorRate1m     float64       `json:"error_rate_1m"`
	P99LatencyMs    float64       `json:"p99_latency_ms"`
	ActiveInstances int           `json:"active_instances"`
	LastDeploy      *DeployInfo   `json:"last_deploy,omitempty"`
}

// DeployInfo describes the last deployment of a service.
type DeployInfo struct {
	Timestamp  time.Time `json:"timestamp"`
	Version    string    `json:"version"`
	DeployedBy string    `json:"deployed_by"`
}

// Runbook is a stored operational playbook.
type Runbook struct {
	RunbookID       string    `json:"runbook_id"`
	Title           string    `json:"title"`
	Tags            []string  `json:"tags"`
	Services        []string  `json:"services"`
	Content         string    `json:"content"`
	SimilarityScore *float64  `json:"similarity_score,omitempty"`
	LastUpdated     time.Time `json:"last_updated"`
}

// Evidence supports a Finding with references to logs, metrics, and traces.
type Evidence struct {
	LogIDs      []string   `json:"log_ids"`
	MetricNames []string   `json:"metric_names"`
	TraceIDs    []string   `json:"trace_ids"`
	TimeRange   *TimeRange `json:"time_range,omitempty"`
	RawSnippets []string   `json:"raw_snippets"`
}

// Finding is a single insight produced by a LangGraph agent.
type Finding struct {
	FindingID  string    `json:"finding_id"`
	Agent      AgentName `json:"agent"`
	Type       string    `json:"type"`
	Severity   Severity  `json:"severity"`
	Title      string    `json:"title"`
	Summary    string    `json:"summary"`
	Evidence   Evidence  `json:"evidence"`
	Confidence float64   `json:"confidence"`
	CreatedAt  time.Time `json:"created_at"`
}

// RootCause describes the determined root cause of an incident.
type RootCause struct {
	Description        string    `json:"description"`
	AffectedServices   []string  `json:"affected_services"`
	Confidence         float64   `json:"confidence"`
	SupportingFindings []Finding `json:"supporting_findings"`
}

// TimelineEvent is a single entry in the incident timeline.
type TimelineEvent struct {
	Timestamp time.Time `json:"timestamp"`
	Event     string    `json:"event"`
	Source    string    `json:"source"`
}

// SuggestedFix is a recommended remediation action.
type SuggestedFix struct {
	Priority         int     `json:"priority"`
	Action           string  `json:"action"`
	Rationale        string  `json:"rationale"`
	RunbookReference *string `json:"runbook_reference,omitempty"`
	RiskLevel        string  `json:"risk_level"`
}

// SimilarIncident references a past incident for correlation.
type SimilarIncident struct {
	IncidentID      string  `json:"incident_id"`
	SimilarityScore float64 `json:"similarity_score"`
	Resolution      string  `json:"resolution"`
}

// ModelMetadata captures LLM usage statistics for an analysis.
type ModelMetadata struct {
	TotalTokensUsed    int       `json:"total_tokens_used"`
	AgentsInvoked      []AgentName `json:"agents_invoked"`
	AnalysisDurationMs int       `json:"analysis_duration_ms"`
}

// IncidentReport is the final output of a LangGraph analysis.
type IncidentReport struct {
	ReportID            string            `json:"report_id"`
	IncidentID          string            `json:"incident_id"`
	AlertID             string            `json:"alert_id"`
	GeneratedAt         time.Time         `json:"generated_at"`
	Title               string            `json:"title"`
	ExecutiveSummary    string            `json:"executive_summary"`
	RootCause           RootCause         `json:"root_cause"`
	Timeline            []TimelineEvent   `json:"timeline"`
	SuggestedFixes      []SuggestedFix    `json:"suggested_fixes"`
	SimilarPastIncidents []SimilarIncident `json:"similar_past_incidents"`
	RunbooksConsulted   []Runbook         `json:"runbooks_consulted"`
	ModelMetadata       ModelMetadata     `json:"model_metadata"`
}

// ─── Shared Request/Response Types ───────────────────────────────────────────

// Pagination holds pagination metadata for list responses.
type Pagination struct {
	Page       int     `json:"page"`
	PageSize   int     `json:"page_size"`
	Total      int     `json:"total"`
	NextCursor *string `json:"next_cursor,omitempty"`
}

// TimeRange represents a time window.
type TimeRange struct {
	From time.Time `json:"from"`
	To   time.Time `json:"to"`
}

// ErrorDetail is the inner error object in an ErrorResponse.
type ErrorDetail struct {
	Code      string      `json:"code"`
	Message   string      `json:"message"`
	Details   interface{} `json:"details,omitempty"`
	RequestID string      `json:"request_id"`
}

// ErrorResponse is the standard error envelope.
type ErrorResponse struct {
	Error ErrorDetail `json:"error"`
}

// AnomalousWindow represents a detected log anomaly window.
type AnomalousWindow struct {
	WindowStart  time.Time `json:"window_start"`
	WindowEnd    time.Time `json:"window_end"`
	Service      string    `json:"service"`
	ErrorRate    float64   `json:"error_rate"`
	BaselineRate float64   `json:"baseline_rate"`
	SpikeFactor  float64   `json:"spike_factor"`
	SampleLogIDs []string  `json:"sample_log_ids"`
}

// ─── Incident List/Detail Types ──────────────────────────────────────────────

// IncidentSummary is the compact form returned in list responses.
type IncidentSummary struct {
	IncidentID       string    `json:"incident_id"`
	Title            string    `json:"title"`
	Severity         Severity  `json:"severity"`
	Status           string    `json:"status"`
	AffectedServices []string  `json:"affected_services"`
	OpenedAt         time.Time `json:"opened_at"`
	ResolvedAt       *time.Time `json:"resolved_at,omitempty"`
	RootCauseSummary *string   `json:"root_cause_summary,omitempty"`
}

// IncidentDetail is the full incident including report and events.
type IncidentDetail struct {
	IncidentID       string          `json:"incident_id"`
	Title            string          `json:"title"`
	Severity         Severity        `json:"severity"`
	Status           string          `json:"status"`
	AlertID          string          `json:"alert_id"`
	AffectedServices []string        `json:"affected_services"`
	OpenedAt         time.Time       `json:"opened_at"`
	ResolvedAt       *time.Time      `json:"resolved_at,omitempty"`
	Report           *IncidentReport `json:"report,omitempty"`
	Events           []Finding       `json:"events"`
}

// ─── Analysis Types ──────────────────────────────────────────────────────────

// AnalysisDetail is the full analysis status returned by GET /analyses/:id.
type AnalysisDetail struct {
	AnalysisID   string         `json:"analysis_id"`
	AlertID      string         `json:"alert_id"`
	IncidentID   *string        `json:"incident_id,omitempty"`
	Status       AnalysisStatus `json:"status"`
	CurrentAgent *AgentName     `json:"current_agent,omitempty"`
	Progress     *AnalysisProgress `json:"progress,omitempty"`
	FindingsSoFar int           `json:"findings_so_far"`
	StartedAt    time.Time      `json:"started_at"`
	CompletedAt  *time.Time     `json:"completed_at,omitempty"`
	ReportID     *string        `json:"report_id,omitempty"`
	Error        *string        `json:"error,omitempty"`
}

// AnalysisProgress tracks step-level progress.
type AnalysisProgress struct {
	StepsCompleted         int    `json:"steps_completed"`
	StepsTotal             int    `json:"steps_total"`
	CurrentStepDescription string `json:"current_step_description"`
}

// TraceSummary is the compact form returned in trace search results.
type TraceSummary struct {
	TraceID     string    `json:"trace_id"`
	RootService string   `json:"root_service"`
	Status      string    `json:"status"`
	DurationMs  float64   `json:"duration_ms"`
	StartedAt   time.Time `json:"started_at"`
	SpanCount   int       `json:"span_count"`
}
