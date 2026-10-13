-- S7 — CDC mirror of postgres `app_users` (conduit bus CDC: postgres →
-- topic `pg_changes` → the connect `cdc` pipeline lands here).
-- Mounted via initdb alongside init.sql; IF NOT EXISTS keeps it idempotent
-- on any re-run (S3's init.sql deliberately owns no cdc tables).

-- ENGINE CHOICE — MergeTree, not ReplacingMergeTree: this table keeps the
-- full change history (one row per event), and replays (conduit ack gaps or
-- a consumer-offset reset on group `udp-cdc`) legitimately re-append the
-- same event — there is no unique key to replace on, because a re-landed
-- row differs by landed_at. Dedupe is therefore a READ-TIME contract:
--
--   CURRENT STATE of an app_user = the event with the greatest `offset`
--   (the changelog's stable total order — single-partition topic, preserved
--   verbatim across replays) per id; ids whose latest op is 'd' are dropped:
--
--     SELECT id, argMax(email, offset) AS email, argMax(plan, offset) AS plan,
--            argMax(status, offset) AS status
--     FROM udp.app_users_cdc
--     GROUP BY id
--     HAVING argMax(op, offset) != 'd'
--
--   argMax keyed on offset (not landed_at): a full replay re-lands old
--   events with FRESH landed_at, which would let stale snapshot rows beat
--   newer updates; the offset is the event's original position in the
--   changelog and never changes on replay. landed_at stays as landing
--   provenance only.
--
-- DELETE SEMANTICS: conduit encodes the operation; a DELETE lands a
-- tombstone row (op='d') rather than removing anything — under postgres'
-- default replica identity the delete event carries only the key, so
-- tombstones have empty email/plan/status and NULL timestamps (Nullable
-- columns below). READERS MUST DROP ids whose latest op is 'd' for current
-- state; the HAVING argMax(op, offset) != 'd' above is not optional — a
-- plain WHERE op != 'd' filter resurrects deleted ids from their earlier
-- snapshot/insert rows.

CREATE TABLE IF NOT EXISTS udp.app_users_cdc
(
    id         Int64,
    email      String,
    plan       String,
    status     String,
    created_at Nullable(DateTime64(3, 'UTC')),
    updated_at Nullable(DateTime64(3, 'UTC')),
    op         LowCardinality(String), -- snapshot | c | u | d
    position   String,                 -- decoded OpenCDC position (type + last_lsn)
    offset     UInt64,                 -- kafka offset: stable changelog order
    landed_at  DateTime64(3, 'UTC') DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (id, offset, op)
COMMENT 'cdc mirror of postgres app_users (conduit pg_changes); MergeTree append log — CURRENT STATE is read-time: argMax(col, offset) per id HAVING argMax(op, offset) != d (tombstones) — see connect/cdc.yaml';
