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

// AlertFilter holds query parameters for listing alerts.
type AlertFilter struct {
	Status   string
	Severity string
	Service  string
	From     *time.Time
	To       *time.Time
	Page     int
	PageSize int
}

// AlertStore defines all alert-related database operations.
type AlertStore interface {
	Save(ctx context.Context, alert *models.Alert) error
	GetByID(ctx context.Context, id string) (*models.Alert, error)
	List(ctx context.Context, f AlertFilter) ([]models.Alert, int, error)
	UpdateStatus(ctx context.Context, id, status string) error
	Acknowledge(ctx context.Context, id, acknowledgedBy string, note *string) (*models.Alert, error)
	Suppress(ctx context.Context, id string, durationMin int, reason string) (*models.Alert, error)
}

type alertStore struct {
	pool *pgxpool.Pool
}

// NewAlertStore creates a new AlertStore backed by the given connection pool.
func NewAlertStore(pool *pgxpool.Pool) AlertStore {
	return &alertStore{pool: pool}
}

func (s *alertStore) Save(ctx context.Context, a *models.Alert) error {
	query := `
		INSERT INTO alerts (
			source, name, severity, status, fired_at, resolved_at,
			labels, annotations, affected_services, generator_url
		) VALUES (
			$1, $2, $3, $4, $5, $6, $7, $8, $9, $10
		) RETURNING alert_id, created_at, updated_at
	`
	var createdAt, updatedAt time.Time
	err := s.pool.QueryRow(ctx, query,
		a.Source, a.Name, a.Severity, a.Status, a.FiredAt, a.ResolvedAt,
		a.Labels, a.Annotations, a.AffectedServices, a.GeneratorURL,
	).Scan(&a.AlertID, &createdAt, &updatedAt)

	if err != nil {
		return fmt.Errorf("alert.Save: %w", err)
	}
	return nil
}

func (s *alertStore) GetByID(ctx context.Context, id string) (*models.Alert, error) {
	query := `
		SELECT alert_id, source, name, severity, status, fired_at, resolved_at,
		       labels, annotations, affected_services, generator_url
		FROM alerts
		WHERE alert_id = $1
	`
	a := &models.Alert{}
	err := s.pool.QueryRow(ctx, query, id).Scan(
		&a.AlertID, &a.Source, &a.Name, &a.Severity, &a.Status, &a.FiredAt, &a.ResolvedAt,
		&a.Labels, &a.Annotations, &a.AffectedServices, &a.GeneratorURL,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("alert.GetByID: %w", err)
	}
	return a, nil
}

// List returns alerts matching the filter along with total count for pagination.
func (s *alertStore) List(ctx context.Context, f AlertFilter) ([]models.Alert, int, error) {
	var conditions []string
	var args []interface{}
	argIdx := 1

	if f.Status != "" {
		conditions = append(conditions, fmt.Sprintf("status = $%d", argIdx))
		args = append(args, f.Status)
		argIdx++
	}
	if f.Severity != "" {
		conditions = append(conditions, fmt.Sprintf("severity = $%d", argIdx))
		args = append(args, f.Severity)
		argIdx++
	}
	if f.Service != "" {
		conditions = append(conditions, fmt.Sprintf("$%d = ANY(affected_services)", argIdx))
		args = append(args, f.Service)
		argIdx++
	}
	if f.From != nil {
		conditions = append(conditions, fmt.Sprintf("fired_at >= $%d", argIdx))
		args = append(args, *f.From)
		argIdx++
	}
	if f.To != nil {
		conditions = append(conditions, fmt.Sprintf("fired_at <= $%d", argIdx))
		args = append(args, *f.To)
		argIdx++
	}

	where := ""
	if len(conditions) > 0 {
		where = "WHERE " + strings.Join(conditions, " AND ")
	}

	// Count query
	countQuery := "SELECT COUNT(*) FROM alerts " + where
	var total int
	if err := s.pool.QueryRow(ctx, countQuery, args...).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("alert.List count: %w", err)
	}

	// Page defaults
	if f.PageSize <= 0 {
		f.PageSize = 50
	}
	if f.Page <= 0 {
		f.Page = 1
	}
	offset := (f.Page - 1) * f.PageSize

	dataQuery := fmt.Sprintf(`
		SELECT alert_id, source, name, severity, status, fired_at, resolved_at,
		       labels, annotations, affected_services, generator_url
		FROM alerts %s
		ORDER BY fired_at DESC
		LIMIT $%d OFFSET $%d
	`, where, argIdx, argIdx+1)
	args = append(args, f.PageSize, offset)

	rows, err := s.pool.Query(ctx, dataQuery, args...)
	if err != nil {
		return nil, 0, fmt.Errorf("alert.List query: %w", err)
	}
	defer rows.Close()

	var alerts []models.Alert
	for rows.Next() {
		var a models.Alert
		if err := rows.Scan(
			&a.AlertID, &a.Source, &a.Name, &a.Severity, &a.Status, &a.FiredAt, &a.ResolvedAt,
			&a.Labels, &a.Annotations, &a.AffectedServices, &a.GeneratorURL,
		); err != nil {
			return nil, 0, fmt.Errorf("alert.List scan: %w", err)
		}
		alerts = append(alerts, a)
	}
	return alerts, total, rows.Err()
}

func (s *alertStore) UpdateStatus(ctx context.Context, id, status string) error {
	query := `UPDATE alerts SET status = $1, updated_at = NOW() WHERE alert_id = $2`
	cmdTag, err := s.pool.Exec(ctx, query, status, id)
	if err != nil {
		return fmt.Errorf("alert.UpdateStatus: %w", err)
	}
	if cmdTag.RowsAffected() == 0 {
		return fmt.Errorf("alert not found")
	}
	return nil
}

// Acknowledge marks an alert as acknowledged. Contract: POST /alerts/:alert_id/acknowledge
func (s *alertStore) Acknowledge(ctx context.Context, id, acknowledgedBy string, note *string) (*models.Alert, error) {
	query := `
		UPDATE alerts
		SET annotations = annotations || jsonb_build_object(
			'acknowledged_by', $2::text,
			'acknowledged_at', NOW()::text,
			'ack_note', COALESCE($3, '')
		), updated_at = NOW()
		WHERE alert_id = $1
		RETURNING alert_id, source, name, severity, status, fired_at, resolved_at,
		          labels, annotations, affected_services, generator_url
	`
	a := &models.Alert{}
	noteVal := ""
	if note != nil {
		noteVal = *note
	}
	err := s.pool.QueryRow(ctx, query, id, acknowledgedBy, noteVal).Scan(
		&a.AlertID, &a.Source, &a.Name, &a.Severity, &a.Status, &a.FiredAt, &a.ResolvedAt,
		&a.Labels, &a.Annotations, &a.AffectedServices, &a.GeneratorURL,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("alert.Acknowledge: %w", err)
	}
	return a, nil
}

// Suppress sets an alert status to suppressed. Contract: POST /alerts/:alert_id/suppress
func (s *alertStore) Suppress(ctx context.Context, id string, durationMin int, reason string) (*models.Alert, error) {
	query := `
		UPDATE alerts
		SET status = 'suppressed',
		    annotations = annotations || jsonb_build_object(
				'suppressed_until', (NOW() + ($2 || ' minutes')::interval)::text,
				'suppress_reason', $3
			),
		    updated_at = NOW()
		WHERE alert_id = $1
		RETURNING alert_id, source, name, severity, status, fired_at, resolved_at,
		          labels, annotations, affected_services, generator_url
	`
	a := &models.Alert{}
	err := s.pool.QueryRow(ctx, query, id, fmt.Sprintf("%d", durationMin), reason).Scan(
		&a.AlertID, &a.Source, &a.Name, &a.Severity, &a.Status, &a.FiredAt, &a.ResolvedAt,
		&a.Labels, &a.Annotations, &a.AffectedServices, &a.GeneratorURL,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("alert.Suppress: %w", err)
	}
	return a, nil
}
