package clients

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/rs/zerolog/log"
)

const (
	retryQueueKey = "sre:langgraph:retry"
	maxAttempts   = 10
)

// retryItem is the envelope persisted in Redis.
type retryItem struct {
	Req     TriggerAnalysisRequest `json:"req"`
	Attempt int                    `json:"attempt"`
}

// RetryQueue is a Redis-backed queue for LangGraph trigger retries with
// exponential backoff. It is safe to use from multiple goroutines.
type RetryQueue struct {
	rdb *redis.Client
}

// NewRetryQueue creates a RetryQueue using the provided Redis client.
func NewRetryQueue(rdb *redis.Client) *RetryQueue {
	return &RetryQueue{rdb: rdb}
}

// Enqueue pushes a TriggerAnalysisRequest onto the retry queue at the given
// attempt number. Call with attempt=0 for first-time failures.
func (q *RetryQueue) Enqueue(ctx context.Context, req TriggerAnalysisRequest, attempt int) error {
	item := retryItem{Req: req, Attempt: attempt}
	data, err := json.Marshal(item)
	if err != nil {
		return fmt.Errorf("retryQueue.Enqueue marshal: %w", err)
	}
	return q.rdb.RPush(ctx, retryQueueKey, data).Err()
}

// StartWorker runs a blocking-pop loop in the current goroutine. Call as `go q.StartWorker(ctx, lg)`.
// It dequeues items, attempts TriggerAnalysis, and re-enqueues with exponential
// backoff up to maxAttempts before discarding.
func (q *RetryQueue) StartWorker(ctx context.Context, langGraph *LangGraphClient) {
	log.Info().Msg("retryQueue: worker started")

	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("retryQueue: worker stopped")
			return
		default:
		}

		// BLPOP blocks up to 5s so ctx cancellation is checked promptly.
		results, err := q.rdb.BLPop(ctx, 5*time.Second, retryQueueKey).Result()
		if err != nil {
			if err == redis.Nil || err == context.Canceled || err == context.DeadlineExceeded {
				continue
			}
			log.Error().Err(err).Msg("retryQueue: BLPop error")
			time.Sleep(2 * time.Second)
			continue
		}

		// results[0] = key name, results[1] = value
		if len(results) < 2 {
			continue
		}

		var item retryItem
		if err := json.Unmarshal([]byte(results[1]), &item); err != nil {
			log.Error().Err(err).Msg("retryQueue: unmarshal item failed, discarding")
			continue
		}

		if item.Attempt >= maxAttempts {
			log.Error().
				Str("alert_id", item.Req.AlertID).
				Int("attempt", item.Attempt).
				Msg("retryQueue: max attempts reached, discarding")
			continue
		}

		// Exponential backoff: 2^attempt seconds, capped at 5 minutes.
		backoff := time.Duration(math.Min(
			math.Pow(2, float64(item.Attempt)),
			300,
		)) * time.Second

		if backoff > 0 {
			log.Info().
				Str("alert_id", item.Req.AlertID).
				Int("attempt", item.Attempt).
				Dur("backoff", backoff).
				Msg("retryQueue: waiting before retry")

			select {
			case <-ctx.Done():
				// Re-enqueue before exit so we don't lose the item
				_ = q.Enqueue(context.Background(), item.Req, item.Attempt)
				return
			case <-time.After(backoff):
			}
		}

		log.Info().
			Str("alert_id", item.Req.AlertID).
			Int("attempt", item.Attempt+1).
			Msg("retryQueue: attempting TriggerAnalysis")

		_, err = langGraph.TriggerAnalysis(ctx, item.Req)
		if err != nil {
			log.Warn().
				Err(err).
				Str("alert_id", item.Req.AlertID).
				Int("attempt", item.Attempt+1).
				Msg("retryQueue: TriggerAnalysis failed, re-enqueueing")
			_ = q.Enqueue(ctx, item.Req, item.Attempt+1)
		} else {
			log.Info().
				Str("alert_id", item.Req.AlertID).
				Int("attempt", item.Attempt+1).
				Msg("retryQueue: TriggerAnalysis succeeded")
		}
	}
}
