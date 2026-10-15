# Architecture (v1 realtime)

The whole stack is one compose project (`name: udp`): single-node Redpanda (Kafka API), ClickHouse, Postgres, Redpanda Console, four Redpanda Connect pipelines (config-only YAML, no custom JVM code), one Conduit bus container, and an optional Laya model container behind the compose profile `model`.

## Flow

```text
udp-generate (host)
   │  tickets (JSON, schema v1, seeded stream)
   ▼
redpanda ── topic: tickets ──────────────▶ ingest (connect) ──▶ clickhouse
   │                                          │  valid  ────────▶ udp.tickets_raw (ReplacingMergeTree)
   │                                          └─ invalid ───────▶ topic tickets_dlq (keyed by reason)
   │
   ├───────── topic: tickets ─────────────▶ decide (connect)
   │                                          │  rules 1–6 match (≈95%) ──▶ udp.fast_decisions
   │                                          │                            + topic fast_decisions
   │                                          └─ flagged/ambiguous (≈5%)
   │                                              │  POST /v1/systemone ──▶ model (Laya container)
   │                                              │  ◀── answers (or degraded fallback)
   │                                              └──────────────────────────▶ udp.model_decisions
   │                                                                          + topic model_decisions
   │
   ├─ topic: fast_decisions ──┐
   └─ topic: model_decisions ─┴──▶ notify (connect)
                                     │  qualifies? (model: p≥0.35 scam ∨ p≥0.75 escalate;
                                     │             fast: scam_report @ conf ≥0.85)
                                     │  dedupe: count(udp.alerts WHERE decision_id=?) — restart-safe
                                     │  rate limit: 10 delivered / epoch-minute (drop-and-record)
                                     ├──▶ POST telegram / discord / mock-receiver (delivered only)
                                     └──▶ udp.alerts (ALWAYS: delivered + suppressed)

postgres (app_users, wal_level=logical)
   └──▶ conduit (pg source: snapshot + logrepl CDC)
          └── topic: pg_changes (OpenCDC: op c|u|d, before/after, position)
                 └──▶ cdc (connect) ──▶ udp.app_users_cdc (MergeTree change log)
```

## Component inventory

| component | image (pinned, linux/amd64, verified 2026-10-13) | host ports | mem_limit | role |
|---|---|---|---|---|
| redpanda | `redpandadata/redpanda:v26.2.4` | 9092 (Kafka API), 9644 (admin) | 1.5g | topics: tickets, fast_decisions, model_decisions, tickets_dlq, pg_changes (created by the one-shot `redpanda-init` sidecar) |
| clickhouse | `clickhouse/clickhouse-server:26.9.13.15` | 8123 (HTTP), 9000 (native) | 3g (2.5g server ceiling) | `udp.tickets_raw`, `udp.fast_decisions`, `udp.model_decisions`, `udp.alerts`, `udp.app_users_cdc` |
| postgres | `postgres:16-alpine` (wal_level=logical) | 5432 | 256m | CDC source: `app_users` |
| console | `redpandadata/console:v3.12.0` | 8085 (8080 is taken by qBittorrent) | 200m | Redpanda UI |
| ingest | `redpandadata/connect:4.113.0` | 4196 → container 4195 | 250m | tickets → tickets_raw / DLQ |
| decide | `redpandadata/connect:4.113.0` | 4197 | 250m | cascade decisions |
| notify | `redpandadata/connect:4.113.0` | 4198 | 250m | alerts + delivery |
| cdc | `redpandadata/connect:4.113.0` | 4199 | 250m | pg_changes → app_users_cdc |
| conduit | `ghcr.io/conduitio/conduit:v0.20.0@sha256:e674b7…` (Docker Hub image no longer exists — ghcr only; v0.20.0 fixes the SEV-0 snapshot→CDC stall) | none (in-network; /healthz on API :8080) | 250m | postgres → pg_changes |
| model | `python:3.12-bookworm@sha256:5560e9ab…`, compose profile `model` | 8082 → container 8081 | 5g | Laya `/v1/systemone` (bind-mounted `.venv` TheRock wheels + `.hf-cache` weights; `/dev/kfd` + video/render GIDs) |
| jev-mock | built `docker/mock.Dockerfile` (`udp/jev-mock:4.113.0-s4`), override `compose.mock.yaml` | none | 150m | stands in for the model in mock runs |
| alert-receiver | built `docker/alert-receiver.Dockerfile` (`udp/alert-receiver:4.113.0-s6`), override `compose.mock.yaml` | none (in-network :8082) | 150m | records POSTed alerts for verification joins |

Every host port is bound to 127.0.0.1 (shared homelab; nothing is LAN-reachable). Connect pipelines expose `/ping` and Prometheus `/metrics` on container :4195, republished as host 4196–4199.

## Decision-path walkthrough (decide)

1. `kafka_franz` input, group `udp-decide`, `checkpoint_limit 1024` (bounds crash redelivery to ~10s of traffic; the franz reader pauses the partition when pending ≥ limit — pending sawtooths ~100–150 at 100 ev/s thanks to the 100-row/1s output batcher).
2. Rule table — deterministic FIRST-MATCH on the lowercased text (docs/rules.md is the human-readable mirror): scam_report 0.95 → billing/bug/access 0.90 → feature_request/complaint 0.85; no match → shape-ambiguous heuristics (garbled / >40% caps / ≥5 !? / <12 chars) flag at 0.40, else 0.45.
3. Confident verdict → FastDecision row + topic fan-out. Flagged → POST `/v1/systemone` (threads 4 caps concurrency at 4 → model cohorts ≤4 states → worst in-load service ≈310ms ≪ the 1500ms timeout; retries only on 429/503). Every flagged ticket yields a ModelDecision row — real answers or a degraded fallback (`escalate=scam=0, degraded=true`, `model_degraded_total` counter).
4. Measured on this GPU: ~95% fast (~2ms each) + ~5% model (≤310ms) → 230–360 msg/s steady throughput vs 100 ev/s inflow; fast-path p95 81ms, model-path p95 467ms end-to-end (docs/jev_eval.md has the full latency analysis).

## Data contracts

Wire schemas (Ticket, FastDecision, ModelDecision, AlertEvent) and every ClickHouse table are specified in [schemas.md](schemas.md) — including the read-time dedupe contracts (ReplacingMergeTree on tickets_raw, MergeTree append logs elsewhere, alerts keyed by decision_id, cdc current-state = `argMax(col, offset)` per id with tombstone drop).

## Failure semantics

| failure | behavior |
|---|---|
| invalid ticket (malformed JSON / missing event_id·text·schema_version) | ingest routes a copy to topic `tickets_dlq` keyed by the reason; never silently dropped |
| model endpoint down / timing out / unreadable 200 | decide stamps degraded fallback rows (p=0 → never alert) and keeps consuming; **model-tier alerting is down** while the endpoint is (fast path unaffected); SQL gauge: `SELECT count() FROM udp.model_decisions WHERE degraded AND decided_at > now() - INTERVAL 5 MINUTE` |
| clickhouse insert failure | connect nacks and retries (`retry_until_success` on ingest/decide; notify's output switch is `strict_mode` with batching count:1 so an alert row lands before its input ack) |
| decide/notify SIGKILL | ~40s franz session-timeout rebalance, then replay of ≤~10s (checkpoint_limit 1024); replayed decisions/alerts are made no-ops by notify's dedupe; the fast/model decision tables grow a thin duplicate tail (read-time dedupe contract) |
| pipeline (re)start, first fetch | one-time ~2–4.4s franz chunk warm-up before the first decisions land |
| conduit restart | checkpoints (incl. the replication slot) persist in the `conduit-data` badger volume — resumes without re-snapshotting; replays re-append rows whose dedupe is read-time (argMax by kafka offset) |
| malformed `pg_changes` record (conduit bug or manual write) | cdc retries it indefinitely (offset pinned behind it, logs ~1/s) while newer records still land; nothing is lost, and the group recovers when topic retention drops the record |
| alert POST failure | retried (3s timeout, 2 retries, backoff on 429/503), then logged (`notify_delivery_failed_total`) — the alerts row ALWAYS lands. Alerts are exactly-once per decision_id (dedupe on the alerts row), barring co-batched duplicates from an upstream producer retry; delivery itself is at-least-once |
| rate limiter RAM counter loss | restart mid-minute re-arms the window (durable decision_id dedupe still prevents double rows); a counter read/update failure fails OPEN (guardrail degraded for that window, rows never blocked) |

## RAM / limits

Container caps and measured peaks from the S8 chaos run (60k events @ 100 ev/s, 10 min, SIGKILL restart at t=180s; `docker stats` sampled every 30s, 20 samples — informational, not gated):

| service | limit | peak RSS |
|---|---|---|
| model | 5g | 1.58g (torch peak allocation 2.5g — matches S0) |
| clickhouse | 3g (server ceiling 2.5g) | 429m |
| redpanda | 1.5g (1G heap, `--overprovisioned`, reserve 0) | 70m–340m |
| notify | 250m | 125m |
| decide | 250m | 99m |
| ingest | 250m | 79m |
| cdc | 250m | 50m |
| console | 200m | 29m |
| conduit | 250m | 28m |
| postgres | 256m | 20m |

The clickhouse ceiling (2.5g) sits below the container cap so CH can never starve the rest of the homelab; the model's 5g cap covers the 2.5g torch peak plus allocator slack.
