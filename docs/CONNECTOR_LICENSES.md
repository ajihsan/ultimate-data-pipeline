# Connector licenses — why the CDC path is Conduit (bus CDC)

Status: verified 2026-10-13. This records the S7 CDC decision for
`app_users` → ClickHouse and the licensed alternative we rejected.

## Rejected: redpanda-connect `postgres_cdc` (enterprise-gated)

Redpanda Connect's `postgres_cdc` input (logical-replication CDC for the
connect pipelines we already run) is **not free software — it requires an
enterprise license**. Its docs page
(docs.redpanda.com → Connect → Components → Inputs → `postgres_cdc`) states:

> License: This component requires an enterprise license. You can either
> upgrade to an Enterprise Edition license, or generate a trial license key
> that's valid for 30 days.

The 30-day trial expires; a portfolio pipeline built on it dies after a
month (and the gated component has been permanently rejected upstream —
it will not be relicensed as BSL/free). So `postgres_cdc` is out for this
repo, despite the appeal of reusing the existing connect image.

## Chosen: Conduit bus CDC (Apache-2.0)

[Conduit](https://github.com/ConduitIO/conduit) is Apache-2.0 with **built-in
postgres and kafka connectors** bundled in the same binary (no extra
containers or registries needed on v0.20.0). It gives us the *bus-first*
shape the realtime design called for:

```
postgres app_users ──(conduit: snapshot + WAL/logrepl CDC)──► redpanda topic
                                                            `pg_changes`
`pg_changes` ──(connect `cdc` pipeline: bloblang → sql_insert)──►
                                                            udp.app_users_cdc
```

- Conduit owns the hard part: initial snapshot, logical replication slot +
  publication, resumable checkpoints (badger state volume `conduit-data`),
  and OpenCDC change events (`payload.before/after`, `op`).
- Redpanda keeps the changelog: replayable, inspectable with `rpk`, and the
  landing side is a plain connect pipeline (same shape as ingest/decide/
  notify — no new landing tech).
- Landing is idempotent at read time: `app_users_cdc` is a MergeTree append
  log; current state = `argMax(col, landed_at)` per id `WHERE op != 'd'`
  (see `connect/cdc.yaml` + `clickhouse/cdc.sql`).
- Pinned: `ghcr.io/conduitio/conduit:v0.20.0` — v0.20.0 is the first stable
  release fixing a SEV-0 Postgres snapshot→CDC stall present in v0.19.0
  (snapshot completes, CDC silently never starts); v0.19.0 and older must
  not be used for this pipeline. Note `conduitio/conduit` on Docker Hub is
  gone (404); images ship from GitHub Container Registry.

## Documented fallback: ClickHouse-native replication

If the bus ever becomes unwanted overhead, the sanctioned fallback is
CH-native replication: a `postgresql(...)` database/table engine over the
postgres DSN plus a materialized view refreshed on a short interval
(~5s). Kept as documentation only — it polls instead of tailing the WAL
(latency and load scale with row count), it is not replayable (no changelog
topic), and deletes/mutations are reconciled by full re-sync rather than
change events. The bus CDC path gives lower tail latency and a durable,
replayable change log; the native path remains the escape hatch.
