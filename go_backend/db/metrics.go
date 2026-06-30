package db

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// MetricDataPoint is a single point in a batch ingest request.
type MetricDataPoint struct {
	MetricName string                 `json:"metric_name"`
	Timestamp  time.Time              `json:"timestamp"`
	Value      float64                `json:"value"`
	Labels     map[string]interface{} `json:"labels,omitempty"`
}

// MetricStore defines metric-related database operations.
type MetricStore interface {
	Query(ctx context.Context, metricName string, from, to time.Time, labels map[string]string, step string) (*models.MetricSeries, error)
	BatchQuery(ctx context.Context, queries []MetricQueryRequest) ([]models.MetricSeries, []MetricQueryError, error)
	Summary(ctx context.Context, metricName string, from, to time.Time, labels map[string]string) (*models.MetricSummary, error)
	Catalog(ctx context.Context) ([]models.MetricCatalogEntry, error)
	Ingest(ctx context.Context, metricName string, ts time.Time, value float64, labels map[string]interface{}) error
	BulkIngest(ctx context.Context, points []MetricDataPoint) (int, error)
}

// MetricQueryRequest mirrors a single query in the batch endpoint.
type MetricQueryRequest struct {
	MetricName string            `json:"metric_name"`
	From       time.Time         `json:"from"`
	To         time.Time         `json:"to"`
	Labels     map[string]string `json:"labels,omitempty"`
	Step       string            `json:"step,omitempty"`
}

// MetricQueryError reports a failed query in a batch.
type MetricQueryError struct {
	MetricName string `json:"metric_name"`
	Error      string `json:"error"`
}

type metricStore struct {
	pool *pgxpool.Pool
}

func NewMetricStore(pool *pgxpool.Pool) MetricStore {
	return &metricStore{pool: pool}
}

// stepToTrunc converts a step string (e.g. "30s", "1m", "5m", "1h") to a
// Postgres date_trunc precision string.  Falls back to "second".
func stepToTrunc(step string) string {
	step = strings.TrimSpace(strings.ToLower(step))
	switch {
	case strings.HasSuffix(step, "h"):
		return "hour"
	case strings.HasSuffix(step, "m"):
		// "1m" → minute, "30m" → minute
		return "minute"
	default:
		// "30s", "1s", "" → second
		return "second"
	}
}

// Query handles GET /metrics/query — fetch a time series for a named metric.
// Supports label JSONB filtering and step-based time bucketing.
func (s *metricStore) Query(ctx context.Context, metricName string, from, to time.Time, labels map[string]string, step string) (*models.MetricSeries, error) {
	var conds []string
	var args []interface{}
	idx := 1

	conds = append(conds, fmt.Sprintf("metric_name = $%d", idx))
	args = append(args, metricName)
	idx++

	conds = append(conds, fmt.Sprintf("timestamp >= $%d", idx))
	args = append(args, from)
	idx++

	conds = append(conds, fmt.Sprintf("timestamp <= $%d", idx))
	args = append(args, to)
	idx++

	// Label JSONB containment filter
	if len(labels) > 0 {
		labelsJSON, err := json.Marshal(labels)
		if err == nil {
			conds = append(conds, fmt.Sprintf("labels @> $%d::jsonb", idx))
			args = append(args, string(labelsJSON))
			idx++
		}
	}

	where := "WHERE " + strings.Join(conds, " AND ")

	var q string
	if step != "" && step != "0" {
		trunc := stepToTrunc(step)
		q = fmt.Sprintf(`
			SELECT date_trunc('%s', timestamp) AS ts_bucket,
			       AVG(value)                  AS value,
			       '{}'::jsonb                 AS labels
			FROM metric_data
			%s
			GROUP BY ts_bucket
			ORDER BY ts_bucket ASC
		`, trunc, where)
	} else {
		// No bucketing — return raw points
		q = fmt.Sprintf(`
			SELECT timestamp, value, labels
			FROM metric_data
			%s
			ORDER BY timestamp ASC
		`, where)
	}

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("metric.Query: %w", err)
	}
	defer rows.Close()

	series := &models.MetricSeries{MetricName: metricName}
	for rows.Next() {
		var pt models.MetricPoint
		var labelsJSON []byte
		if err := rows.Scan(&pt.Timestamp, &pt.Value, &labelsJSON); err != nil {
			return nil, fmt.Errorf("metric.Query scan: %w", err)
		}
		_ = json.Unmarshal(labelsJSON, &pt.Labels)
		series.DataPoints = append(series.DataPoints, pt)
	}

	// Fetch unit from catalog
	var unit string
	_ = s.pool.QueryRow(ctx, "SELECT unit FROM metric_catalog WHERE metric_name = $1", metricName).Scan(&unit)
	series.Unit = unit

	return series, rows.Err()
}

// batchResult carries the result of a single goroutine query.
type batchResult struct {
	idx    int
	series *models.MetricSeries
	err    *MetricQueryError
}

// BatchQuery handles POST /metrics/query/batch — parallel goroutine fan-out with partial success.
func (s *metricStore) BatchQuery(ctx context.Context, queries []MetricQueryRequest) ([]models.MetricSeries, []MetricQueryError, error) {
	results := make([]batchResult, len(queries))
	var wg sync.WaitGroup

	for i, q := range queries {
		wg.Add(1)
		go func(i int, q MetricQueryRequest) {
			defer wg.Done()
			series, err := s.Query(ctx, q.MetricName, q.From, q.To, q.Labels, q.Step)
			if err != nil {
				results[i] = batchResult{idx: i, err: &MetricQueryError{
					MetricName: q.MetricName,
					Error:      err.Error(),
				}}
			} else {
				results[i] = batchResult{idx: i, series: series}
			}
		}(i, q)
	}
	wg.Wait()

	var series []models.MetricSeries
	var errors []MetricQueryError
	for _, r := range results {
		if r.err != nil {
			errors = append(errors, *r.err)
		} else if r.series != nil {
			series = append(series, *r.series)
		}
	}
	return series, errors, nil
}

// Summary handles GET /metrics/summary — aggregate stats using Postgres PERCENTILE_CONT.
func (s *metricStore) Summary(ctx context.Context, metricName string, from, to time.Time, labels map[string]string) (*models.MetricSummary, error) {
	var conds []string
	var args []interface{}
	idx := 1

	conds = append(conds, fmt.Sprintf("metric_name = $%d", idx))
	args = append(args, metricName)
	idx++

	conds = append(conds, fmt.Sprintf("timestamp >= $%d", idx))
	args = append(args, from)
	idx++

	conds = append(conds, fmt.Sprintf("timestamp <= $%d", idx))
	args = append(args, to)
	idx++

	if len(labels) > 0 {
		labelsJSON, err := json.Marshal(labels)
		if err == nil {
			conds = append(conds, fmt.Sprintf("labels @> $%d::jsonb", idx))
			args = append(args, string(labelsJSON))
			idx++
		}
	}
	_ = idx

	where := "WHERE " + strings.Join(conds, " AND ")

	q := fmt.Sprintf(`
		SELECT MIN(value), MAX(value), AVG(value),
		       PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY value),
		       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value),
		       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value)
		FROM metric_data
		%s
	`, where)

	summary := &models.MetricSummary{MetricName: metricName}

	var minV, maxV, avgV, p50V, p95V, p99V *float64
	err := s.pool.QueryRow(ctx, q, args...).Scan(&minV, &maxV, &avgV, &p50V, &p95V, &p99V)
	if err != nil {
		return nil, fmt.Errorf("metric.Summary: %w", err)
	}
	if minV != nil {
		summary.Min = *minV
	}
	if maxV != nil {
		summary.Max = *maxV
	}
	if avgV != nil {
		summary.Avg = math.Round(*avgV*1000) / 1000
	}
	if p50V != nil {
		summary.P50 = *p50V
	}
	if p95V != nil {
		summary.P95 = *p95V
	}
	if p99V != nil {
		summary.P99 = *p99V
	}

	var unit string
	_ = s.pool.QueryRow(ctx, "SELECT unit FROM metric_catalog WHERE metric_name = $1", metricName).Scan(&unit)
	summary.Unit = unit

	return summary, nil
}

// Catalog handles GET /metrics/catalog — union of metric_catalog entries and
// distinct metric names actually present in metric_data.
func (s *metricStore) Catalog(ctx context.Context) ([]models.MetricCatalogEntry, error) {
	q := `
		SELECT
			COALESCE(mc.metric_name, d.metric_name) AS metric_name,
			COALESCE(mc.description, '')             AS description,
			COALESCE(mc.labels, ARRAY[]::TEXT[])     AS labels,
			COALESCE(mc.unit, '')                    AS unit
		FROM (
			SELECT DISTINCT metric_name FROM metric_data
		) d
		LEFT JOIN metric_catalog mc ON mc.metric_name = d.metric_name
		UNION
		SELECT metric_name, description, labels, unit
		FROM metric_catalog
		ORDER BY metric_name
	`
	rows, err := s.pool.Query(ctx, q)
	if err != nil {
		return nil, fmt.Errorf("metric.Catalog: %w", err)
	}
	defer rows.Close()

	seen := make(map[string]bool)
	var entries []models.MetricCatalogEntry
	for rows.Next() {
		var e models.MetricCatalogEntry
		if err := rows.Scan(&e.Name, &e.Description, &e.Labels, &e.Unit); err != nil {
			return nil, fmt.Errorf("metric.Catalog scan: %w", err)
		}
		if !seen[e.Name] {
			seen[e.Name] = true
			entries = append(entries, e)
		}
	}
	return entries, rows.Err()
}

// Ingest inserts a single metric data point and upserts its catalog entry.
func (s *metricStore) Ingest(ctx context.Context, metricName string, ts time.Time, value float64, labels map[string]interface{}) error {
	labelsJSON, _ := json.Marshal(labels)
	_, err := s.pool.Exec(ctx, `
		INSERT INTO metric_data (metric_name, timestamp, value, labels)
		VALUES ($1, $2, $3, $4)
	`, metricName, ts, value, labelsJSON)
	if err != nil {
		return err
	}
	// Upsert catalog entry so catalog is always populated
	_, _ = s.pool.Exec(ctx, `
		INSERT INTO metric_catalog (metric_name) VALUES ($1)
		ON CONFLICT (metric_name) DO NOTHING
	`, metricName)
	return nil
}

// BulkIngest inserts multiple metric data points using a pgx.Batch for a single
// round-trip. Also upserts distinct metric names into metric_catalog.
func (s *metricStore) BulkIngest(ctx context.Context, points []MetricDataPoint) (int, error) {
	if len(points) == 0 {
		return 0, nil
	}

	batch := &pgx.Batch{}
	for _, p := range points {
		labelsJSON, _ := json.Marshal(p.Labels)
		batch.Queue(`
			INSERT INTO metric_data (metric_name, timestamp, value, labels)
			VALUES ($1, $2, $3, $4)
		`, p.MetricName, p.Timestamp, p.Value, labelsJSON)
	}

	br := s.pool.SendBatch(ctx, batch)
	defer br.Close()

	inserted := 0
	for range points {
		_, err := br.Exec()
		if err != nil {
			return inserted, fmt.Errorf("metric.BulkIngest row %d: %w", inserted, err)
		}
		inserted++
	}

	// Upsert distinct metric names into catalog (best-effort, single round-trip)
	seen := make(map[string]bool)
	catBatch := &pgx.Batch{}
	for _, p := range points {
		if !seen[p.MetricName] {
			seen[p.MetricName] = true
			catBatch.Queue(`
				INSERT INTO metric_catalog (metric_name) VALUES ($1)
				ON CONFLICT (metric_name) DO NOTHING
			`, p.MetricName)
		}
	}
	catBR := s.pool.SendBatch(ctx, catBatch)
	defer catBR.Close()
	for range seen {
		_, _ = catBR.Exec()
	}

	return inserted, nil
}
