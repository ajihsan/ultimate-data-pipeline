"""Ultimate Data Pipeline — realtime ticket ingest, decision, and alerting.

Ingests a synthetic app-user ticket stream from Redpanda (Kafka API), applies a
two-stage cascade decision (rule fast path with a Laya decision-model fallback),
persists results to ClickHouse, and triggers Telegram/Discord notifications.
Postgres CDC flows through a Conduit bus; the model server is wired in S5.
"""

__version__ = "0.1.0"
