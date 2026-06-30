package clients

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// EmbedderClient calls an external embedding service to produce vector
// representations of text for pgvector cosine similarity search.
// If EmbedderURL is empty, all methods are no-ops (returns nil, nil).
type EmbedderClient struct {
	url    string
	client *http.Client
}

// NewEmbedderClient creates an EmbedderClient. url may be empty to disable embedding.
func NewEmbedderClient(url string) *EmbedderClient {
	return &EmbedderClient{
		url: url,
		client: &http.Client{
			Timeout: 15 * time.Second,
		},
	}
}

// Enabled returns true when an embedder URL is configured.
func (e *EmbedderClient) Enabled() bool {
	return e.url != ""
}

// Embed sends text to the embedding service and returns the float32 vector.
// Returns (nil, nil) when no embedder URL is configured.
func (e *EmbedderClient) Embed(ctx context.Context, text string) ([]float32, error) {
	if e.url == "" {
		return nil, nil
	}

	body, _ := json.Marshal(map[string]string{"text": text})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, e.url+"/embed", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("embedder: build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("embedder: request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("embedder: status %d: %s", resp.StatusCode, string(raw))
	}

	var result struct {
		Embedding []float32 `json:"embedding"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("embedder: decode response: %w", err)
	}
	if len(result.Embedding) == 0 {
		return nil, fmt.Errorf("embedder: empty embedding returned")
	}
	return result.Embedding, nil
}
