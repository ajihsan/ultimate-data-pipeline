# v2 — batch repo spec (pointer)

v1 (this repo) is **realtime only**. It provides stream ingest, cascade
decisions, alerts, and CDC. Batch analytics moves to a **separate repo at v2**.
This page is the contract for that repo. It exists now so the realtime tables
do not gain ad-hoc readers that break the split later.

## Ownership split

| | v1 (this repo) | v2 (batch repo) |
|---|---|---|
| raw landing | owns: tickets_raw, fast_decisions, model_decisions, alerts, app_users_cdc | **read-only, never alters raw** (no CREATE/INSERT/UPDATE/DELETE, no TTL/mutation on these tables) |
| marts | none | owns: dbt models (staging → marts) in a separate ClickHouse database (e.g. `udp_marts`), materialized views allowed **only** there |
| transforms | none | dbt |
| schema changes | additive, versioned wire schemas (docs/schemas.md) | proposes raw changes via PR here; never migrates without one |

The batch repo connects to the **same ClickHouse** instance (separate credentials with read-only grants on `udp.*`, write grants only on its own database).

## Read-time dedupe patterns (the v1 tables are append logs)

Every raw table needs dedupe at read time. Replays are legal. The system is
at-least-once end to end. `checkpoint_limit 1024` bounds a crash replay at about
10 s of traffic. The v2 staging models MUST encode these patterns.

| table | pattern | notes |
|---|---|---|
| `udp.tickets_raw` | `FROM tickets_raw FINAL` or `argMax(col, event_id)` | ReplacingMergeTree ORDER BY event_id — dedupe is eventual, never automatic at read time |
| `udp.fast_decisions` | `argMax(decided_at, event_id)`-style pick per event_id, or `DISTINCT event_id` when any row is equivalent | MergeTree, ORDER BY (event_id, decided_at); crash replay can duplicate an event (measured ≤0.8% of rows) |
| `udp.model_decisions` | latest per `event_id` = `argMax(decided_at, decision_id)` grouped by event_id | one event CAN have several decision rows (replayed verdicts); `decision_id` itself is unique per row |
| `udp.alerts` | `uniqExact(decision_id) == count()` is an invariant — plain dedupe by decision_id, plus `fast:` prefix handling (fast-path keys are synthesized `fast:<event_id>`) | delivered vs suppressed split is a column, not a row family |
| `udp.app_users_cdc` | current state: `argMax(col, offset) GROUP BY id HAVING argMax(op, offset) != 'd'` | **offset, never landed_at** — a full replay re-lands old events with fresh landed_at, which would let stale snapshot rows win; the kafka offset is the stable changelog order. NEVER use a bare `WHERE op != 'd'` (this brings deleted ids back from earlier snapshot/insert rows). History views read the raw log as-is |

## Table inventory (v2 reads from here)

- `udp.tickets_raw` — raw tickets (event_id, user_id, text, created_at, ingested_at, schema_version, ingested_batch_ts)
- `udp.fast_decisions` — rule-tier verdicts (event_id, category, confidence, decided_at, ingested_batch_ts)
- `udp.model_decisions` — model-tier verdicts (decision_id, event_id, question_type, escalate/scam probability, degraded, answers_json, ingested_batch_ts)
- `udp.alerts` — trigger log (decision_id, event_id, channel, suppressed, created_at, ingested_batch_ts)
- `udp.app_users_cdc` — postgres app_users change log (id, email, plan, status, created_at, updated_at, op, position, offset, landed_at)

`ingested_batch_ts` and `landed_at` (DEFAULT now()) record landing provenance.
The pipelines never write them.

Schema definitions stay authoritative in this repo. [schemas.md](schemas.md)
defines the wire fields. [clickhouse/init.sql](../clickhouse/init.sql) and
[clickhouse/cdc.sql](../clickhouse/cdc.sql) define the DDL. We recommend that
the v2 repo vendor or link to these files, not fork them.

## Open questions (for the v2 repo to resolve)

- Freshness contract: how stale can marts be? (Stream refresh through
  ClickHouse materialized views, or scheduled dbt runs. The raw tables are
  append-only, so incremental dbt on ingest timestamps is natural.)
- Late and replayed rows: dbt incremental models need a replay-safe window
  (re-read the last N hours) because of the read-time dedupe contracts above.
- ClickHouse user model: one read-only `udp` role + `udp_marts` owner, or a dedicated role per consumer?
- answers_json (raw model output) — normalize into typed columns in staging, or keep as audit-only payload?
- Cost and retention: does v2 want longer retention on raw than the 7d topic
  retention implies? (Tables are unbounded. Topics are not.)
- Naming: `udp_marts` vs the legacy warehouse naming the batch lineage used pre-v1.
