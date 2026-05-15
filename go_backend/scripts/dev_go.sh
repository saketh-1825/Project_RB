#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  scripts/dev-go.sh
#  Go backend developer entrypoint.
#  Starts: go-backend (real) + mock-langgraph + redis + postgres + chroma
#          + prometheus + alertmanager + simulator + adminer
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "❌  .env not found. Run: cp .env.example .env  then fill in your values."
  exit 1
fi

echo "🔧  Starting Go dev environment..."
echo "     Real services:  go-backend (:8080)"
echo "     Mock services:  mock-langgraph (:9000)"
echo "     Infra:          redis postgres chroma prometheus alertmanager"
echo "     Tools:          simulator adminer (:8888)"
echo ""

docker compose --profile go up --build "$@"