package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// ServiceStore defines service topology and health operations.
type ServiceStore interface {
	List(ctx context.Context) ([]models.ServiceNode, error)
	GetHealth(ctx context.Context, serviceID string) (*models.ServiceHealthDetail, error)
}

type serviceStore struct {
	pool *pgxpool.Pool
}

func NewServiceStore(pool *pgxpool.Pool) ServiceStore {
	return &serviceStore{pool: pool}
}

func (s *serviceStore) List(ctx context.Context) ([]models.ServiceNode, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT service_id, name, health, version, tags FROM services ORDER BY name
	`)
	if err != nil {
		return nil, fmt.Errorf("service.List: %w", err)
	}
	defer rows.Close()

	nodeMap := make(map[string]*models.ServiceNode)
	var nodes []models.ServiceNode
	for rows.Next() {
		var n models.ServiceNode
		if err := rows.Scan(&n.ServiceID, &n.Name, &n.Health, &n.Version, &n.Tags); err != nil {
			return nil, fmt.Errorf("service.List scan: %w", err)
		}
		n.Dependencies = []models.ServiceDependency{}
		nodes = append(nodes, n)
		nodeMap[n.ServiceID] = &nodes[len(nodes)-1]
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// Load dependencies
	depRows, err := s.pool.Query(ctx, `
		SELECT service_id, depends_on_id, call_type, avg_latency_ms, error_rate_percent
		FROM service_dependencies
	`)
	if err != nil {
		return nil, fmt.Errorf("service.List deps: %w", err)
	}
	defer depRows.Close()

	for depRows.Next() {
		var svcID, depID, callType string
		var latency, errRate float64
		if err := depRows.Scan(&svcID, &depID, &callType, &latency, &errRate); err != nil {
			return nil, fmt.Errorf("service.List deps scan: %w", err)
		}
		if node, ok := nodeMap[svcID]; ok {
			node.Dependencies = append(node.Dependencies, models.ServiceDependency{
				ServiceID:        depID,
				CallType:         callType,
				AvgLatencyMs:     latency,
				ErrorRatePercent: errRate,
			})
		}
	}

	return nodes, depRows.Err()
}

// GetHealth returns a health snapshot for a service. It first fetches the
// static service row for deploy info, then queries live metric_data for the
// last 1 minute to compute real-time error_rate and p99 latency.
// Falls back to the stored columns if no live metric data is available.
func (s *serviceStore) GetHealth(ctx context.Context, serviceID string) (*models.ServiceHealthDetail, error) {
	q := `
		SELECT service_id, health, error_rate_1m, p99_latency_ms, active_instances,
		       last_deploy_at, last_deploy_version, last_deploy_by
		FROM services WHERE service_id = $1
	`
	h := &models.ServiceHealthDetail{}
	var lastDeployAtRaw interface{}
	var deployVersion, deployBy *string

	err := s.pool.QueryRow(ctx, q, serviceID).Scan(
		&h.ServiceID, &h.Health, &h.ErrorRate1m, &h.P99LatencyMs, &h.ActiveInstances,
		&lastDeployAtRaw, &deployVersion, &deployBy,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("service.GetHealth: %w", err)
	}

	if deployVersion != nil && deployBy != nil {
		di := &models.DeployInfo{Version: *deployVersion, DeployedBy: *deployBy}
		h.LastDeploy = di
	}

	// ── Live error_rate from metric_data (last 1 minute) ────────────────────
	// We filter by labels->>'service' = service name. First resolve service name.
	var serviceName string
	_ = s.pool.QueryRow(ctx, "SELECT name FROM services WHERE service_id = $1", serviceID).Scan(&serviceName)

	if serviceName != "" {
		// Live error rate: AVG of error_rate metric over last 1 min for this service
		var liveErrorRate *float64
		errRateQ := `
			SELECT AVG(value)
			FROM metric_data
			WHERE metric_name = 'error_rate'
			  AND timestamp >= NOW() - INTERVAL '1 minute'
			  AND (labels->>'service' = $1 OR labels->>'service_name' = $1)
		`
		_ = s.pool.QueryRow(ctx, errRateQ, serviceName).Scan(&liveErrorRate)
		if liveErrorRate != nil {
			h.ErrorRate1m = *liveErrorRate
		}

		// Live p99 latency: PERCENTILE_CONT(0.99) over last 1 min for this service
		var liveP99 *float64
		p99Q := `
			SELECT PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY value)
			FROM metric_data
			WHERE metric_name IN ('http_request_duration_seconds', 'request_duration_ms', 'p99_latency_ms')
			  AND timestamp >= NOW() - INTERVAL '1 minute'
			  AND (labels->>'service' = $1 OR labels->>'service_name' = $1)
		`
		_ = s.pool.QueryRow(ctx, p99Q, serviceName).Scan(&liveP99)
		if liveP99 != nil {
			h.P99LatencyMs = *liveP99
		}
	}

	return h, nil
}
