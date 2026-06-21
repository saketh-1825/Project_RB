package db

import (
	"context"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"sre-copilot/models"
)

// RunbookStore defines runbook-related database operations.
type RunbookStore interface {
	Search(ctx context.Context, query string, topK int, serviceFilter, tagFilter string) ([]models.Runbook, error)
	GetByID(ctx context.Context, id string) (*models.Runbook, error)
	List(ctx context.Context, tag, service string, page, pageSize int) ([]models.Runbook, int, error)
	Create(ctx context.Context, r *models.Runbook) error
}

type runbookStore struct {
	pool *pgxpool.Pool
}

func NewRunbookStore(pool *pgxpool.Pool) RunbookStore {
	return &runbookStore{pool: pool}
}

// Search performs a keyword-based search over runbooks.
// Note: For true semantic/vector search, this would use pgvector cosine similarity
// against the embedding column. For now, we use full-text search on title + content.
func (s *runbookStore) Search(ctx context.Context, query string, topK int, serviceFilter, tagFilter string) ([]models.Runbook, error) {
	if topK <= 0 {
		topK = 5
	}
	if topK > 20 {
		topK = 20
	}

	var conds []string
	var args []interface{}
	idx := 1

	// Full-text search on title and content
	conds = append(conds, fmt.Sprintf(
		"(to_tsvector('english', title || ' ' || content) @@ plainto_tsquery('english', $%d))", idx))
	args = append(args, query)
	idx++

	if serviceFilter != "" {
		conds = append(conds, fmt.Sprintf("$%d = ANY(services)", idx))
		args = append(args, serviceFilter)
		idx++
	}
	if tagFilter != "" {
		tags := strings.Split(tagFilter, ",")
		for _, t := range tags {
			conds = append(conds, fmt.Sprintf("$%d = ANY(tags)", idx))
			args = append(args, strings.TrimSpace(t))
			idx++
		}
	}

	where := "WHERE " + strings.Join(conds, " AND ")

	q := fmt.Sprintf(`
		SELECT runbook_id, title, tags, services, content, last_updated,
		       ts_rank(to_tsvector('english', title || ' ' || content), plainto_tsquery('english', $1)) AS score
		FROM runbooks %s
		ORDER BY score DESC
		LIMIT $%d
	`, where, idx)
	args = append(args, topK)

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("runbook.Search: %w", err)
	}
	defer rows.Close()

	var runbooks []models.Runbook
	for rows.Next() {
		var r models.Runbook
		var score float64
		if err := rows.Scan(&r.RunbookID, &r.Title, &r.Tags, &r.Services,
			&r.Content, &r.LastUpdated, &score); err != nil {
			return nil, fmt.Errorf("runbook.Search scan: %w", err)
		}
		r.SimilarityScore = &score
		runbooks = append(runbooks, r)
	}
	return runbooks, rows.Err()
}

func (s *runbookStore) GetByID(ctx context.Context, id string) (*models.Runbook, error) {
	q := `SELECT runbook_id, title, tags, services, content, last_updated FROM runbooks WHERE runbook_id = $1`
	r := &models.Runbook{}
	err := s.pool.QueryRow(ctx, q, id).Scan(&r.RunbookID, &r.Title, &r.Tags, &r.Services, &r.Content, &r.LastUpdated)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("runbook.GetByID: %w", err)
	}
	return r, nil
}

func (s *runbookStore) List(ctx context.Context, tag, service string, page, pageSize int) ([]models.Runbook, int, error) {
	var conds []string
	var args []interface{}
	idx := 1

	if tag != "" {
		conds = append(conds, fmt.Sprintf("$%d = ANY(tags)", idx))
		args = append(args, tag)
		idx++
	}
	if service != "" {
		conds = append(conds, fmt.Sprintf("$%d = ANY(services)", idx))
		args = append(args, service)
		idx++
	}

	where := ""
	if len(conds) > 0 {
		where = "WHERE " + strings.Join(conds, " AND ")
	}

	var total int
	_ = s.pool.QueryRow(ctx, "SELECT COUNT(*) FROM runbooks "+where, args...).Scan(&total)

	if pageSize <= 0 {
		pageSize = 20
	}
	if page <= 0 {
		page = 1
	}
	offset := (page - 1) * pageSize

	q := fmt.Sprintf(`
		SELECT runbook_id, title, tags, services, content, last_updated
		FROM runbooks %s ORDER BY last_updated DESC LIMIT $%d OFFSET $%d
	`, where, idx, idx+1)
	args = append(args, pageSize, offset)

	rows, err := s.pool.Query(ctx, q, args...)
	if err != nil {
		return nil, 0, fmt.Errorf("runbook.List: %w", err)
	}
	defer rows.Close()

	var runbooks []models.Runbook
	for rows.Next() {
		var r models.Runbook
		if err := rows.Scan(&r.RunbookID, &r.Title, &r.Tags, &r.Services, &r.Content, &r.LastUpdated); err != nil {
			return nil, 0, fmt.Errorf("runbook.List scan: %w", err)
		}
		runbooks = append(runbooks, r)
	}
	return runbooks, total, rows.Err()
}

func (s *runbookStore) Create(ctx context.Context, r *models.Runbook) error {
	q := `
		INSERT INTO runbooks (title, tags, services, content)
		VALUES ($1, $2, $3, $4)
		RETURNING runbook_id, last_updated
	`
	return s.pool.QueryRow(ctx, q, r.Title, r.Tags, r.Services, r.Content).Scan(&r.RunbookID, &r.LastUpdated)
}
