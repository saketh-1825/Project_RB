package main

import (
	"fmt"
	"os"
)

// Config holds all environment-driven configuration for the Go backend.
type Config struct {
	Port                    string
	Env                     string
	PostgresDSN             string
	RedisURL                string
	ChromaURL               string
	ChromaToken             string
	LangGraphURL            string
	SREInternalToken        string
	PrometheusWebhookSecret string
	DatadogWebhookSecret    string
	LogLevel                string
}

// LoadConfig reads environment variables and returns a validated Config.
func LoadConfig() (*Config, error) {
	cfg := &Config{
		Port:                    getEnv("PORT", "8080"),
		Env:                     getEnv("ENV", "development"),
		PostgresDSN:             os.Getenv("POSTGRES_DSN"),
		RedisURL:                getEnv("REDIS_URL", "redis://localhost:6379"),
		ChromaURL:               getEnv("CHROMA_URL", "http://localhost:8001"),
		ChromaToken:             os.Getenv("CHROMA_TOKEN"),
		LangGraphURL:            getEnv("LANGGRAPH_URL", "http://localhost:9000"),
		SREInternalToken:        os.Getenv("SRE_INTERNAL_TOKEN"),
		PrometheusWebhookSecret: os.Getenv("PROMETHEUS_WEBHOOK_SECRET"),
		DatadogWebhookSecret:    os.Getenv("DATADOG_WEBHOOK_SECRET"),
		LogLevel:                getEnv("LOG_LEVEL", "info"),
	}

	if cfg.PostgresDSN == "" {
		return nil, fmt.Errorf("config: POSTGRES_DSN is required")
	}
	if cfg.SREInternalToken == "" {
		return nil, fmt.Errorf("config: SRE_INTERNAL_TOKEN is required")
	}

	return cfg, nil
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
