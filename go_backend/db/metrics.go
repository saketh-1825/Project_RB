package db

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// MetricStore defines metric-related database operations.
type MetricStore interface {
	Query(ctx context.Context, metricName string, from, to time.Time, labels map[string]string, step string) (*models.MetricSeries, error)
	BatchQuery(ctx context.Context, queries []MetricQueryRequest) ([]models.MetricSeries, []MetricQueryError, error)
	Summary(ctx context.Context, metricName string, from, to time.Time, labels map[string]string) (*models.MetricSummary, error)
	Catalog(ctx context.Context) ([]models.MetricCatalogEntry, error)
	Ingest(ctx context.Context, metricName string, ts time.Time, value float64, labels map[string]interface{}) error
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

func (s *metricStore) Query(ctx context.Context, metricName string, from, to time.Time, labels map[string]string, step string) (*models.MetricSeries, error) {
	q := `
		SELECT timestamp, value, labels
		FROM metric_data
		WHERE metric_name = $1 AND timestamp >= $2 AND timestamp <= $3
		ORDER BY timestamp ASC
	`
	rows, err := s.pool.Query(ctx, q, metricName, from, to)
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

func (s *metricStore) BatchQuery(ctx context.Context, queries []MetricQueryRequest) ([]models.MetricSeries, []MetricQueryError, error) {
	var results []models.MetricSeries
	var errors []MetricQueryError

	for _, q := range queries {
		series, err := s.Query(ctx, q.MetricName, q.From, q.To, q.Labels, q.Step)
		if err != nil {
			errors = append(errors, MetricQueryError{MetricName: q.MetricName, Error: err.Error()})
			continue
		}
		results = append(results, *series)
	}
	return results, errors, nil
}

func (s *metricStore) Summary(ctx context.Context, metricName string, from, to time.Time, labels map[string]string) (*models.MetricSummary, error) {
	q := `
		SELECT MIN(value), MAX(value), AVG(value),
		       PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY value),
		       PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY value),
		       PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value)
		FROM metric_data
		WHERE metric_name = $1 AND timestamp >= $2 AND timestamp <= $3
	`
	summary := &models.MetricSummary{MetricName: metricName}

	var minV, maxV, avgV, p50V, p95V, p99V *float64
	err := s.pool.QueryRow(ctx, q, metricName, from, to).Scan(&minV, &maxV, &avgV, &p50V, &p95V, &p99V)
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

func (s *metricStore) Catalog(ctx context.Context) ([]models.MetricCatalogEntry, error) {
	rows, err := s.pool.Query(ctx, "SELECT metric_name, description, labels, unit FROM metric_catalog ORDER BY metric_name")
	if err != nil {
		return nil, fmt.Errorf("metric.Catalog: %w", err)
	}
	defer rows.Close()

	var entries []models.MetricCatalogEntry
	for rows.Next() {
		var e models.MetricCatalogEntry
		if err := rows.Scan(&e.Name, &e.Description, &e.Labels, &e.Unit); err != nil {
			return nil, fmt.Errorf("metric.Catalog scan: %w", err)
		}
		entries = append(entries, e)
	}
	return entries, rows.Err()
}

func (s *metricStore) Ingest(ctx context.Context, metricName string, ts time.Time, value float64, labels map[string]interface{}) error {
	labelsJSON, _ := json.Marshal(labels)
	_, err := s.pool.Exec(ctx, `
		INSERT INTO metric_data (metric_name, timestamp, value, labels)
		VALUES ($1, $2, $3, $4)
	`, metricName, ts, value, labelsJSON)
	return err
}

// ensure sort is imported (used in percentile calculations above, but Postgres handles it)
var _ = sort.Float64s
