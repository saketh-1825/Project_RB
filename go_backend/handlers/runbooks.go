package handlers

import (
	"context"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/rs/zerolog/log"

	"sre-copilot/clients"
	"sre-copilot/db"
	"sre-copilot/models"
)

// RunbookHandler handles /api/v1/runbooks endpoints.
type RunbookHandler struct {
	store    db.RunbookStore
	embedder *clients.EmbedderClient
}

func NewRunbookHandler(store db.RunbookStore, embedder *clients.EmbedderClient) *RunbookHandler {
	return &RunbookHandler{store: store, embedder: embedder}
}

// Search handles GET /runbooks/search — cosine similarity search when an embedder
// is configured; falls back to full-text ts_rank otherwise.
func (h *RunbookHandler) Search(c *gin.Context) {
	q := c.Query("q")
	if q == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "q query param is required"))
		return
	}
	topK, _ := strconv.Atoi(c.DefaultQuery("top_k", "5"))
	serviceFilter := c.Query("service_filter")
	tagFilter := c.Query("tag_filter")

	var runbooks []models.Runbook

	// Try vector search if embedder is available
	if h.embedder != nil && h.embedder.Enabled() {
		vec, err := h.embedder.Embed(c.Request.Context(), q)
		if err != nil {
			log.Warn().Err(err).Msg("embedder.Embed failed, falling back to FTS")
		} else if len(vec) > 0 {
			runbooks, err = h.store.SearchByVector(c.Request.Context(), vec, topK, serviceFilter, tagFilter)
			if err != nil {
				log.Warn().Err(err).Msg("SearchByVector failed, falling back to FTS")
				runbooks = nil
			}
		}
	}

	// FTS fallback
	if runbooks == nil {
		var err error
		runbooks, err = h.store.Search(c.Request.Context(), q, topK, serviceFilter, tagFilter)
		if err != nil {
			c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
			return
		}
	}

	if runbooks == nil {
		runbooks = []models.Runbook{}
	}
	c.JSON(http.StatusOK, gin.H{"runbooks": runbooks})
}

// GetByID handles GET /runbooks/:runbook_id.
func (h *RunbookHandler) GetByID(c *gin.Context) {
	rb, err := h.store.GetByID(c.Request.Context(), c.Param("runbook_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if rb == nil {
		c.JSON(http.StatusNotFound, errResp(c, "RUNBOOK_NOT_FOUND", "no runbook found with given ID"))
		return
	}
	c.JSON(http.StatusOK, rb)
}

// List handles GET /runbooks — list all runbooks with pagination.
func (h *RunbookHandler) List(c *gin.Context) {
	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	pageSize, _ := strconv.Atoi(c.DefaultQuery("page_size", "20"))

	runbooks, total, err := h.store.List(c.Request.Context(),
		c.Query("tag"), c.Query("service"), page, pageSize)
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if runbooks == nil {
		runbooks = []models.Runbook{}
	}
	c.JSON(http.StatusOK, gin.H{
		"runbooks": runbooks,
		"pagination": models.Pagination{
			Page:     page,
			PageSize: pageSize,
			Total:    total,
		},
	})
}

// Create handles POST /runbooks — store content, trigger async embedding pipeline.
func (h *RunbookHandler) Create(c *gin.Context) {
	var req struct {
		Title    string   `json:"title" binding:"required"`
		Content  string   `json:"content" binding:"required"`
		Tags     []string `json:"tags"`
		Services []string `json:"services"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", err.Error()))
		return
	}

	rb := &models.Runbook{
		Title:    req.Title,
		Content:  req.Content,
		Tags:     req.Tags,
		Services: req.Services,
	}
	if rb.Tags == nil {
		rb.Tags = []string{}
	}
	if rb.Services == nil {
		rb.Services = []string{}
	}

	if err := h.store.Create(c.Request.Context(), rb); err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}

	// Async embedding pipeline: generate vector and store it
	if h.embedder != nil && h.embedder.Enabled() {
		runbookID := rb.RunbookID
		content := req.Title + "\n\n" + req.Content
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()

			vec, err := h.embedder.Embed(ctx, content)
			if err != nil {
				log.Error().Err(err).Str("runbook_id", runbookID).Msg("embedding failed")
				return
			}
			if len(vec) == 0 {
				return
			}
			if err := h.store.UpdateEmbedding(ctx, runbookID, vec); err != nil {
				log.Error().Err(err).Str("runbook_id", runbookID).Msg("UpdateEmbedding failed")
				return
			}
			log.Info().Str("runbook_id", runbookID).Int("dims", len(vec)).Msg("embedding stored")
		}()
	}

	c.JSON(http.StatusCreated, rb)
}

// errResp is already defined in helpers.go — see that file.
var _ = fmt.Sprintf // keep fmt imported
