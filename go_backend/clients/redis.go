package clients

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"
)

// RedisClient wraps go-redis for use across the backend.
type RedisClient struct {
	c *redis.Client
}

// NewRedisClient parses a Redis URL and returns a connected client.
// url format: redis://[:password@]host[:port][/db]
func NewRedisClient(url string) (*RedisClient, error) {
	opt, err := redis.ParseURL(url)
	if err != nil {
		return nil, fmt.Errorf("redis: parse URL: %w", err)
	}
	c := redis.NewClient(opt)

	// Validate connection
	if err := c.Ping(context.Background()).Err(); err != nil {
		return nil, fmt.Errorf("redis: ping failed: %w", err)
	}

	return &RedisClient{c: c}, nil
}

// Client returns the underlying *redis.Client for direct use.
func (r *RedisClient) Client() *redis.Client {
	return r.c
}

// Close closes the Redis connection.
func (r *RedisClient) Close() error {
	return r.c.Close()
}
