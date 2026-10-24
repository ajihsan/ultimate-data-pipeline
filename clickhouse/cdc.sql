-- CDC mirror of postgres app_users (conduit -> pg_changes -> cdc pipeline).
-- Mounted via initdb alongside init.sql; IF NOT EXISTS keeps it idempotent.

-- MergeTree append log: replays legitimately re-append events. CURRENT STATE
-- is a read-time contract:
--
--   SELECT id, argMax(email, offset), argMax(plan, offset), argMax(status, offset)
--   FROM udp.app_users_cdc GROUP BY id HAVING argMax(op, offset) != 'd'
--
-- Key on `offset`, never `landed_at`: a replay re-stamps landed_at. Drop ids
-- whose latest op is 'd'; a plain `WHERE op != 'd'` brings deleted ids back.
-- DELETE tombstones have empty strings and NULL timestamps because the default
-- replica identity sends only the key.

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
