package handlers

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"sre-copilot/db"
	"sre-copilot/models"
)

// ServiceHandler handles /api/v1/services endpoints.
type ServiceHandler struct {
	store db.ServiceStore
}

func NewServiceHandler(store db.ServiceStore) *ServiceHandler {
	return &ServiceHandler{store: store}
}

// List handles GET /services — return the full service dependency graph.
func (h *ServiceHandler) List(c *gin.Context) {
	services, err := h.store.List(c.Request.Context())
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if services == nil {
		services = []models.ServiceNode{}
	}
	c.JSON(http.StatusOK, gin.H{
		"services":     services,
		"generated_at": time.Now().UTC().Format(time.RFC3339),
	})
}

// GetHealth handles GET /services/:service_id/health — current health snapshot.
func (h *ServiceHandler) GetHealth(c *gin.Context) {
	health, err := h.store.GetHealth(c.Request.Context(), c.Param("service_id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, errResp(c, "INTERNAL_ERROR", err.Error()))
		return
	}
	if health == nil {
		c.JSON(http.StatusNotFound, errResp(c, "SERVICE_NOT_FOUND", "no service found with given ID"))
		return
	}
	c.JSON(http.StatusOK, health)
}
