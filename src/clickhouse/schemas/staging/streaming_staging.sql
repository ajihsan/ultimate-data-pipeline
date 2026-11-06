CREATE DATABASE IF NOT EXISTS streaming;

CREATE TABLE IF NOT EXISTS streaming.staging_cdc_events
(
    _operation String,
    _timestamp Int64,
    _table String,
    _before String,
    _after String,
    _inserted_at DateTime DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (_table, _timestamp)
PARTITION BY toYYYYMMDD(_inserted_at);

CREATE TABLE IF NOT EXISTS streaming.staging_mastodon
(
    event_id String,
    event_type String,
    account_id String,
    account_username_hash String,
    content_redacted String,
    visibility String,
    sensitive Bool,
    language Nullable(String),
    uri String,
    url String,
    replies_count Int32,
    reblogs_count Int32,
    favourites_count Int32,
    timestamp_ms Int64,
    processed_at DateTime,
    pii_detected Bool,
    pii_types String,
    _inserted_at DateTime DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (event_id, timestamp_ms)
PARTITION BY toYYYYMMDD(_inserted_at);

CREATE TABLE IF NOT EXISTS streaming.staging_mastodon_raw
(
    event_id String,
    event_type String,
    account_id String,
    account_username String,
    content_raw String,
    visibility String,
    sensitive Bool,
    language Nullable(String),
    uri String,
    url String,
    replies_count Int32,
    reblogs_count Int32,
    favourites_count Int32,
    timestamp_ms Int64,
    created_at String,
    payload_raw String,
    _inserted_at DateTime DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (event_id, timestamp_ms)
PARTITION BY toYYYYMMDD(_inserted_at);

CREATE VIEW IF NOT EXISTS streaming.vw_mastodon_raw_vs_redacted AS
SELECT
    coalesce(raw.event_id, redacted.event_id) AS event_id,
    raw.account_id AS raw_account_id,
    redacted.account_id AS redacted_account_id,
    raw.account_username,
    raw.content_raw,
    redacted.content_redacted,
    redacted.pii_detected,
    redacted.pii_types,
    raw.timestamp_ms AS raw_timestamp_ms,
    redacted.timestamp_ms AS redacted_timestamp_ms
FROM streaming.staging_mastodon_raw AS raw
FULL OUTER JOIN streaming.staging_mastodon AS redacted
    ON raw.event_id = redacted.event_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS streaming.mv_mastodon_stats
ENGINE = SummingMergeTree()
ORDER BY (account_id, toDate(timestamp_ms / 1000))
AS SELECT
    account_id,
    toDate(timestamp_ms / 1000) AS event_date,
    count() AS total_events,
    sum(replies_count) AS total_replies,
    sum(reblogs_count) AS total_reblogs,
    sum(favourites_count) AS total_favourites,
    countIf(pii_detected = true) AS events_with_pii,
    groupUniqArray(language) AS languages
FROM streaming.staging_mastodon
GROUP BY account_id, toDate(timestamp_ms / 1000);
