#!/usr/bin/env bash
# S8 smoke — one-command end-to-end acceptance for the realtime pipeline.
#
#   tests/integration/smoke.sh [--mock|--model]
#
#   --mock (default)  `make up-mock`-style stack: the jev-mock server answers
#                     the flagged tickets, the alert-receiver records the
#                     deliveries. No GPU / no model download — this is what
#                     the CI `smoke` job runs.
#   --model           same stack plus the real Laya model container
#                     (compose profile `model`, TheRock ROCm wheels) answering
#                     the flagged tickets. Local-only: needs the S0 .venv
#                     model runtime + the GPU.
#
# Phases: clean slate (`down -v`) → healthchecks → steady 2000 @ 100 ev/s
# (seed 7) → SQL asserts (counts, 95/5 split ±3%, degraded=0, DLQ=0, data
# quality, alert joins) → scam-spike burst (`--profile demo`, seed 11 — a
# different seed than steady ON PURPOSE: the generator derives event ids from
# the seed, so a repeated seed would collide with the steady phase in
# tickets_raw) → degraded=0 + drain ≤60 s → notifier joins (delivered ⊆
# receiver receipts, rate limit ≤10/min) → CDC: 2 mutations land ≤10 s and
# the CH current-state diff vs Postgres is empty.
#
# Prints a PASS/FAIL table; exits non-zero on any FAIL.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

MODE="${1:---mock}"
case "$MODE" in
  --mock)
    UP_ARGS=(-f docker-compose.yaml -f compose.mock.yaml up -d --wait)
    DOWN_ARGS=(-f docker-compose.yaml -f compose.mock.yaml --profile model down -v)
    ;;
  --model)
    UP_ARGS=(-f docker-compose.yaml -f compose.mock.yaml -f tests/integration/chaos.compose.yaml --profile model up -d --wait)
    DOWN_ARGS=(-f docker-compose.yaml -f compose.mock.yaml --profile model down -v)
    ;;
  *)
    echo "usage: $0 [--mock|--model]" >&2
    exit 2
    ;;
esac

# ---------- helpers ----------------------------------------------------------

# ClickHouse query (auth comes from the container's own environment).
chq() {
  docker compose exec -T clickhouse sh -c \
    'exec clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" -q "$1"' \
    -- "$1"
}

# Summed high-watermark over a topic's partitions (message count).
topic_count() {
  docker compose exec -T redpanda rpk topic describe "$1" -p 2>/dev/null \
    | awk 'NR > 1 { s += $NF } END { print s + 0 }'
}

# Consumer-group TOTAL-LAG (999999 if rpk fails or the group is gone).
group_lag() {
  local lag
  lag=$(docker compose exec -T redpanda rpk group describe "$1" 2>/dev/null \
    | awk '/^TOTAL-LAG/ { print $2 }' | tail -1)
  echo "${lag:-999999}"
}

# Poll every group in GROUPS until TOTAL-LAG < threshold; prints elapsed s.
# Prints TIMEOUT (and returns 1) if the deadline passes.
wait_drain() {
  local threshold="$1" timeout="$2" start now lag ok
  start=$(date +%s)
  while :; do
    now=$(date +%s)
    if ((now - start >= timeout)); then
      echo "TIMEOUT"
      return 1
    fi
    ok=1
    for g in "${GROUPS[@]}"; do
      lag=$(group_lag "$g")
      if ((lag >= threshold)); then ok=0; break; fi
    done
    if ((ok)); then
      echo $((now - start))
      return 0
    fi
    sleep 2
  done
}

# PASS/FAIL ledger (awk for the numeric comparisons — bc is not in CI images).
RESULTS=()
FAILURES=0
pass() { RESULTS+=("PASS|$1|$2"); }
fail() { RESULTS+=("FAIL|$1|$2"); FAILURES=$((FAILURES + 1)); }
check_eq() { # name expected actual
  if [[ "$2" == "$3" ]]; then pass "$1" "$3"; else fail "$1" "expected $2, got $3"; fi
}
check_ge() { # name actual min
  if awk -v a="$2" -v b="$3" 'BEGIN { exit !(a >= b) }'; then pass "$1" "$2"; else fail "$1" "$2 < $3"; fi
}
check_le() { # name actual max
  if awk -v a="$2" -v b="$3" 'BEGIN { exit !(a <= b) }'; then pass "$1" "$2"; else fail "$1" "$2 > $3"; fi
}
check_between() { # name actual min max
  if awk -v a="$2" -v lo="$3" -v hi="$4" 'BEGIN { exit !(a >= lo && a <= hi) }'; then
    pass "$1" "$2"
  else
    fail "$1" "$2 outside [$3, $4]"
  fi
}

GROUPS=(udp-ingest udp-decide udp-notify-fast udp-notify-model)

# Load sizing (the acceptance defaults are also what `make demo` runs). CI
# halves the load and skips the burst — mock server, no GPU, no model
# download: SMOKE_STEADY_COUNT=1000 SMOKE_STEADY_RATE=50 SMOKE_BURST_COUNT=0.
STEADY_COUNT="${SMOKE_STEADY_COUNT:-2000}"
STEADY_RATE="${SMOKE_STEADY_RATE:-100}"
STEADY_SEED="${SMOKE_STEADY_SEED:-7}"
BURST_COUNT="${SMOKE_BURST_COUNT:-18000}"
BURST_SEED="${SMOKE_BURST_SEED:-11}"

echo "=== S8 smoke (mode: $MODE, steady ${STEADY_COUNT} @ ${STEADY_RATE}/s, burst ${BURST_COUNT}) ==="
echo "--- 1/7 clean slate (docker compose down -v)"
docker compose "${DOWN_ARGS[@]}" >/dev/null 2>&1 || true

echo "--- 2/7 stack up"
docker compose "${UP_ARGS[@]}"

echo "--- 3/7 healthchecks"
H_FAIL=0
hp() { # name url
  if curl -fsS --max-time 5 "$2" >/dev/null 2>&1; then pass "health:$1" "ok"; else fail "health:$1" "unreachable: $2"; H_FAIL=1; fi
}
hex() { # name service port  (in-network services without host ports)
  if docker compose exec -T "$2" python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:$3/healthz', timeout=3)" 2>/dev/null; then
    pass "health:$1" "ok"
  else
    fail "health:$1" "container healthz failed"
    H_FAIL=1
  fi
}
hp "clickhouse" "http://127.0.0.1:8123/ping"
hp "console(8085)" "http://127.0.0.1:8085/"
hp "ingest :4196" "http://127.0.0.1:4196/ping"
hp "decide  :4197" "http://127.0.0.1:4197/ping"
hp "notify  :4198" "http://127.0.0.1:4198/ping"
hp "cdc     :4199" "http://127.0.0.1:4199/ping"
hex "alert-receiver" alert-receiver 8082
hex "jev-mock" jev-mock 8081
if [[ "$MODE" == "--model" ]]; then
  hp "model   :8082" "http://127.0.0.1:8082/healthz"
fi
if docker compose exec -T redpanda rpk cluster health >/dev/null; then
  pass "health:redpanda" "cluster healthy"
else
  fail "health:redpanda" "rpk cluster health failed"
  H_FAIL=1
fi
if ((H_FAIL)); then
  echo "healthchecks failed — aborting before load"
  exit 1
fi

echo "--- 4/7 steady: ${STEADY_COUNT} @ ${STEADY_RATE} ev/s (seed ${STEADY_SEED})"
STEADY_T0=$(date +%s)
uv run udp-generate --profile steady --rate "$STEADY_RATE" --count "$STEADY_COUNT" --seed "$STEADY_SEED" >/dev/null
DRAIN_S=$(wait_drain 5 180) || { fail "steady:drain" "groups never drained"; DRAIN_S=999; }
sleep 5 # settle the sql_insert batcher flush waves before the exact-count asserts
echo "steady wall: $(( $(date +%s) - STEADY_T0 ))s (drain poll: ${DRAIN_S}s)"

check_eq "steady:tickets_raw distinct" "$STEADY_COUNT" \
  "$(chq "SELECT uniqExact(event_id) FROM udp.tickets_raw")"
check_eq "steady:DLQ rows" "0" "$(topic_count tickets_dlq)"
check_eq "steady:degraded decisions" "0" "$(chq 'SELECT count() FROM udp.model_decisions WHERE degraded')"
FAST_N=$(chq "SELECT uniqExact(event_id) FROM udp.fast_decisions")
MODEL_N=$(chq "SELECT uniqExact(event_id) FROM udp.model_decisions")
check_between "steady:95/5 fast share" \
  "$(awk -v f="$FAST_N" -v m="$MODEL_N" 'BEGIN { if (f + m > 0) printf "%.3f", f / (f + m); else print 0 }')" \
  "0.92" "0.98"
check_eq "steady:null event_id rows" "0" \
  "$(chq "SELECT count() FROM udp.tickets_raw WHERE event_id = ''")"
DUP_PCT=$(chq "SELECT round(100 * (count() - uniqExact(event_id)) / count(), 3) FROM udp.tickets_raw")
check_le "steady:dup event_id %" "$DUP_PCT" "1"
check_eq "steady:decision coverage misses (non-DLQ)" "0" \
  "$(chq "SELECT count() FROM (SELECT DISTINCT event_id FROM udp.tickets_raw) t
          LEFT JOIN (SELECT DISTINCT event_id FROM udp.fast_decisions
                     UNION ALL SELECT DISTINCT event_id FROM udp.model_decisions) d USING (event_id)
          WHERE d.event_id = ''")"
check_eq "alerts:dedupe (rows == distinct decision_id)" "0" \
  "$(chq 'SELECT count() - uniqExact(decision_id) FROM udp.alerts')"
check_eq "alerts:join model alerts w/o model_decisions row" "0" \
  "$(chq "SELECT count() FROM udp.alerts a
          LEFT JOIN (SELECT DISTINCT decision_id FROM udp.model_decisions) m ON a.decision_id = m.decision_id
          WHERE a.decision_id NOT LIKE 'fast:%' AND m.decision_id = ''")"
check_eq "alerts:join fast alerts w/o fast_decisions row" "0" \
  "$(chq "SELECT count() FROM udp.alerts a
          WHERE a.decision_id LIKE 'fast:%'
            AND a.event_id NOT IN (SELECT DISTINCT event_id FROM udp.fast_decisions)")"

if [[ "$BURST_COUNT" != "0" ]]; then
  echo "--- 5/7 burst demo: --profile demo (${BURST_COUNT} events, seed ${BURST_SEED})"
  BURST_T0=$(date +%s)
  uv run udp-generate --profile demo --count "$BURST_COUNT" --seed "$BURST_SEED" >/dev/null
  BURST_END=$(date +%s)
  BURST_DRAIN_S=$(wait_drain 5 60) || { fail "burst:drain ≤60s after end" "still lagging 60s after burst end"; BURST_DRAIN_S=999; }
  sleep 5 # settle batcher flush waves before the total-count assert
  echo "burst window: $(( BURST_END - BURST_T0 ))s; drain: ${BURST_DRAIN_S}s"
  check_le "burst:drain ≤60s after end" "$BURST_DRAIN_S" "60"
  check_eq "burst:degraded decisions" "0" "$(chq 'SELECT count() FROM udp.model_decisions WHERE degraded')"
  check_eq "burst:tickets_raw total distinct" "$((STEADY_COUNT + BURST_COUNT))" \
    "$(chq "SELECT uniqExact(event_id) FROM udp.tickets_raw")"
else
  echo "--- 5/7 burst demo: SKIPPED (SMOKE_BURST_COUNT=0)"
fi

echo "--- 6/7 notifier joins (delivered ⊆ receiver, rate limit)"
ALERTS_TOTAL=$(chq 'SELECT count() FROM udp.alerts')
ALERTS_DELIVERED=$(chq 'SELECT count() FROM udp.alerts WHERE NOT suppressed')
RECEIPTS_JSON=$(docker compose exec -T alert-receiver python -c \
  'import json, urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://localhost:8082/alerts"))["alerts"]))')
RECEIPT_N=$(echo "$RECEIPTS_JSON" | uv run python -c 'import json, sys; print(len(json.load(sys.stdin)))')
MISSING=$(RECEIPTS="$RECEIPTS_JSON" uv run python -c '
import json, os, subprocess
receipt_ids = {r.get("decision_id") for r in json.loads(os.environ["RECEIPTS"])}
delivered = subprocess.run(
    ["docker", "compose", "exec", "-T", "clickhouse", "sh", "-c",
     "exec clickhouse-client --user \"$CLICKHOUSE_USER\" --password \"$CLICKHOUSE_PASSWORD\" -q \"$1\"",
     "--", "SELECT decision_id FROM udp.alerts WHERE NOT suppressed FORMAT TSV"],
    capture_output=True, text=True, check=True).stdout.split()
print(sum(1 for d in delivered if d not in receipt_ids))
')
check_ge "notifier:delivered alerts ≥1 (limit exercised)" "$ALERTS_DELIVERED" "1"
check_eq "notifier:delivered alerts missing from receiver" "0" "$MISSING"
PER_MIN_MAX=$(chq "SELECT max(n) FROM (SELECT toStartOfMinute(created_at) m, count() n
                    FROM udp.alerts WHERE NOT suppressed GROUP BY m)")
check_le "notifier:max delivered in one minute (limit 10)" "$PER_MIN_MAX" "10"
echo "alerts: total=$ALERTS_TOTAL delivered=$ALERTS_DELIVERED receiver_receipts=$RECEIPT_N"

echo "--- 7/7 CDC: 2 mutations through the conduit bus"
CDC_BEFORE=$(chq 'SELECT count() FROM udp.app_users_cdc')
uv run udp-pg-mutate --mode mix --count 2 --seed 8 >/dev/null
CDC_OK=0
for _ in $(seq 1 20); do # ≤10s in 0.5s polls
  CDC_NOW=$(chq 'SELECT count() FROM udp.app_users_cdc')
  if ((CDC_NOW - CDC_BEFORE >= 2)); then CDC_OK=1; break; fi
  sleep 0.5
done
check_eq "cdc:2 mutations landed ≤10s" "1" "$CDC_OK"
DIFF_ROWS=$(diff \
  <(docker compose exec -T postgres sh -c \
    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT id, email, plan, status FROM app_users ORDER BY id"' \
    | tr '|' '\t') \
  <(chq "SELECT id, argMax(email, offset), argMax(plan, offset), argMax(status, offset)
         FROM udp.app_users_cdc GROUP BY id HAVING argMax(op, offset) != 'd' ORDER BY id") \
  | wc -l)
check_eq "cdc:CH current-state diff vs Postgres (rows)" "0" "$DIFF_ROWS"

# ---------- report -----------------------------------------------------------

echo
echo "=== SMOKE RESULTS ($MODE) ==="
printf '%-6s %-55s %s\n' "RESULT" "CHECK" "DETAIL"
for row in "${RESULTS[@]}"; do
  IFS='|' read -r status name detail <<<"$row"
  printf '%-6s %-55s %s\n' "$status" "$name" "$detail"
done
echo
if ((FAILURES)); then
  echo "SMOKE: $FAILURES FAILURE(S)"
  exit 1
fi
echo "SMOKE: ALL PASS"
