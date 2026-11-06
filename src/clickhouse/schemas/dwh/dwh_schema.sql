CREATE DATABASE IF NOT EXISTS dwh;

CREATE TABLE IF NOT EXISTS dwh.dim_accounts
(
    account_id String,
    account_username_hash String,
    first_seen_at DateTime,
    last_seen_at DateTime,
    _updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_updated_at)
ORDER BY account_id;

CREATE TABLE IF NOT EXISTS dwh.fact_mastodon_events
(
    event_id String,
    account_id String,
    event_type String,
    visibility String,
    sensitive Bool,
    language Nullable(String),
    replies_count Int32,
    reblogs_count Int32,
    favourites_count Int32,
    pii_detected Bool,
    event_timestamp DateTime,
    processed_at DateTime,
    _inserted_at DateTime DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (account_id, event_timestamp, event_id)
PARTITION BY toYYYYMMDD(event_timestamp);

CREATE MATERIALIZED VIEW IF NOT EXISTS dwh.mv_fact_events_daily
ENGINE = SummingMergeTree()
ORDER BY (account_id, event_date, event_type)
AS SELECT
    account_id,
    toDate(event_timestamp) AS event_date,
    event_type,
    visibility,
    count() AS event_count,
    sum(replies_count) AS total_replies,
    sum(reblogs_count) AS total_reblogs,
    sum(favourites_count) AS total_favourites,
    countIf(pii_detected = true) AS pii_events_count
FROM dwh.fact_mastodon_events
GROUP BY account_id, toDate(event_timestamp), event_type, visibility;
