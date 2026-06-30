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

// TraceStore defines trace-related database operations.
type TraceStore interface {
	GetByTraceID(ctx context.Context, traceID string) ([]models.Span, error)
	Search(ctx context.Context, f TraceFilter) ([]models.TraceSummary, *string, error)
	BulkIngest(ctx context.Context, spans []models.Span) (int, error)
}

// TraceFilter holds query params for GET /traces.
type TraceFilter struct {
	From          time.Time
	To            time.Time
	Service       string
	Status        string
	MinDurationMs *int
	PageSize      int
	Cursor        string
}

type traceStore struct {
	pool *pgxpool.Pool
}

func NewTraceStore(pool *pgxpool.Pool) TraceStore {
	return &traceStore{pool: pool}
}

func (s *traceStore) GetByTraceID(ctx context.Context, traceID string) ([]models.Span, error) {
	query := `
		SELECT span_id, trace_id, parent_span_id, service, operation, start_time,
		       duration_ms, status, attributes, error_message
		FROM spans WHERE trace_id = $1
		ORDER BY start_time ASC
	`
	rows, err := s.pool.Query(ctx, query, traceID)
	if err != nil {
		return nil, fmt.Errorf("trace.GetByTraceID: %w", err)
	}
	defer rows.Close()

	var spans []models.Span
	for rows.Next() {
		var sp models.Span
		if err := rows.Scan(&sp.SpanID, &sp.TraceID, &sp.ParentSpanID, &sp.Service, &sp.Operation,
			&sp.StartTime, &sp.DurationMs, &sp.Status, &sp.Attributes, &sp.ErrorMessage); err != nil {
			return nil, fmt.Errorf("trace.GetByTraceID scan: %w", err)
		}
		spans = append(spans, sp)
	}
	if len(spans) == 0 {
		return nil, pgx.ErrNoRows
	}
	return spans, rows.Err()
}

func (s *traceStore) Search(ctx context.Context, f TraceFilter) ([]models.TraceSummary, *string, error) {
	var conds []string
	var args []interface{}
	idx := 1

	conds = append(conds, fmt.Sprintf("start_time >= $%d", idx))
	args = append(args, f.From)
	idx++
	conds = append(conds, fmt.Sprintf("start_time <= $%d", idx))
	args = append(args, f.To)
	idx++

	if f.Service != "" {
		conds = append(conds, fmt.Sprintf("service = $%d", idx))
		args = append(args, f.Service)
		idx++
	}
	if f.Status != "" {
		conds = append(conds, fmt.Sprintf("status = $%d", idx))
		args = append(args, f.Status)
		idx++
	}

	// Keyset cursor: use trace_id of last seen row
	if f.Cursor != "" {
		conds = append(conds, fmt.Sprintf("trace_id < $%d", idx))
		args = append(args, f.Cursor)
		idx++
	}

	where := "WHERE " + strings.Join(conds, " AND ")

	if f.PageSize <= 0 {
		f.PageSize = 50
	}

	// Aggregate by trace_id — HAVING for min_duration
	havingClause := ""
	if f.MinDurationMs != nil {
		havingClause = fmt.Sprintf("HAVING MAX(start_time + (duration_ms || 'ms')::interval) - MIN(start_time) >= ($%d || 'ms')::interval", idx)
		args = append(args, fmt.Sprintf("%d", *f.MinDurationMs))
		idx++
	}

	q := fmt.Sprintf(`
		SELECT trace_id,
		       (array_agg(service ORDER BY start_time ASC))[1] AS root_service,
		       CASE WHEN bool_or(status = 'error') THEN 'error'
		            WHEN bool_or(status = 'timeout') THEN 'timeout'
		            ELSE 'ok' END AS status,
		       EXTRACT(EPOCH FROM (MAX(start_time + (duration_ms || 'ms')::interval) - MIN(start_time))) * 1000 AS duration_ms,
		       MIN(start_time) AS started_at,
		       COUNT(*) AS span_count
		FROM spans %s
		GROUP BY trace_id %s
		ORDER BY started_at DESC
		LIMIT $%d
	`, where, havingClause, idx)
	args = append(args, f.PageSize+1)

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, nil, fmt.Errorf("trace.Search: %w", err)
	}
	defer rows.Close()

	var traces []models.TraceSummary
	for rows.Next() {
		var t models.TraceSummary
		if err := rows.Scan(&t.TraceID, &t.RootService, &t.Status, &t.DurationMs, &t.StartedAt, &t.SpanCount); err != nil {
			return nil, nil, fmt.Errorf("trace.Search scan: %w", err)
		}
		traces = append(traces, t)
	}

	var nextCursor *string
	if len(traces) > f.PageSize {
		c := traces[f.PageSize-1].TraceID
		nextCursor = &c
		traces = traces[:f.PageSize]
	}

	return traces, nextCursor, rows.Err()
}

// BulkIngest inserts spans using a pgx.Batch. Each span is upserted by span_id
// so the operation is idempotent (safe for at-least-once delivery).
func (s *traceStore) BulkIngest(ctx context.Context, spans []models.Span) (int, error) {
	if len(spans) == 0 {
		return 0, nil
	}

	batch := &pgx.Batch{}
	for _, sp := range spans {
		batch.Queue(`
			INSERT INTO spans (
				span_id, trace_id, parent_span_id, service, operation,
				start_time, duration_ms, status, attributes, error_message
			) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
			ON CONFLICT (span_id) DO UPDATE SET
				duration_ms    = EXCLUDED.duration_ms,
				status         = EXCLUDED.status,
				attributes     = EXCLUDED.attributes,
				error_message  = EXCLUDED.error_message
		`,
			sp.SpanID, sp.TraceID, sp.ParentSpanID, sp.Service, sp.Operation,
			sp.StartTime, sp.DurationMs, sp.Status, sp.Attributes, sp.ErrorMessage,
		)
	}

	br := s.pool.SendBatch(ctx, batch)
	defer br.Close()

	inserted := 0
	for range spans {
		_, err := br.Exec()
		if err != nil {
			return inserted, fmt.Errorf("trace.BulkIngest row %d: %w", inserted, err)
		}
		inserted++
	}
	return inserted, nil
}
