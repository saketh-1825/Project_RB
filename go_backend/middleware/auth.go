// Package middleware provides Gin middleware for authentication, request tracing, and logging.
package middleware

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// BearerAuth validates the Authorization header against a shared internal token.
// Contract: "Authorization: Bearer <INTERNAL_SERVICE_TOKEN>"
func BearerAuth(token string) gin.HandlerFunc {
	return func(c *gin.Context) {
		auth := c.GetHeader("Authorization")
		if auth == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "UNAUTHORIZED",
					"message":    "missing Authorization header",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		parts := strings.SplitN(auth, " ", 2)
		if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") || parts[1] != token {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "UNAUTHORIZED",
					"message":    "invalid or missing service token",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		c.Next()
	}
}

// HMACAuth verifies the X-Webhook-Signature header using HMAC-SHA256.
// Contract: "X-Webhook-Signature: sha256=<signature>"
// The secret is the per-source webhook secret (e.g. PROMETHEUS_WEBHOOK_SECRET).
func HMACAuth(secret string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if secret == "" {
			// No secret configured — skip verification (dev mode).
			c.Next()
			return
		}

		sigHeader := c.GetHeader("X-Webhook-Signature")
		if sigHeader == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "WEBHOOK_BAD_SIGNATURE",
					"message":    "missing X-Webhook-Signature header",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		// Expect format: "sha256=<hex>"
		parts := strings.SplitN(sigHeader, "=", 2)
		if len(parts) != 2 || parts[0] != "sha256" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "WEBHOOK_BAD_SIGNATURE",
					"message":    "malformed signature header, expected sha256=<hex>",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		expectedSig, err := hex.DecodeString(parts[1])
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "WEBHOOK_BAD_SIGNATURE",
					"message":    "invalid hex in signature",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		// Read and buffer the body so downstream handlers can still read it.
		body, err := io.ReadAll(c.Request.Body)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{
				"error": gin.H{
					"code":    "BAD_REQUEST",
					"message": "failed to read request body",
				},
			})
			return
		}
		// Restore the body for downstream handlers.
		c.Request.Body = io.NopCloser(strings.NewReader(string(body)))

		mac := hmac.New(sha256.New, []byte(secret))
		mac.Write(body)
		computedSig := mac.Sum(nil)

		if !hmac.Equal(computedSig, expectedSig) {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": gin.H{
					"code":       "WEBHOOK_BAD_SIGNATURE",
					"message":    "HMAC signature mismatch",
					"request_id": c.GetString("request_id"),
				},
			})
			return
		}

		c.Next()
	}
}
