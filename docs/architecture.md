# Architecture (v1 realtime)

The stack is one compose project (`name: udp`). It has single-node Redpanda
(Kafka API), ClickHouse, Postgres, Redpanda Console, and four Redpanda Connect
pipelines. The pipelines use config-only YAML and no custom JVM code. One
Conduit bus container moves CDC. An optional Laya model container runs in the
compose profile `model`.

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

Every host port binds to 127.0.0.1. This is a shared homelab, so nothing is
reachable from the LAN. Connect pipelines expose `/ping` and Prometheus
`/metrics` on container port 4195, republished as host ports 4196–4199.

## Decision-path walkthrough (decide)

1. The `kafka_franz` input uses group `udp-decide` and
   `checkpoint_limit 1024`. The limit bounds crash redelivery to about 10 s of
   traffic. The franz reader pauses the partition when pending reaches the
   limit. Pending moves between about 100 and 150 at 100 events/s because the
   output batcher flushes 100 rows each second.

2. The rule table uses deterministic first-match order on lowercased text.
   [rules.md](rules.md) is the human-readable mirror. The order is scam_report
   0.95, then billing/bug/access 0.90, then feature_request/complaint 0.85. A
   no-match ticket uses the shape heuristics (garbled, >40% caps, ≥5 `!`/`?`,
   or <12 chars) at 0.40, or 0.45.

3. A confident verdict produces a FastDecision row and a topic fan-out. A
   flagged ticket POSTs `/v1/systemone`. Four threads cap concurrency at 4, so
   model cohorts stay at 4 states or fewer. Service stays at about 310 ms,
   below the 1500 ms timeout. Retries fire only on 429/503. Every flagged
   ticket produces a ModelDecision row, with real answers or a degraded
   fallback (`escalate=scam=0`, `degraded=true`, `model_degraded_total` counter).

4. On this GPU, about 95% of tickets took the fast path (about 2 ms each) and
   about 5% took the model path (≤310 ms). This is 230–360 messages/s against
   100 events/s inflow. Fast-path p95 is 81 ms. Model-path p95 is 467 ms end
   to end. See [jev_eval.md](jev_eval.md) for the full latency analysis.

## Data contracts

Wire field contracts for Ticket, FastDecision, ModelDecision, and AlertEvent are
in [schemas.md](schemas.md). ClickHouse DDL and engine choices are in
[clickhouse/init.sql](../clickhouse/init.sql) and
[clickhouse/cdc.sql](../clickhouse/cdc.sql). The read-time dedupe contracts
are:

- `tickets_raw` uses ReplacingMergeTree.

- The other tables are MergeTree append logs.

- Alerts use `decision_id` as the key.

- CDC current state is `argMax(col, offset)` per id with a tombstone drop.

## Failure semantics

| failure | behavior |
|---|---|
| invalid ticket (malformed JSON, or a missing or invalid required field) | ingest routes a copy to topic `tickets_dlq` keyed by the reason; decide drops the same payload instead of deciding it |
| model endpoint down / timing out / unreadable 200 | decide stamps degraded fallback rows (p=0 → never alert) and keeps consuming; **model-tier alerting is down** while the endpoint is (fast path unaffected); SQL gauge: `SELECT count() FROM udp.model_decisions WHERE degraded AND decided_at > now() - INTERVAL 5 MINUTE` |
| clickhouse insert failure | connect nacks and retries (`retry_until_success` on ingest/decide; notify's output switch is `strict_mode` with batching count:1 so an alert row lands before its input ack) |
| decide/notify SIGKILL | ~40s franz session-timeout rebalance, then replay of ≤~10s (checkpoint_limit 1024). Both alert keys derive from the ticket (`fast:<event_id>`, `model:<event_id>`), so notify drops a replayed alert. The decision tables grow a thin duplicate tail (read-time dedupe contract). |
| pipeline (re)start, first fetch | one-time ~2–4.4s franz chunk warm-up before the first decisions land |
| conduit restart | checkpoints (incl. the replication slot) persist in the `conduit-data` badger volume — resumes without re-snapshotting; replays re-append rows whose dedupe is read-time (argMax by kafka offset) |
| malformed `pg_changes` record (conduit bug or manual write) | cdc routes it to topic `pg_changes_dlq` keyed by the reason, increments `cdc_malformed_total`, and continues with later records; one bad record cannot pin the offset |
| alert POST failure | retried (3s timeout, 2 retries, backoff on 429/503), then logged (`notify_delivery_failed_total`). The alerts row always lands. `suppressed=false` means the alert was not rate-limited, not that the POST succeeded. Alerts are exactly-once per decision_id (one per ticket); delivery is at-least-once. |
| rate limiter RAM counter loss | restart mid-minute re-arms the window (durable decision_id dedupe still prevents double rows); a counter read/update failure fails OPEN (guardrail degraded for that window, rows never blocked) |

## RAM / limits

This table lists container caps and measured peaks from the chaos run. The run
sent 60k events at 100 events/s for 10 minutes and killed the processes at
t=180 s. `docker stats` sampled every 30 s, 20 times. The values are
informational, not gated.

| service | limit | peak RSS |
|---|---|---|
| model | 5g | 1.58g (torch peak allocation 2.5g, matches the spike measurement) |
| clickhouse | 3g (server ceiling 2.5g) | 429m |
| redpanda | 1.5g (1G heap, `--overprovisioned`, reserve 0) | 70m–340m |
| notify | 250m | 125m |
| decide | 250m | 99m |
| ingest | 250m | 79m |
| cdc | 250m | 50m |
| console | 200m | 29m |
| conduit | 250m | 28m |
| postgres | 256m | 20m |

The ClickHouse ceiling (2.5g) sits below the container cap. ClickHouse can
never starve the rest of the homelab. The model 5g cap covers the 2.5g torch
peak plus allocator slack.
