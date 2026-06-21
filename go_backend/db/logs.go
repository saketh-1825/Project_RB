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

// LogFilter holds query parameters for the GET /logs endpoint.
type LogFilter struct {
	From     time.Time
	To       time.Time
	Services []string
	Levels   []string
	Hosts    []string
	TraceID  string
	Search   string
	Regex    string
	PageSize int
	Cursor   string
	Sort     string // "asc" or "desc"
}

// LogStore defines log-related database operations.
type LogStore interface {
	Query(ctx context.Context, f LogFilter) ([]models.LogEntry, int, *string, error)
	GetByID(ctx context.Context, id string) (*models.LogEntry, error)
	GetAnomalies(ctx context.Context, from, to time.Time, services []string, thresholdMult float64) ([]models.AnomalousWindow, error)
	Ingest(ctx context.Context, entry *models.LogEntry) error
}

type logStore struct {
	pool *pgxpool.Pool
}

func NewLogStore(pool *pgxpool.Pool) LogStore {
	return &logStore{pool: pool}
}

func (s *logStore) Query(ctx context.Context, f LogFilter) ([]models.LogEntry, int, *string, error) {
	var conds []string
	var args []interface{}
	idx := 1

	conds = append(conds, fmt.Sprintf("timestamp >= $%d", idx))
	args = append(args, f.From)
	idx++
	conds = append(conds, fmt.Sprintf("timestamp <= $%d", idx))
	args = append(args, f.To)
	idx++

	if len(f.Services) > 0 {
		conds = append(conds, fmt.Sprintf("service = ANY($%d)", idx))
		args = append(args, f.Services)
		idx++
	}
	if len(f.Levels) > 0 {
		conds = append(conds, fmt.Sprintf("level = ANY($%d)", idx))
		args = append(args, f.Levels)
		idx++
	}
	if len(f.Hosts) > 0 {
		conds = append(conds, fmt.Sprintf("host = ANY($%d)", idx))
		args = append(args, f.Hosts)
		idx++
	}
	if f.TraceID != "" {
		conds = append(conds, fmt.Sprintf("trace_id = $%d", idx))
		args = append(args, f.TraceID)
		idx++
	}
	if f.Search != "" {
		conds = append(conds, fmt.Sprintf("to_tsvector('english', message) @@ plainto_tsquery('english', $%d)", idx))
		args = append(args, f.Search)
		idx++
	}
	if f.Regex != "" {
		conds = append(conds, fmt.Sprintf("message ~ $%d", idx))
		args = append(args, f.Regex)
		idx++
	}
	if f.Cursor != "" {
		// Cursor is the log_id of the last item from the previous page.
		// We use keyset pagination on (timestamp, log_id).
		op := "<"
		if f.Sort == "asc" {
			op = ">"
		}
		conds = append(conds, fmt.Sprintf("(timestamp, log_id) %s (SELECT timestamp, log_id FROM logs WHERE log_id = $%d)", op, idx))
		args = append(args, f.Cursor)
		idx++
	}

	where := "WHERE " + strings.Join(conds, " AND ")

	// Total count (without cursor filter for accuracy)
	countConds := conds
	if f.Cursor != "" {
		countConds = countConds[:len(countConds)-1]
	}
	countWhere := "WHERE " + strings.Join(countConds, " AND ")
	countArgs := args
	if f.Cursor != "" {
		countArgs = countArgs[:len(countArgs)-1]
	}
	var total int
	_ = s.pool.QueryRow(ctx, "SELECT COUNT(*) FROM logs "+countWhere, countArgs...).Scan(&total)

	if f.PageSize <= 0 {
		f.PageSize = 200
	}
	if f.PageSize > 2000 {
		f.PageSize = 2000
	}
	sortDir := "DESC"
	if f.Sort == "asc" {
		sortDir = "ASC"
	}

	q := fmt.Sprintf(`
		SELECT log_id, timestamp, level, service, host, message, trace_id, span_id, attributes
		FROM logs %s ORDER BY timestamp %s, log_id %s LIMIT $%d
	`, where, sortDir, sortDir, idx)
	args = append(args, f.PageSize+1) // Fetch one extra to determine next_cursor

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, 0, nil, fmt.Errorf("log.Query: %w", err)
	}
	defer rows.Close()

	var entries []models.LogEntry
	for rows.Next() {
		var e models.LogEntry
		if err := rows.Scan(&e.ID, &e.Timestamp, &e.Level, &e.Service, &e.Host,
			&e.Message, &e.TraceID, &e.SpanID, &e.Attributes); err != nil {
			return nil, 0, nil, fmt.Errorf("log.Query scan: %w", err)
		}
		entries = append(entries, e)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, nil, err
	}

	// Determine next cursor
	var nextCursor *string
	if len(entries) > f.PageSize {
		cursor := entries[f.PageSize-1].ID
		nextCursor = &cursor
		entries = entries[:f.PageSize]
	}

	return entries, total, nextCursor, nil
}

func (s *logStore) GetByID(ctx context.Context, id string) (*models.LogEntry, error) {
	query := `
		SELECT log_id, timestamp, level, service, host, message, trace_id, span_id, attributes
		FROM logs WHERE log_id = $1
	`
	e := &models.LogEntry{}
	err := s.pool.QueryRow(ctx, query, id).Scan(
		&e.ID, &e.Timestamp, &e.Level, &e.Service, &e.Host,
		&e.Message, &e.TraceID, &e.SpanID, &e.Attributes,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("log.GetByID: %w", err)
	}
	return e, nil
}

// GetAnomalies computes sliding-window error rate anomalies.
func (s *logStore) GetAnomalies(ctx context.Context, from, to time.Time, services []string, thresholdMult float64) ([]models.AnomalousWindow, error) {
	if thresholdMult <= 0 {
		thresholdMult = 3.0
	}

	svcFilter := ""
	args := []interface{}{from, to}
	idx := 3
	if len(services) > 0 {
		svcFilter = fmt.Sprintf("AND service = ANY($%d)", idx)
		args = append(args, services)
	}

	// 1-minute sliding windows, compute error rate vs baseline
	query := fmt.Sprintf(`
		WITH windows AS (
			SELECT
				date_trunc('minute', timestamp) AS window_start,
				date_trunc('minute', timestamp) + interval '1 minute' AS window_end,
				service,
				COUNT(*) FILTER (WHERE level IN ('ERROR','FATAL'))::float / GREATEST(COUNT(*), 1) AS error_rate,
				COUNT(*) AS total_logs
			FROM logs
			WHERE timestamp >= $1 AND timestamp <= $2 %s
			GROUP BY date_trunc('minute', timestamp), service
		),
		baselines AS (
			SELECT service, AVG(error_rate) AS baseline_rate, STDDEV(error_rate) AS stddev_rate
			FROM windows GROUP BY service
		)
		SELECT w.window_start, w.window_end, w.service, w.error_rate, b.baseline_rate,
		       CASE WHEN b.baseline_rate > 0 THEN w.error_rate / b.baseline_rate ELSE 0 END AS spike_factor
		FROM windows w
		JOIN baselines b ON w.service = b.service
		WHERE w.error_rate > b.baseline_rate + (COALESCE(b.stddev_rate, 0) * $%d)
		ORDER BY w.window_start DESC
	`, svcFilter, idx)
	if len(services) > 0 {
		idx++
	}
	args = append(args, thresholdMult)

	rows, err := s.pool.Query(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("log.GetAnomalies: %w", err)
	}
	defer rows.Close()

	var windows []models.AnomalousWindow
	for rows.Next() {
		var w models.AnomalousWindow
		if err := rows.Scan(&w.WindowStart, &w.WindowEnd, &w.Service,
			&w.ErrorRate, &w.BaselineRate, &w.SpikeFactor); err != nil {
			return nil, fmt.Errorf("log.GetAnomalies scan: %w", err)
		}
		w.SampleLogIDs = []string{} // Could query sample IDs per window if needed
		windows = append(windows, w)
	}
	return windows, rows.Err()
}

func (s *logStore) Ingest(ctx context.Context, e *models.LogEntry) error {
	query := `
		INSERT INTO logs (timestamp, level, service, host, message, trace_id, span_id, attributes)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
		RETURNING log_id
	`
	return s.pool.QueryRow(ctx, query,
		e.Timestamp, e.Level, e.Service, e.Host, e.Message, e.TraceID, e.SpanID, e.Attributes,
	).Scan(&e.ID)
}
