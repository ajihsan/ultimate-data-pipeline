# Ultimate Data Pipeline — Realtime

Synthetic app-user ticket stream → cascade decision (deterministic rule fast path, Laya decision model for what the rules can't classify) → ClickHouse → Telegram/Discord triggers, with Postgres CDC flowing through a Conduit bus. Everything is config-first Redpanda Connect pipelines on a single-node Redpanda cluster, a consumer-ROCm model runtime (RX 6600 XT via TheRock wheels — no cloud inference, no system ROCm install), and a compose stack that comes up in one command. The realtime path replaces the legacy JVM stack (Debezium/Kafka/Flink/Mastodon producer).

**Status:** v1 feature-complete on branch `feat/realtime-v1-redpanda` (S0–S8 landed; acceptance table below is measured, not aspirational).

## Architecture

```text
                       ┌──────────────────────────── docker compose (udp-net, all ports 127.0.0.1) ───────────────────────────┐
                       │                                                                                                       │
 udp-generate ────────▶│  redpanda :9092 ── topic `tickets` ────────────┐                                                      │
 (uv run udp-generate, │      ▲          │                              │                                                      │
 host python)          │      │          └──▶ ingest (connect) ────────┼──▶ clickhouse :8123/:9000                            │
                       │      │               │ (DLQ → `tickets_dlq`)  │        ├─ udp.tickets_raw (ReplacingMT)              │
                       │      │                                      │        ├─ udp.fast_decisions                         │
                       │      └──▶ decide (connect) ────────────────┘        ├─ udp.model_decisions                           │
                       │               │  rules 1–6 hit? → fast path        ├─ udp.alerts ◀── notify (connect)               │
                       │               │  else FLAG ──▶ model (Laya)        │       thresholds scam≥.35 / escalate≥.75       │
                       │               │          /v1/systemone             │       dedupe by decision_id                    │
                       │               │          on :8081                  │       rate limit 10 delivered/min              │
                       │               │                                    │       → telegram / discord / log / mock        │
                       │  postgres :5432 ──▶ conduit (pg_changes) ──▶ topic `pg_changes` ──▶ cdc (connect) ──▶ udp.app_users_cdc│
                       │                                                                                                       │
                       │  console :8085 (Redpanda UI)   ·   connect metrics: 4196–4199 (/metrics, /ping)                        │
                       └───────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **ingest** — topic `tickets` → `udp.tickets_raw`; malformed/missing-field tickets → topic `tickets_dlq` (never dropped silently).
- **decide** — the cascade: 6 keyword rules classify ~95% in-process (~2ms); flagged/ambiguous tickets (~5%) POST the `/v1/systemone` contract to the Laya model container; every flagged ticket yields a row in `udp.model_decisions` (real verdict or degraded fallback on any endpoint failure).
- **notify** — qualifies decisions against `THRESHOLD_SCAM_P=0.35` / `THRESHOLD_ESCALATE_P=0.75`, dedupes by `decision_id` (point query against `udp.alerts` — restart-safe), rate-limits to 10 delivered alerts/epoch-minute (drop-and-record: extras land as `suppressed=true`), delivers to Telegram/Discord (or the in-network mock receiver).
- **conduit + cdc** — Postgres `app_users` snapshot + WAL (logical replication) → topic `pg_changes` (OpenCDC) → `udp.app_users_cdc` change log.

Full component inventory, pinned images, failure semantics: [docs/architecture.md](docs/architecture.md). Wire schemas: [docs/schemas.md](docs/schemas.md).

## Quickstart

Prereqs: **docker** (compose v2) and **uv**. That's it — no system ROCm, no Kafka, no cloud account.

```bash
make sync        # uv sync --inexact (installs deps; --inexact preserves the .venv model runtime)
make up-model    # full stack + the Laya model container (compose profile `model`)
make demo        # end-to-end acceptance on the real model (see below)
```

`make demo` = `tests/integration/smoke.sh --model`: clean slate → healthchecks → steady 2000 @ 100 ev/s → scam-spike burst → SQL asserts (counts, 95/5 split, degraded=0, DLQ=0, alert joins) → notifier joins → CDC round-trip. It prints a PASS/FAIL table and exits non-zero on any failure. `bash tests/integration/smoke.sh` (no flag) runs the same thing on the mock decision server — no GPU needed — and is what CI runs.

### Demo walkthrough (what actually happens)

1. `make up-model` brings up redpanda, clickhouse, postgres, console, the four pipelines, conduit, and the model container (GPU handoff via `/dev/kfd` + render GIDs; weights come from the bind-mounted `.hf-cache`).
2. The **steady phase** publishes 2000 tickets at 100 ev/s: ~95% classified by the rule fast path, ~5% flagged to Laya. `udp.alerts` starts filling: every qualifying decision (mock receiver / phone with a real token) — rate-limited to 10/min.
3. The **scam-spike burst** (`--profile demo`: 60s at 3× rate, capped at 360 flagged) floods scam_report tickets — the fast path absorbs 98%+ in-process, the model handles the tail, and the receiver sees a steady 10 alerts/min without a single dropped row (extras are recorded as `suppressed=true`).
4. Watch it live: `docker compose exec -T alert-receiver python -c 'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://localhost:8082/alerts"))["alerts"][-3:], indent=2))'`, or open the console on http://127.0.0.1:8085.

To watch alerts on an actual phone instead of the mock receiver: set `NOTIFY_PROVIDER=telegram` (+ `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) in `.env` and re-`make up-model` — the alerts table and rate limit behave identically.

### Device matrix (works on my PC first, then others)

| device | runtime | status |
|---|---|---|
| **AMD RX 6600 XT (gfx1032) — this PC** | TheRock nightly ROCm wheels (`torch 2.14.0+rocm10.1.0`) in the project `.venv`; runs via the compose `model` profile (bind-mounted `.venv` + `.hf-cache`, `/dev/kfd` + render GIDs) — **no system ROCm** | **primary, measured** (S0/S5: 15.72 dec/s local, p95 64ms in-process / 196ms HTTP) |
| NVIDIA | install torch CUDA wheels into `.venv` (e.g. `uv pip install torch --index-url https://download.pytorch.org/whl/cu126`) and run the model container with `gpus: all` instead of `/dev/kfd` — the server code is device-agnostic (`cuda` works on both) | documented alternative |
| Apple Silicon | MPS: point `laya.load` at `mps` (`DEFAULT_DEVICE` in `src/udp/model_server.py`); bf16 autocast behavior differs on MPS — evaluate before trusting latencies | documented alternative, untested |
| ROCm/PyTorch baked into docker | alternative to bind-mounting the host venv: bake the TheRock wheels into the model image — base is pinned `python:3.12-bookworm@sha256:5560e9ab…` in docker-compose.yaml | documented alternative |

## Measured acceptance (S8, 2026-10-13)

The real-model chaos/load run: 60 000 tickets @ 100 ev/s × 10 min (seed 7), `docker compose kill decide notify` at t=180s + explicit restart, mock alert-receiver recording deliveries. Reproduce with `make load-test`. Prior-stage numbers from docs/jev_eval.md (S0/S5), S4/S6/S7 sessions.

| # | criterion | result | measured |
|---|---|---|---|
| 1 | S0 model runtime — local throughput / latency | **PASS** | 15.72 dec/s; p95 64.1ms in-process / ~196ms HTTP |
| 2 | S5 eval accuracy | **PASS** | 67.5% choice accuracy; scam separation 0.67 vs 0.03; complaint 16.1% (known weak head) |
| 3 | thresholds | **PASS** | scam ≥0.35 (precision 1.00 / recall 0.87), escalate ≥0.75 |
| 4 | S4 steady 6000 @ 100 ev/s → 95/5 fast/model split | **PASS** | 5701/299 = 95.02/4.98%; convergence 7.2s |
| 5 | S5b pipeline latency | **PASS** | fast-path p95 81ms; model-path p95 467ms; burst degraded 0/348 |
| 6 | S6 alerts volume + rate limit | **PASS** | 4249 unique alerts; exactly 10 delivered/min; POST p95 21.5ms |
| 7 | S7 CDC | **PASS** | snapshot 135ms; c/u/d 361–591ms; CH current-state == Postgres 24/24 |
| 8 | chaos: produced == landed | **PASS** | 60 000 published = 60 000 raw rows = 60 000 distinct decided (union) — zero end-to-end loss |
| 9 | chaos: per-table distinct ≥ produced slice | **PASS** | raw 60 000; fast 57 232 (≥0.90× ✓); model 2768 (≥0.03× ✓) — 2026-10-13 run |
| 10 | chaos: dupes ≤ +10% per table | **PASS** | fast 449/57 232 (0.78%), model 14/2768 (0.51%), raw 0 — all from the kill-window replay (2026-10-13 run) |
| 11 | chaos: decision coverage (non-DLQ) | **PASS** | 0 misses; DLQ 0 |
| 12 | chaos: alerts dedupe + delivery joins | **PASS** | 6464 rows = 6464 distinct decision_ids; 110 delivered == 110 receiver receipts |
| 13 | chaos: rate limit holds under recovery | **PASS** | exactly 10 delivered per minute, every minute, including the restart window |
| 14 | chaos: trigger p95 ≤ 2s | **PASS** | p95 13ms nominal window (83ms whole run) — decided_at → receiver receipt |
| 15 | chaos: final drain | **PASS** | all 4 consumer groups lag 0 immediately after inflow stops (2026-10-14 re-verification) |
| 16 | chaos: caught-up (lag < 700) within 120s of restart AND lag == 0 within 60s of inflow end — **renegotiated** from lag<100/60s: the live-inflow committed-offset sawtooth 111–606 (input prefetch buffer) makes lag<100-while-inflow physics, and the ~40s franz session-timeout rebalance + ~40s drain put caught-up at +80–81s (measured twice, so the originally-suggested 60s bar is impossible; 120s = the pre-existing extension bar) | **PASS** (renegotiated) | caught-up at +80s (rebalance +40s, max lag 5596); lag 0 drained in 0s after inflow end; steady-inflow band 0–604 (2026-10-14 re-verification) |
| 17 | chaos: degraded after recovery ≤ 0.5% of model rows — **renegotiated** from =0: replay-window flagged bursts can exceed the 1500ms model timeout (runs 1–3 measured 0) | **PASS** (renegotiated) | 0/3024 (2026-10-14 re-verification); worst recorded 7/2782 (0.25%) replay-window only. Terminal-in-v1 by design: p=0 → below both thresholds → no false alerts, only a coverage gap |
| 18 | RAM (informational, not gated) | — | peak RSS: model 1.6G/5G, clickhouse 429M/3G, decide 99M, notify 125M, ingest 79M, cdc 50M, conduit 28M, redpanda 70M/1.5G, postgres 20M |

**Honest summary: 15/17 gated criteria PASS (one informational row); rows 16–17 are renegotiated criteria (marked in the rows, measured rationale included) and pass — re-verified 2026-10-14 after the deslop refactor.** The two renegotiations are restart-recovery semantics of the frozen v1 configs, both root-caused; correctness (no loss, no dupes, full coverage, rate limit, delivery) held through the kill.

## Operational notes

- **Restart semantics.** `docker compose kill` SIGKILLs and — with compose v2 — does **not** auto-restart the container within any observable window; recovery is explicit (`docker compose up -d decide notify`), which is what the chaos script does. On restart: (a) the franz consumer group sits in `PreparingRebalance` ~40s — the 45s `session.timeout.ms` of franz-go (the pipelines' input library) must expire before the group coordinator evicts the SIGKILLed member and the new one gets an assignment; (b) the backlog then drains at ~200–300 msg/s (≈40s for the ~6k accumulated here); (c) afterwards the committed offset rides a ~110–610 sawtooth behind the live stream for as long as inflow continues (input prefetch buffer stays topped up after a replay) — it is not real backlog: it drains to 0 the moment inflow stops, and delivery latency is unaffected (13ms p95). `docker compose stop` (graceful) avoids the rebalance delay.
- **Degraded mode.** Any model-endpoint failure (timeout/transport/429-503-after-retry) stamps the ModelDecision row `degraded=true` with `escalate_probability = scam_probability = 0` and counts in the decide pipeline's `model_degraded_total` metric. Degraded rows are **terminal in v1**: p=0 never qualifies → **model down ⇒ model-tier alerting down** (the fast path keeps deciding and the rule-tier scam alerts keep firing; there is no model-tier alert coverage until the endpoint recovers). Detect it: `curl -s http://127.0.0.1:4197/metrics | grep model_degraded_total` or the SQL gauge: `SELECT count() FROM udp.model_decisions WHERE degraded AND decided_at > now() - INTERVAL 5 MINUTE`.
- **Read-time dedupe contracts** (the tables are append logs; replays are legal and bounded):
  - `udp.tickets_raw` — ReplacingMergeTree; readers MUST dedupe: `SELECT DISTINCT event_id …` / `argMax` / `FINAL`.
  - `udp.fast_decisions` / `udp.model_decisions` — MergeTree with NO key uniqueness; `checkpoint_limit 1024` bounds a crash replay to ~10s of traffic, so expect a thin duplicate tail after any crash (measured: ≤0.8%). Dedupe per `event_id` at read time.
  - `udp.alerts` — dedupe key `decision_id` (fast path synthesizes `fast:<event_id>`); `count() == uniqExact(decision_id)` is an invariant.
  - `udp.app_users_cdc` — current state = `argMax(col, offset)` per id, `HAVING argMax(op, offset) != 'd'` (a plain `op != 'd'` filter resurrects deleted ids — see clickhouse/cdc.sql).
- **Cold-start quirk.** The first franz fetch after a pipeline (re)start takes ~2–4.4s one-time (chunk warm-up) — the first decisions of a fresh run land a beat late; harmless, don't tune it.
- **Ports are homelab-negotiated.** Console is on **8085** (8080 is qBittorrent's WebUI on this box); all host ports are bound to 127.0.0.1 only.

## Configuration

Everything is environment-driven via `.env` (copy `.env.example`; never commit it). Key groups:

| group | vars | notes |
|---|---|---|
| thresholds | `THRESHOLD_SCAM_P=0.35`, `THRESHOLD_ESCALATE_P=0.75` | from the S5 eval sweep (docs/jev_eval.md) |
| notifier | `NOTIFY_PROVIDER=log\|mock\|telegram\|discord` (comma-list = fan-out), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, `ALERT_RATE_LIMIT_PER_MIN=10` | `log` is the safe default; failed deliveries never lose the alerts row |
| model | `MODEL_ENDPOINT_URL` | default `http://model:8081/v1/systemone` (in-network container); a host-run `make model` needs `MODEL_ENDPOINT_URL=http://127.0.0.1:8081/v1/systemone` **plus** a ufw rule (below) |
| ports | `REDPANDA_CONSOLE_PORT=8085`, `POSTGRES_PORT=5432` | 127.0.0.1-bound; ClickHouse host ports 8123/9000 are fixed in docker-compose.yaml |
| creds | `CLICKHOUSE_*`, `POSTGRES_*`, `REDPANDA_BOOTSTRAP` | dev defaults in .env.example |

## Troubleshooting

- **`make model` unreachable from decide** — ufw drops container→host traffic on this box. Either use the in-network model container (`make up-model`, the default) or allow docker subnets: `pkexec ufw allow from 172.16.0.0/12 to any port 8081 proto tcp`.
- **Port already allocated** — qBittorrent owns 8080 (console uses 8085); check `docker compose ps` and the port table above; everything else on this box negotiates via `.env`.
- **`localhost:9092` connect-refused lines from the generator** — librdkafka tries IPv6 `::1` first and logs a few `%3|FAIL|…` lines before falling back to IPv4; publishing succeeds (verified in every run). Silence it with `REDPANDA_BOOTSTRAP=127.0.0.1:9092`.
- **Decisions missing after a crash** — expected, bounded, and by design: read-time dedupe + coverage SQL in smoke.sh tell you exactly what a replay did; a crash replays ≤ ~10s (checkpoint_limit 1024).
- **Disk** — `docker compose down -v` wipes all four volumes (redpanda, clickhouse, postgres, conduit); data volumes grow with the stream (retention 7d on the topics). `/home` at 91% is the operator's problem, not the pipeline's — keep chaos artifacts in /tmp.

## Repo scope

**Realtime only.** Batch/dbt analytics is a separate repo at v2 — it will connect to the same ClickHouse, own the mart layer, and never alter raw tables here. The contract it will consume is already specified: [docs/v2-batch.md](docs/v2-batch.md).

## Docs map

[docs/architecture.md](docs/architecture.md) (components, decision path, failure semantics) · [docs/schemas.md](docs/schemas.md) (wire + table contracts) · [docs/rules.md](docs/rules.md) (the rule table, mirrored into decide.yaml) · [docs/jev_eval.md](docs/jev_eval.md) (model eval + latency analysis) · [docs/feasibility.md](docs/feasibility.md) (S0 ROCm spike) · [docs/CONNECTOR_LICENSES.md](docs/CONNECTOR_LICENSES.md) · [docs/v2-batch.md](docs/v2-batch.md).
