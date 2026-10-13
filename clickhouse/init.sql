-- S3 landed tables (idempotent; mounted into /docker-entrypoint-initdb.d).
-- S7 owns the CDC mirror tables (clickhouse/cdc.sql) — do not add them here.
-- Timestamps are pinned to UTC so ingest stays deterministic regardless of
-- container/server timezone.

CREATE DATABASE IF NOT EXISTS udp;

-- Raw landed tickets. ReplacingMergeTree dedupe is eventual: readers MUST
-- dedupe at read time (FINAL / argMax by event_id).
CREATE TABLE IF NOT EXISTS udp.tickets_raw
(
    event_id          String,
    user_id           String,
    text              String,
    created_at        DateTime64(3, 'UTC'),
    ingested_at       DateTime64(3, 'UTC'),
    schema_version    String,
    ingested_batch_ts DateTime64(3, 'UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree
ORDER BY event_id
COMMENT 'raw landed tickets; ReplacingMergeTree dedupe is eventual — readers MUST dedupe at read time (FINAL/argMax(event_id)) — see docs/v2-batch.md (v2)';

CREATE TABLE IF NOT EXISTS udp.fast_decisions
(
    event_id          String,
    category          LowCardinality(String),
    confidence        Float32,
    decided_at        DateTime64(3, 'UTC'),
    schema_version    String,
    ingested_batch_ts DateTime64(3, 'UTC') DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (event_id, decided_at);

CREATE TABLE IF NOT EXISTS udp.model_decisions
(
    decision_id          String,
    event_id             String,
    question_type        LowCardinality(String),
    escalate_probability Float32,
    scam_probability     Float32,
    degraded             Bool DEFAULT false,
    decided_at           DateTime64(3, 'UTC'),
    schema_version       String,
    ingested_batch_ts    DateTime64(3, 'UTC') DEFAULT now(),
    answers_json         String
)
ENGINE = MergeTree
ORDER BY (decision_id, decided_at);

CREATE TABLE IF NOT EXISTS udp.alerts
(
    decision_id       String,
    event_id          String,
    channel           LowCardinality(String),
    suppressed        Bool DEFAULT false,
    created_at        DateTime64(3, 'UTC'),
    schema_version    String,
    ingested_batch_ts DateTime64(3, 'UTC') DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (decision_id, created_at);
