package db

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// IncidentFilter holds query parameters for listing incidents.
type IncidentFilter struct {
	Service  string
	Severity string
	Status   string
	From     *time.Time
	To       *time.Time
	Page     int
	PageSize int
}

// IncidentStore defines all incident-related database operations.
type IncidentStore interface {
	Create(ctx context.Context, alertID, title string, severity models.Severity, services []string, openedBy string) (*models.IncidentSummary, error)
	GetByID(ctx context.Context, id string) (*models.IncidentDetail, error)
	List(ctx context.Context, f IncidentFilter) ([]models.IncidentSummary, int, error)
	Update(ctx context.Context, id string, fields map[string]interface{}) error
	AddEvent(ctx context.Context, incidentID string, finding *models.Finding) error
	AddReport(ctx context.Context, incidentID string, report *models.IncidentReport) error
	GetEvents(ctx context.Context, incidentID string) ([]models.Finding, error)
	GetReport(ctx context.Context, incidentID string) (*models.IncidentReport, error)
}

type incidentStore struct {
	pool *pgxpool.Pool
}

func NewIncidentStore(pool *pgxpool.Pool) IncidentStore {
	return &incidentStore{pool: pool}
}

func (s *incidentStore) Create(ctx context.Context, alertID, title string, severity models.Severity, services []string, openedBy string) (*models.IncidentSummary, error) {
	query := `
		INSERT INTO incidents (alert_id, title, severity, affected_services, opened_by)
		VALUES ($1, $2, $3, $4, $5)
		RETURNING incident_id, title, severity, status, affected_services, opened_at
	`
	inc := &models.IncidentSummary{}
	err := s.pool.QueryRow(ctx, query, alertID, title, severity, services, openedBy).Scan(
		&inc.IncidentID, &inc.Title, &inc.Severity, &inc.Status, &inc.AffectedServices, &inc.OpenedAt,
	)
	if err != nil {
		return nil, fmt.Errorf("incident.Create: %w", err)
	}
	return inc, nil
}

func (s *incidentStore) GetByID(ctx context.Context, id string) (*models.IncidentDetail, error) {
	query := `
		SELECT incident_id, title, severity, status, alert_id, affected_services, opened_at, resolved_at
		FROM incidents WHERE incident_id = $1
	`
	inc := &models.IncidentDetail{}
	err := s.pool.QueryRow(ctx, query, id).Scan(
		&inc.IncidentID, &inc.Title, &inc.Severity, &inc.Status, &inc.AlertID,
		&inc.AffectedServices, &inc.OpenedAt, &inc.ResolvedAt,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("incident.GetByID: %w", err)
	}

	// Load events
	events, err := s.GetEvents(ctx, id)
	if err != nil {
		return nil, err
	}
	inc.Events = events

	// Load report
	report, err := s.GetReport(ctx, id)
	if err != nil {
		return nil, err
	}
	inc.Report = report

	return inc, nil
}

func (s *incidentStore) List(ctx context.Context, f IncidentFilter) ([]models.IncidentSummary, int, error) {
	var conds []string
	var args []interface{}
	idx := 1

	if f.Service != "" {
		conds = append(conds, fmt.Sprintf("$%d = ANY(affected_services)", idx))
		args = append(args, f.Service)
		idx++
	}
	if f.Severity != "" {
		conds = append(conds, fmt.Sprintf("severity = $%d", idx))
		args = append(args, f.Severity)
		idx++
	}
	if f.Status != "" {
		conds = append(conds, fmt.Sprintf("status = $%d", idx))
		args = append(args, f.Status)
		idx++
	}
	if f.From != nil {
		conds = append(conds, fmt.Sprintf("opened_at >= $%d", idx))
		args = append(args, *f.From)
		idx++
	}
	if f.To != nil {
		conds = append(conds, fmt.Sprintf("opened_at <= $%d", idx))
		args = append(args, *f.To)
		idx++
	}

	where := ""
	if len(conds) > 0 {
		where = "WHERE " + strings.Join(conds, " AND ")
	}

	var total int
	if err := s.pool.QueryRow(ctx, "SELECT COUNT(*) FROM incidents "+where, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("incident.List count: %w", err)
	}

	if f.PageSize <= 0 {
		f.PageSize = 20
	}
	if f.Page <= 0 {
		f.Page = 1
	}
	offset := (f.Page - 1) * f.PageSize

	q := fmt.Sprintf(`
		SELECT incident_id, title, severity, status, affected_services, opened_at, resolved_at
		FROM incidents %s ORDER BY opened_at DESC LIMIT $%d OFFSET $%d
	`, where, idx, idx+1)
	args = append(args, f.PageSize, offset)

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, 0, fmt.Errorf("incident.List query: %w", err)
	}
	defer rows.Close()

	var incidents []models.IncidentSummary
	for rows.Next() {
		var i models.IncidentSummary
		if err := rows.Scan(&i.IncidentID, &i.Title, &i.Severity, &i.Status,
			&i.AffectedServices, &i.OpenedAt, &i.ResolvedAt); err != nil {
			return nil, 0, fmt.Errorf("incident.List scan: %w", err)
		}
		incidents = append(incidents, i)
	}
	return incidents, total, rows.Err()
}

func (s *incidentStore) Update(ctx context.Context, id string, fields map[string]interface{}) error {
	var sets []string
	var args []interface{}
	idx := 1

	for k, v := range fields {
		sets = append(sets, fmt.Sprintf("%s = $%d", k, idx))
		args = append(args, v)
		idx++
	}
	sets = append(sets, "updated_at = NOW()")
	args = append(args, id)

	q := fmt.Sprintf("UPDATE incidents SET %s WHERE incident_id = $%d", strings.Join(sets, ", "), idx)
	cmdTag, err := s.pool.Exec(ctx, q, args...)
	if err != nil {
		return fmt.Errorf("incident.Update: %w", err)
	}
	if cmdTag.RowsAffected() == 0 {
		return fmt.Errorf("incident not found")
	}
	return nil
}

func (s *incidentStore) AddEvent(ctx context.Context, incidentID string, f *models.Finding) error {
	evidenceJSON, _ := json.Marshal(f.Evidence)
	query := `
		INSERT INTO incident_events (incident_id, agent, type, severity, title, summary, evidence, confidence)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		RETURNING finding_id, created_at
	`
	return s.pool.QueryRow(ctx, query,
		incidentID, f.Agent, f.Type, f.Severity, f.Title, f.Summary, evidenceJSON, f.Confidence,
	).Scan(&f.FindingID, &f.CreatedAt)
}

func (s *incidentStore) AddReport(ctx context.Context, incidentID string, r *models.IncidentReport) error {
	rootCauseJSON, _ := json.Marshal(r.RootCause)
	timelineJSON, _ := json.Marshal(r.Timeline)
	fixesJSON, _ := json.Marshal(r.SuggestedFixes)
	similarJSON, _ := json.Marshal(r.SimilarPastIncidents)
	runbooksJSON, _ := json.Marshal(r.RunbooksConsulted)
	metadataJSON, _ := json.Marshal(r.ModelMetadata)

	query := `
		INSERT INTO incident_reports (
			incident_id, alert_id, title, executive_summary, root_cause,
			timeline, suggested_fixes, similar_past_incidents, runbooks_consulted, model_metadata
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
		RETURNING report_id, generated_at
	`
	return s.pool.QueryRow(ctx, query,
		incidentID, r.AlertID, r.Title, r.ExecutiveSummary, rootCauseJSON,
		timelineJSON, fixesJSON, similarJSON, runbooksJSON, metadataJSON,
	).Scan(&r.ReportID, &r.GeneratedAt)
}

func (s *incidentStore) GetEvents(ctx context.Context, incidentID string) ([]models.Finding, error) {
	query := `
		SELECT finding_id, agent, type, severity, title, summary, evidence, confidence, created_at
		FROM incident_events WHERE incident_id = $1 ORDER BY created_at ASC
	`
	rows, err := s.pool.Query(ctx, query, incidentID)
	if err != nil {
		return nil, fmt.Errorf("incident.GetEvents: %w", err)
	}
	defer rows.Close()

	var findings []models.Finding
	for rows.Next() {
		var f models.Finding
		var evidenceJSON []byte
		if err := rows.Scan(&f.FindingID, &f.Agent, &f.Type, &f.Severity,
			&f.Title, &f.Summary, &evidenceJSON, &f.Confidence, &f.CreatedAt); err != nil {
			return nil, fmt.Errorf("incident.GetEvents scan: %w", err)
		}
		_ = json.Unmarshal(evidenceJSON, &f.Evidence)
		findings = append(findings, f)
	}
	return findings, rows.Err()
}

func (s *incidentStore) GetReport(ctx context.Context, incidentID string) (*models.IncidentReport, error) {
	query := `
		SELECT report_id, incident_id, alert_id, generated_at, title, executive_summary,
		       root_cause, timeline, suggested_fixes, similar_past_incidents, runbooks_consulted, model_metadata
		FROM incident_reports WHERE incident_id = $1
	`
	r := &models.IncidentReport{}
	var rcJSON, tlJSON, sfJSON, spJSON, rbJSON, mmJSON []byte
	err := s.pool.QueryRow(ctx, query, incidentID).Scan(
		&r.ReportID, &r.IncidentID, &r.AlertID, &r.GeneratedAt, &r.Title, &r.ExecutiveSummary,
		&rcJSON, &tlJSON, &sfJSON, &spJSON, &rbJSON, &mmJSON,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("incident.GetReport: %w", err)
	}
	_ = json.Unmarshal(rcJSON, &r.RootCause)
	_ = json.Unmarshal(tlJSON, &r.Timeline)
	_ = json.Unmarshal(sfJSON, &r.SuggestedFixes)
	_ = json.Unmarshal(spJSON, &r.SimilarPastIncidents)
	_ = json.Unmarshal(rbJSON, &r.RunbooksConsulted)
	_ = json.Unmarshal(mmJSON, &r.ModelMetadata)
	return r, nil
}
