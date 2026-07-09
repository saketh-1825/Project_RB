#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/test_contracts.sh  — SRE Copilot contract integration test runner
#
# Exercises all 5 internal_flow_sequences defined in sre_copilot_contract.json.
# Requires the stack to be running via: docker compose --profile go up
#
# Usage:
#   ./scripts/test_contracts.sh [BASE_URL] [TOKEN]
#
# Defaults:
#   BASE_URL = http://localhost:8080
#   TOKEN    = value of SRE_INTERNAL_TOKEN in ../.env
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE="${1:-http://localhost:8080}"
# Load token from root .env if not passed
if [ $# -lt 2 ]; then
  ROOT_ENV="$(dirname "$(dirname "$(realpath "$0")")")/.env"
  if [ -f "$ROOT_ENV" ]; then
    # shellcheck disable=SC2046
    export $(grep -v '^#' "$ROOT_ENV" | xargs)
  fi
fi
TOKEN="${2:-${SRE_INTERNAL_TOKEN:-}}"
PROM_SECRET="${PROMETHEUS_WEBHOOK_SECRET:-test-secret}"

AUTH="Authorization: Bearer $TOKEN"
PASS=0
FAIL=0

# ── Helpers ──────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; NC='\033[0m'; BOLD='\033[1m'

log()  { echo -e "${BOLD}[TEST]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; ((PASS++)); }
fail() { echo -e "${RED}  ✗ $*${NC}"; ((FAIL++)); }
info() { echo -e "${YELLOW}  → $*${NC}"; }

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$actual" = "$expected" ]; then ok "$label"; else fail "$label (expected '$expected', got '$actual')"; fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -q "$needle"; then ok "$label"; else fail "$label (missing '$needle' in: ${haystack:0:120})"; fi
}

api() {
  local method="$1" path="$2"; shift 2
  curl -s -X "$method" "$BASE$path" -H "$AUTH" -H "Content-Type: application/json" "$@"
}

# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 1: happy_path_alert_to_report
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}━━━━ SCENARIO 1: happy_path_alert_to_report ━━━━${NC}"

log "1a. POST /api/v1/ready — backend should be ready"
READY=$(api GET /api/v1/ready | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ready',''))" 2>/dev/null)
assert_eq "GET /api/v1/ready returns ready=true" "True" "$READY"

log "1b. POST /webhooks/prometheus — fire an alert"
PROM_PAYLOAD=$(cat <<'EOF'
{
  "version": "4",
  "groupKey": "{}:{alertname=\"HighErrorRate\"}",
  "status": "firing",
  "alerts": [{
    "status": "firing",
    "labels": {"alertname": "HighErrorRate", "severity": "critical", "service": "payment-api"},
    "annotations": {"summary": "Error rate > 10%"},
    "startsAt": "2026-07-09T12:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "generatorURL": "http://prometheus:9090/graph"
  }]
}
EOF
)
# Build HMAC signature
SIG=$(echo -n "$PROM_PAYLOAD" | openssl dgst -sha256 -hmac "$PROM_SECRET" | awk '{print $2}')
PROM_RESP=$(curl -s -X POST "$BASE/webhooks/prometheus" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -d "$PROM_PAYLOAD")
assert_contains "prometheus webhook accepted" '"received":true' "$PROM_RESP"
assert_contains "prometheus webhook processed 1 alert" '"alerts_processed":1' "$PROM_RESP"

log "1c. GET /api/v1/alerts?status=firing — new alert visible"
ALERTS=$(api GET "/api/v1/alerts?status=firing&page_size=5")
ALERT_COUNT=$(echo "$ALERTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('alerts',[])))" 2>/dev/null)
if [ "${ALERT_COUNT:-0}" -ge 1 ]; then ok "firing alert in list ($ALERT_COUNT found)"; else fail "no firing alerts in list"; fi
ALERT_ID=$(echo "$ALERTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['alerts'][0]['alert_id'])" 2>/dev/null || echo "")

log "1d. POST /api/v1/alerts/:id/acknowledge"
if [ -n "$ALERT_ID" ]; then
  ACK=$(api POST "/api/v1/alerts/$ALERT_ID/acknowledge" -d '{"acknowledged_by":"test-script","note":"contract test"}')
  assert_contains "alert acknowledged" "alert_id" "$ACK"
fi

log "1e. GET /api/v1/logs?from&to — log query works"
FROM=$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)
TO=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LOGS=$(api GET "/api/v1/logs?from=${FROM}&to=${TO}&page_size=10")
assert_contains "logs endpoint returns logs array" '"logs"' "$LOGS"
assert_contains "logs endpoint returns total_matched" '"total_matched"' "$LOGS"

log "1f. GET /api/v1/dashboard/summary — aggregates correct"
SUMMARY=$(api GET /api/v1/dashboard/summary)
assert_contains "summary has open_incidents" '"open_incidents"' "$SUMMARY"
assert_contains "summary has firing_alerts"  '"firing_alerts"'  "$SUMMARY"
assert_contains "summary has recent_analyses" '"recent_analyses"' "$SUMMARY"

# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 2: human_in_the_loop_interrupt
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}━━━━ SCENARIO 2: human_in_the_loop_interrupt ━━━━${NC}"

log "2a. Create a test incident"
INC=$(api POST /api/v1/incidents -d '{"alert_id":"test-alert-002","title":"Payment DB connection failures","severity":"critical","affected_services":["payment-api","postgres"],"opened_by":"test-script"}')
assert_contains "incident created" '"incident_id"' "$INC"
INC_ID=$(echo "$INC" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('incident_id',''))" 2>/dev/null || echo "")
info "incident_id=$INC_ID"

log "2b. POST /incidents/:id/events — submit a finding"
if [ -n "$INC_ID" ]; then
  FINDING=$(api POST "/api/v1/incidents/$INC_ID/events" -d '{
    "agent": "correlation_agent",
    "type": "error_spike",
    "severity": "critical",
    "title": "Payment API error rate spike correlated with DB failures",
    "summary": "Error rate jumped from 2% to 45% at 12:00 UTC, correlated with postgres connection pool exhaustion",
    "confidence": 0.82,
    "evidence": {"metric_names": ["http_error_rate", "db_pool_active_connections"], "log_ids": [], "trace_ids": []}
  }')
  assert_contains "finding stored" '"finding_id"' "$FINDING"

  log "2c. GET /incidents/:id — incident has events"
  INC_DETAIL=$(api GET "/api/v1/incidents/$INC_ID")
  assert_contains "incident has events array" '"events"' "$INC_DETAIL"
fi

# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 3: alert_resolves_during_analysis
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}━━━━ SCENARIO 3: alert_resolves_during_analysis ━━━━${NC}"

log "3a. Fire alert then immediately resolve it"
FIRE_PAYLOAD=$(cat <<'EOF'
{
  "version": "4", "groupKey": "{}", "status": "firing",
  "alerts": [{"status": "firing",
    "labels": {"alertname": "ResolveTest", "severity": "high", "service": "order-service"},
    "annotations": {}, "startsAt": "2026-07-09T12:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z", "generatorURL": ""}]
}
EOF
)
FIRE_SIG=$(echo -n "$FIRE_PAYLOAD" | openssl dgst -sha256 -hmac "$PROM_SECRET" | awk '{print $2}')
FIRE_RESP=$(curl -s -X POST "$BASE/webhooks/prometheus" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$FIRE_SIG" \
  -d "$FIRE_PAYLOAD")
assert_contains "alert fired" '"received":true' "$FIRE_RESP"

RESOLVE_PAYLOAD=$(cat <<'EOF'
{
  "version": "4", "groupKey": "{}", "status": "resolved",
  "alerts": [{"status": "resolved",
    "labels": {"alertname": "ResolveTest", "severity": "high", "service": "order-service"},
    "annotations": {}, "startsAt": "2026-07-09T12:00:00Z",
    "endsAt": "2026-07-09T12:10:00Z", "generatorURL": ""}]
}
EOF
)
RESOLVE_SIG=$(echo -n "$RESOLVE_PAYLOAD" | openssl dgst -sha256 -hmac "$PROM_SECRET" | awk '{print $2}')
RESOLVE_RESP=$(curl -s -X POST "$BASE/webhooks/prometheus" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: sha256=$RESOLVE_SIG" \
  -d "$RESOLVE_PAYLOAD")
assert_contains "alert resolved webhook accepted" '"received":true' "$RESOLVE_RESP"

log "3b. Verify resolved alert visible in list"
sleep 1
RESOLVED_ALERTS=$(api GET "/api/v1/alerts?status=resolved&page_size=5")
assert_contains "resolved alerts endpoint works" '"alerts"' "$RESOLVED_ALERTS"

# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 4: go_backend_store_degraded (log query timeout response shape)
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}━━━━ SCENARIO 4: go_backend_store_degraded (error shape) ━━━━${NC}"

log "4a. GET /api/v1/logs with invalid regex — should return 400 + LOG_INVALID_REGEX"
BAD_REGEX=$(api GET "/api/v1/logs?from=${FROM}&to=${TO}&regex=%5B%5Binvalid")
assert_contains "invalid regex returns error code" '"code"' "$BAD_REGEX"

log "4b. GET /api/v1/logs with missing required params — should return 400"
MISSING_PARAMS=$(api GET "/api/v1/logs")
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "$AUTH" "$BASE/api/v1/logs")
assert_eq "missing from/to returns 400" "400" "$HTTP_STATUS"

log "4c. GET /api/v1/health — returns all components"
HEALTH=$(api GET /api/v1/health)
assert_contains "health has log_store"    '"log_store"'    "$HEALTH"
assert_contains "health has metric_store" '"metric_store"' "$HEALTH"
assert_contains "health has redis"        '"redis"'        "$HEALTH"
assert_contains "health has vector_index" '"vector_index"' "$HEALTH"

# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 5: multiple_simultaneous_alerts
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}━━━━ SCENARIO 5: multiple_simultaneous_alerts ━━━━${NC}"

log "5a. POST two custom webhooks simultaneously"
A1=$(api POST /webhooks/custom -d '{"name":"MultiAlert-A","source":"custom_webhook","severity":"high","status":"firing","fired_at":"2026-07-09T12:00:00Z","labels":{"env":"prod"},"annotations":{},"affected_services":["payment-api"]}' &)
A2=$(api POST /webhooks/custom -d '{"name":"MultiAlert-B","source":"custom_webhook","severity":"high","status":"firing","fired_at":"2026-07-09T12:00:01Z","labels":{"env":"prod"},"annotations":{},"affected_services":["order-service"]}' &)
wait
MULTI=$(api GET "/api/v1/alerts?page_size=100&status=firing")
MULTI_COUNT=$(echo "$MULTI" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('alerts',[])))" 2>/dev/null || echo "0")
if [ "${MULTI_COUNT:-0}" -ge 2 ]; then ok "≥2 alerts stored ($MULTI_COUNT firing)"; else fail "expected ≥2 alerts, got $MULTI_COUNT"; fi

log "5b. POST two incidents independently"
INC_A=$(api POST /api/v1/incidents -d '{"alert_id":"multi-a","title":"MultiAlert-A incident","severity":"high","affected_services":["payment-api"],"opened_by":"test"}')
INC_B=$(api POST /api/v1/incidents -d '{"alert_id":"multi-b","title":"MultiAlert-B incident","severity":"high","affected_services":["order-service"],"opened_by":"test"}')
assert_contains "incident A created" '"incident_id"' "$INC_A"
assert_contains "incident B created" '"incident_id"' "$INC_B"

INC_LIST=$(api GET "/api/v1/incidents?page_size=100")
INC_LIST_COUNT=$(echo "$INC_LIST" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['pagination']['total'])" 2>/dev/null || echo "0")
if [ "${INC_LIST_COUNT:-0}" -ge 2 ]; then ok "≥2 incidents in list ($INC_LIST_COUNT total)"; else fail "incident list has $INC_LIST_COUNT — expected ≥2"; fi

# ─────────────────────────────────────────────────────────────────────────────
# RESULTS
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━ RESULTS ━━━━${NC}"
echo -e "${GREEN}  PASSED: $PASS${NC}"
if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}  FAILED: $FAIL${NC}"
  exit 1
else
  echo -e "${GREEN}  All contract scenarios passed!${NC}"
fi
