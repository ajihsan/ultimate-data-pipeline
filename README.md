# Ultimate Data Pipeline — Realtime

This repository contains a real-time pipeline for app-user tickets. The
pipeline classifies each ticket with a deterministic rule table. It sends
tickets that the rules cannot classify to a Laya decision model. It stores
decisions in ClickHouse and sends alerts to Telegram or Discord. Postgres
changes flow through a Conduit bus.

The realtime stack replaces the legacy JVM stack (Debezium, Kafka, Flink, and
the Mastodon producer). The legacy stack remains in git history.

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

- **ingest** reads topic `tickets` and writes `udp.tickets_raw`. A malformed
  ticket goes to topic `tickets_dlq`. A ticket with a missing or invalid
  required field also goes to that topic. The pipeline does not drop it
  silently.

- **decide** runs the cascade. Six keyword rules classify about 95% of tickets
  in process. The remaining tickets POST the `/v1/systemone` contract to the
  Laya model container. Every flagged ticket produces a row in
  `udp.model_decisions`: a real verdict or a degraded fallback.

- **notify** checks a decision against `THRESHOLD_SCAM_P=0.35` and
  `THRESHOLD_ESCALATE_P=0.75`. It drops a duplicate `decision_id` with a point
  query against `udp.alerts`. It limits delivery to 10 alerts per epoch minute.
  Extra alerts land with `suppressed=true`. Delivery goes to Telegram, Discord,
  the log, or the in-network mock receiver.

- **conduit + cdc** copy Postgres `app_users` changes through logical
  replication to topic `pg_changes`. The `cdc` pipeline writes the change log
  to `udp.app_users_cdc`. A malformed record goes to `pg_changes_dlq`.

Full component inventory, ports, and failure semantics:
[docs/architecture.md](docs/architecture.md). Wire contracts:
[docs/schemas.md](docs/schemas.md).

## Quickstart

Prerequisites: Docker with Compose v2 and `uv`. The project needs no system
ROCm, no Kafka install, and no cloud account.

```bash
make sync        # uv sync --inexact (preserves the .venv model runtime)
make up-model    # full stack plus the Laya model container (profile `model`)
make demo        # end-to-end acceptance on the real model
```

`make demo` runs `tests/integration/smoke.sh --model`. It starts from a clean
slate and sends 2000 tickets at 100 events/s. It runs a scam burst and checks
counts, the 95/5 split, degraded rows, the DLQ, alert joins, and CDC. It prints
a PASS/FAIL table and exits nonzero on a failure.

`bash tests/integration/smoke.sh` runs the same checks on the mock decision
server. It needs no GPU. The CI smoke job runs this mode.

### Demo walkthrough

1. `make up-model` starts Redpanda, ClickHouse, Postgres, Console, the four
   pipelines, Conduit, and the model container. The model uses the bind-mounted
   `.venv` and `.hf-cache` and the GPU device nodes.

2. The steady phase sends 2000 tickets at 100 events/s. About 95% take the rule
   fast path and about 5% go to Laya. `udp.alerts` fills, limited to 10
   delivered alerts per minute.

3. The scam burst sends 60 s at 3× rate with a 360-event flagged budget. The
   fast path absorbs most of the burst. Extra alerts are recorded with
   `suppressed=true`.

4. To watch the alerts, use the mock receiver command below or open the Console
   at <http://127.0.0.1:8085>.

```bash
docker compose exec -T alert-receiver python -c \
  'import json, urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://localhost:8082/alerts"))["alerts"][-3:], indent=2))'
```

To send alerts to a phone, set `NOTIFY_PROVIDER=telegram` plus
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, then run `make up-model`
again.

## Status and measurements

v1 is feature-complete. The measured acceptance table is in
[docs/acceptance.md](docs/acceptance.md). Key results:

- 60 000 tickets at 100 events/s: no end-to-end loss. The fast/model split was
  95.02/4.98. All four consumer groups drained.

- Fast path p95 was 81 ms. Model path p95 was 467 ms.

- Model runtime was 15.72 decisions/s. In-process p95 was 64.1 ms.

- Chaos restart at t=180 s: alerts deduped by `decision_id`. The rate limit
  held at 10 delivered alerts per minute.

- ClickHouse current state matched Postgres after the CDC test.

The device matrix and runtime options are in
[docs/model-runtime.md](docs/model-runtime.md). Restart, degraded-mode, and
dedupe notes are in [docs/operations.md](docs/operations.md).

## Configuration

Everything is environment-driven through `.env`. Copy `.env.example` and never
commit `.env`.

| group | variables | notes |
|---|---|---|
| thresholds | `THRESHOLD_SCAM_P=0.35`, `THRESHOLD_ESCALATE_P=0.75` | from the model eval sweep in [docs/jev_eval.md](docs/jev_eval.md) |
| notifier | `NOTIFY_PROVIDER=log\|mock\|telegram\|discord` (comma list = fan-out), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_WEBHOOK_URL`, `ALERT_RATE_LIMIT_PER_MIN=10` | `log` is the safe default; a failed POST does not lose the alerts row |
| model | `MODEL_ENDPOINT_URL` | default `http://model:8081/v1/systemone`; a host-run `make model` needs `MODEL_HOST=0.0.0.0` and a ufw rule (below) |
| host tools | `REDPANDA_BOOTSTRAP`, `POSTGRES_*` | `udp-generate` and `udp-pg-mutate` read these from the shell; Compose reads `.env` for its own interpolation |
| ports | `REDPANDA_CONSOLE_PORT=8085`, `POSTGRES_PORT=5432` | 127.0.0.1-bound; ClickHouse host ports 8123/9000 are fixed in `docker-compose.yaml` |
| credentials | `CLICKHOUSE_*`, `POSTGRES_*` | dev defaults in `.env.example` |

`udp-generate` reads `REDPANDA_BOOTSTRAP` from the environment. If you keep the
value only in `.env`, export it in the shell or pass `--bootstrap`:

```bash
export REDPANDA_BOOTSTRAP=127.0.0.1:9092
uv run udp-generate --count 10
```

## Troubleshooting

- **`make model` is unreachable from decide.** The container cannot reach the
  host's 127.0.0.1, and ufw drops container-to-host traffic on this box. Use
  the in-network model container (`make up-model`), or run the host server with
  `MODEL_HOST=0.0.0.0`, set
  `MODEL_ENDPOINT_URL=http://host.docker.internal:8081/v1/systemone`, and allow
  the Docker subnets: `pkexec ufw allow from 172.16.0.0/12 to any port 8081 proto tcp`.

- **Port already in use.** qBittorrent owns 8080, so the Console uses 8085.
  Check `docker compose ps` and the port table in
  [docs/architecture.md](docs/architecture.md).

- **`localhost:9092` connect-refused lines from the generator.** librdkafka
  tries IPv6 `::1` before IPv4. The producer still publishes. To silence the
  lines, export `REDPANDA_BOOTSTRAP=127.0.0.1:9092`.

- **Decisions missing after a crash.** This is expected and bounded. A crash
  replays at most about 10 s of traffic (`checkpoint_limit 1024`). The tables
  are append logs. Dedupe at read time. See
  [docs/operations.md](docs/operations.md).

- **Disk use.** The topics keep 7 days. The ClickHouse tables are unbounded.
  Monitor disk use and set a TTL before production.

## Repo scope

This repository is realtime only. Batch analytics moves to a separate repo at
v2. That repo will connect to the same ClickHouse, own the mart layer, and not
alter the raw tables here. The contract is in
[docs/v2-batch.md](docs/v2-batch.md).

## Docs map

- [docs/architecture.md](docs/architecture.md): components, decision path,
  failure semantics.
- [docs/schemas.md](docs/schemas.md): wire and table contracts.
- [docs/rules.md](docs/rules.md): human-readable mirror of the rule table.
- [docs/acceptance.md](docs/acceptance.md): measured acceptance results.
- [docs/operations.md](docs/operations.md): restart, degraded mode, dedupe,
  retention.
- [docs/model-runtime.md](docs/model-runtime.md): device matrix and runtime.
- [docs/jev_eval.md](docs/jev_eval.md): model eval and latency analysis.
- [docs/history/s0-feasibility.md](docs/history/s0-feasibility.md): early ROCm
  spike log.
- [docs/adr/0001-conduit-cdc.md](docs/adr/0001-conduit-cdc.md): why the CDC
  path uses Conduit.
- [docs/v2-batch.md](docs/v2-batch.md): v2 batch repo contract.
