# Ultimate Data Pipeline — Realtime

Synthetic app-user ticket stream → rule fast path with a Laya decision-model fallback → ClickHouse → Telegram/Discord triggers, with Postgres CDC flowing through a Conduit bus. The realtime path replaces the legacy JVM stack (Debezium/Kafka/Flink/Mastodon producer) with a config-first Connect deployment on Redpanda.

**Status:** v1 work in progress on branch `feat/realtime-v1-redpanda`.

## Why lightweight

- **Config-first Connect:** declarative connector JSON on Redpanda instead of custom JVM producers and processors.
- **Cascade decisions:** a cheap rule fast path handles most tickets; the Laya model only sees what rules can't — see [docs/feasibility.md](docs/feasibility.md).
- **Local model runtime:** Laya runs on consumer ROCm via TheRock wheels (RX 6600 XT), no cloud inference required.

## Repo scope

Realtime only. Batch/dbt analytics moves to a separate repo at v2 and will not live here.

## Toolchain quickstart

```bash
uv sync --inexact   # install deps; --inexact preserves the .venv model runtime (`make sync`)
make lint           # ruff check
make test           # pytest
```

Full runbook (compose up, demo, load test) lands in S8 — `make demo` / `make load-test` are honest placeholders until then.
