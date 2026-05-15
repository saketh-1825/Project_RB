#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  scripts/dev-ai.sh
#  AI/LangGraph developer entrypoint.
#  Starts: langgraph-service (real) + mock-go-backend + redis + postgres
#          + chroma + adminer
#  The mock-go-backend (WireMock) serves all endpoints from ./mocks/go-backend/
#  and returns realistic data so LangGraph can run full analysis cycles locally.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "❌  .env not found. Run: cp .env.example .env  then fill in your OPENROUTER_API_KEY."
  exit 1
fi

echo "🤖  Starting AI dev environment..."
echo "     Real services:  langgraph-service (:9000)"
echo "     Mock services:  mock-go-backend (:8080)  ← WireMock, loaded from ./mocks/go-backend/"
echo "     Infra:          redis postgres chroma"
echo "     Tools:          adminer (:8888)"
echo ""
echo "     WireMock admin UI: http://localhost:8080/__admin"
echo "     To add a new mock: drop a .json file in ./mocks/go-backend/mappings/ and restart."
echo ""

docker compose --profile ai up --build "$@"