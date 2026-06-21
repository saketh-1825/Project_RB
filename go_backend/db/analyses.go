package db

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// AnalysisStore defines analysis session database operations.
type AnalysisStore interface {
	Create(ctx context.Context, alertID string) (*models.AnalysisDetail, error)
	GetByID(ctx context.Context, id string) (*models.AnalysisDetail, error)
	List(ctx context.Context, status, alertID string, page, pageSize int) ([]models.AnalysisDetail, int, error)
	UpdateStatus(ctx context.Context, id string, status models.AnalysisStatus) error
	Update(ctx context.Context, id string, fields map[string]interface{}) error
}

type analysisStore struct {
	pool *pgxpool.Pool
}

func NewAnalysisStore(pool *pgxpool.Pool) AnalysisStore {
	return &analysisStore{pool: pool}
}

func (s *analysisStore) Create(ctx context.Context, alertID string) (*models.AnalysisDetail, error) {
	q := `
		INSERT INTO analyses (alert_id) VALUES ($1)
		RETURNING analysis_id, alert_id, status, started_at
	`
	a := &models.AnalysisDetail{}
	err := s.pool.QueryRow(ctx, q, alertID).Scan(&a.AnalysisID, &a.AlertID, &a.Status, &a.StartedAt)
	if err != nil {
		return nil, fmt.Errorf("analysis.Create: %w", err)
	}
	return a, nil
}

func (s *analysisStore) GetByID(ctx context.Context, id string) (*models.AnalysisDetail, error) {
	q := `
		SELECT analysis_id, alert_id, incident_id, status, current_agent,
		       steps_completed, steps_total, current_step_desc, findings_count,
		       report_id, error_message, started_at, completed_at
		FROM analyses WHERE analysis_id = $1
	`
	a := &models.AnalysisDetail{}
	var currentAgent, currentStepDesc, reportID, errorMsg, incidentID *string
	var stepsCompleted, stepsTotal int
	var completedAt *time.Time

	err := s.pool.QueryRow(ctx, q, id).Scan(
		&a.AnalysisID, &a.AlertID, &incidentID, &a.Status, &currentAgent,
		&stepsCompleted, &stepsTotal, &currentStepDesc, &a.FindingsSoFar,
		&reportID, &errorMsg, &a.StartedAt, &completedAt,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("analysis.GetByID: %w", err)
	}

	a.IncidentID = incidentID
	a.CompletedAt = completedAt
	a.ReportID = reportID
	a.Error = errorMsg
	if currentAgent != nil {
		agent := models.AgentName(*currentAgent)
		a.CurrentAgent = &agent
	}
	a.Progress = &models.AnalysisProgress{
		StepsCompleted:         stepsCompleted,
		StepsTotal:             stepsTotal,
		CurrentStepDescription: "",
	}
	if currentStepDesc != nil {
		a.Progress.CurrentStepDescription = *currentStepDesc
	}

	return a, nil
}

func (s *analysisStore) List(ctx context.Context, status, alertID string, page, pageSize int) ([]models.AnalysisDetail, int, error) {
	var conds []string
	var args []interface{}
	idx := 1

	if status != "" {
		conds = append(conds, fmt.Sprintf("status = $%d", idx))
		args = append(args, status)
		idx++
	}
	if alertID != "" {
		conds = append(conds, fmt.Sprintf("alert_id = $%d", idx))
		args = append(args, alertID)
		idx++
	}

	where := ""
	if len(conds) > 0 {
		where = "WHERE " + strings.Join(conds, " AND ")
	}

	var total int
	_ = s.pool.QueryRow(ctx, "SELECT COUNT(*) FROM analyses "+where, args...).Scan(&total)

	if pageSize <= 0 {
		pageSize = 20
	}
	if page <= 0 {
		page = 1
	}
	offset := (page - 1) * pageSize

	q := fmt.Sprintf(`
		SELECT analysis_id, alert_id, status, started_at, completed_at
		FROM analyses %s ORDER BY started_at DESC LIMIT $%d OFFSET $%d
	`, where, idx, idx+1)
	args = append(args, pageSize, offset)

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, 0, fmt.Errorf("analysis.List: %w", err)
	}
	defer rows.Close()

	var analyses []models.AnalysisDetail
	for rows.Next() {
		var a models.AnalysisDetail
		if err := rows.Scan(&a.AnalysisID, &a.AlertID, &a.Status, &a.StartedAt, &a.CompletedAt); err != nil {
			return nil, 0, fmt.Errorf("analysis.List scan: %w", err)
		}
		analyses = append(analyses, a)
	}
	return analyses, total, rows.Err()
}

func (s *analysisStore) UpdateStatus(ctx context.Context, id string, status models.AnalysisStatus) error {
	q := `UPDATE analyses SET status = $1, updated_at = NOW() WHERE analysis_id = $2`
	if status == models.AnalysisCompleted || status == models.AnalysisFailed || status == models.AnalysisCancelled {
		q = `UPDATE analyses SET status = $1, completed_at = NOW(), updated_at = NOW() WHERE analysis_id = $2`
	}
	cmdTag, err := s.pool.Exec(ctx, q, status, id)
	if err != nil {
		return fmt.Errorf("analysis.UpdateStatus: %w", err)
	}
	if cmdTag.RowsAffected() == 0 {
		return fmt.Errorf("analysis not found")
	}
	return nil
}

func (s *analysisStore) Update(ctx context.Context, id string, fields map[string]interface{}) error {
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

	q := fmt.Sprintf("UPDATE analyses SET %s WHERE analysis_id = $%d", strings.Join(sets, ", "), idx)
	_, err := s.pool.Exec(ctx, q, args...)
	return err
}
