# ADR 0001: Use Conduit for Postgres CDC

- Status: accepted
- Date: 2026-10-13
- Decision: move `app_users` changes to ClickHouse through a Conduit bus.
- Rejected alternative: the Redpanda Connect `postgres_cdc` input.

## Rejected: redpanda-connect `postgres_cdc` (enterprise-gated)

Redpanda Connect's `postgres_cdc` input (logical-replication CDC for the
connect pipelines we already run) is **not free software — it requires an
enterprise license**. Its docs page
(docs.redpanda.com → Connect → Components → Inputs → `postgres_cdc`) states:

> License: This component requires an enterprise license. You can either
> upgrade to an Enterprise Edition license, or generate a trial license key
> that's valid for 30 days.

The 30-day trial expires. A production pipeline cannot depend on it. As of
2026-10-13, Redpanda's documentation states that `postgres_cdc` requires an
enterprise license and does not list a free path. `postgres_cdc` is out for
this repo, even though it reuses the existing Connect image.

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

- Conduit handles the initial snapshot, the logical replication slot and
  publication, resumable checkpoints (badger state volume `conduit-data`),
  and OpenCDC change events (`payload.before/after`, `op`).

- Redpanda keeps the changelog. The log is replayable and inspectable with
  `rpk`. The landing side is a plain Connect pipeline with the same shape as
  ingest, decide, and notify.

- Landing is idempotent at read time. `app_users_cdc` is a MergeTree append
  log. Current state is `argMax(col, offset)` per id with
  `HAVING argMax(op, offset) != 'd'`, keyed on the Kafka offset, never
  `landed_at`. Tombstones are dropped in the HAVING, not a plain WHERE. A plain
  `WHERE op != 'd'` filter brings deleted ids back. See `connect/cdc.yaml` and
  `clickhouse/cdc.sql`.

- Pinned: `ghcr.io/conduitio/conduit:v0.20.0`. v0.20.0 is the first stable
  release that fixes a SEV-0 Postgres snapshot-to-CDC stall in v0.19.0. In
  v0.19.0, the snapshot completes and CDC never starts. Do not use v0.19.0 or
  older for this pipeline. The `conduitio/conduit` image on Docker Hub is gone
  (404). Images ship from GitHub Container Registry.

## Documented fallback: ClickHouse-native replication

If the bus becomes unwanted overhead, the fallback is ClickHouse-native
replication. It uses a `postgresql(...)` database/table engine over the
Postgres DSN and a materialized view refreshed every ~5 s. This is
documentation only. It polls instead of tailing the WAL, so latency and load
scale with row count. It is not replayable because it has no changelog topic.
It reconciles deletes and mutations by full re-sync, not by change events.

The bus CDC path gives lower tail latency and a durable, replayable change log.
The native path remains the fallback.
