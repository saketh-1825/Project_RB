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

func (s *serviceStore) GetHealth(ctx context.Context, serviceID string) (*models.ServiceHealthDetail, error) {
	q := `
		SELECT service_id, health, error_rate_1m, p99_latency_ms, active_instances,
		       last_deploy_at, last_deploy_version, last_deploy_by
		FROM services WHERE service_id = $1
	`
	h := &models.ServiceHealthDetail{}
	var deployAt *interface{}
	var deployVersion, deployBy *string

	// Use individual nullable scans
	var lastDeployAtRaw interface{}
	err := s.pool.QueryRow(ctx, q, serviceID).Scan(
		&h.ServiceID, &h.Health, &h.ErrorRate1m, &h.P99LatencyMs, &h.ActiveInstances,
		&lastDeployAtRaw, &deployVersion, &deployBy,
	)
	_ = deployAt
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

	return h, nil
}
