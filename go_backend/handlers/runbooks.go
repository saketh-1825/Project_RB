package handlers

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// RunbookHandler handles /api/v1/runbooks endpoints.
type RunbookHandler struct {
	store db.RunbookStore
}

func NewRunbookHandler(store db.RunbookStore) *RunbookHandler {
	return &RunbookHandler{store: store}
}

// Search handles GET /runbooks/search — semantic search over runbooks.
func (h *RunbookHandler) Search(c *gin.Context) {
	q := c.Query("q")
	if q == "" {
		c.JSON(http.StatusBadRequest, errResp(c, "BAD_REQUEST", "q query param is required"))
		return
	}
	topK, _ := strconv.Atoi(c.DefaultQuery("top_k", "5"))

	runbooks, err := h.store.Search(c.Request.Context(), q, topK,
		c.Query("service_filter"), c.Query("tag_filter"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
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

// Create handles POST /runbooks — ingest a new runbook.
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
	c.JSON(http.StatusCreated, rb)
}
