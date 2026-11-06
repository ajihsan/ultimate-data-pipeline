"""Realtime Flink job for Mastodon stream redaction."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.clickhouse import ClickHouseSink
from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer, KafkaSource

from redactor import PIIRedactor


def create_mastodon_source(env: StreamExecutionEnvironment):
    """Create a Kafka source for Mastodon raw topic.

    Args:
        env: Flink stream execution environment.

    Returns:
        DataStream: Flink datastream containing raw Mastodon events.
    """
    return env.add_source(
        KafkaSource.builder()
        .set_bootstrap_servers("redpanda:9092")
        .set_topics("stream.mastodon.raw")
        .set_group_id("mastodon-flink-consumer")
        .set_starting_offsets(KafkaOffsetsInitializer.latest())
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )


def process_mastodon_stream(stream):
    """Apply PII redaction to a raw Mastodon stream.

    Args:
        stream: Source datastream with raw events.

    Returns:
        DataStream: Redacted event stream.
    """
    return stream.map(lambda value: redact_pii(value))


def redact_pii(raw_event: str) -> dict[str, Any]:
    """Redact PII fields from one raw Mastodon event payload.

    Args:
        raw_event: Raw JSON event as string.

    Returns:
        Dict[str, Any]: Redacted event payload for sink ingestion.
    """
    redactor = PIIRedactor()
    data = json.loads(raw_event)

    content_redacted = redactor.redact_text(data.get("content", ""))
    account_username = data.get("account_username", "")
    username_hash = redactor.hash_username(account_username)

    pii_detected = content_redacted != data.get("content", "")
    pii_types = ["content_pii"] if pii_detected else []

    return {
        "event_id": data.get("id", ""),
        "event_type": data.get("event_type", ""),
        "account_id": data.get("account_id", ""),
        "account_username_hash": username_hash,
        "content_redacted": content_redacted,
        "visibility": data.get("visibility", "public"),
        "sensitive": data.get("sensitive", False),
        "language": data.get("language"),
        "uri": data.get("uri", ""),
        "url": data.get("url", ""),
        "replies_count": data.get("replies_count", 0),
        "reblogs_count": data.get("reblogs_count", 0),
        "favourites_count": data.get("favourites_count", 0),
        "timestamp_ms": data.get("timestamp_ms", 0),
        "processed_at": datetime.now().isoformat(),
        "pii_detected": pii_detected,
        "pii_types": ",".join(pii_types) if pii_types else "",
    }


def create_clickhouse_sink(stream) -> None:
    """Attach ClickHouse sink to a redacted Mastodon stream.

    Args:
        stream: Redacted event datastream.
    """
    from pyflink.datastream.connectors.clickhouse import ClickHouseSinkOptions

    options = (
        ClickHouseSinkOptions.Builder()
        .with_url("jdbc:clickhouse://clickhouse:8123/default")
        .with_database("streaming")
        .with_table("staging_mastodon")
        .with_username("default")
        .with_password("")
        .build()
    )

    sink = ClickHouseSink.build(options)
    stream.add_sink(sink)


def main() -> None:
    """Run the Flink Mastodon redaction job."""
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)

    mastodon_stream = create_mastodon_source(env)
    processed_stream = process_mastodon_stream(mastodon_stream)

    processed_stream.print()

    env.execute("Mastodon Stream Processing with PII Redaction")


if __name__ == "__main__":
    main()
