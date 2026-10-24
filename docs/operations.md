# Operations

## Restart semantics

`docker compose kill` sends SIGKILL. Compose v2 does not restart the container
in the observed window. Recovery is explicit:
`docker compose up -d decide notify`. The chaos script does this.

On restart:

1. The franz consumer group stays in `PreparingRebalance` for about 40 s. The
   45 s `session.timeout.ms` must expire before the coordinator evicts the dead
   member.
2. The backlog drains at about 200–300 messages/s. The ~6k messages here took
   about 40 s.
3. While inflow continues, the committed offset moves between about 110 and 610
   behind the stream. This is the input prefetch buffer, not real backlog. It
   drains to 0 when inflow stops. Delivery latency is unaffected (13 ms p95).

`docker compose stop` is graceful and avoids the rebalance delay.

## Degraded mode

A model failure stamps the ModelDecision row `degraded=true` with
`escalate_probability = scam_probability = 0`. The failure can be a timeout, a
transport error, 429/503 after retries, or a 200 with an unreadable answers
shape. Degraded rows are terminal in v1: p=0 never qualifies, so model-tier
alerts are down until the endpoint recovers. The fast path continues to make
decisions, and the rule-tier scam alerts continue to send.

Detect it:

```bash
curl -s http://127.0.0.1:4197/metrics | grep model_degraded_total
```

Or query the table:

```sql
SELECT count() FROM udp.model_decisions
WHERE degraded AND decided_at > now() - INTERVAL 5 MINUTE
```

## Delivery latency

The notify pipeline uses one worker for dedupe, rate limit, and delivery. A
slow chat provider can delay later alerts. Each POST has a 3 s timeout and 2
retries, so the worst case is near 9 s per provider. Monitor
`notify_delivery_failed_total` and the notify consumer lag. If the delay is
too high, split delivery into a second pipeline.

## Read-time dedupe contracts

The tables are append logs. Replays are legal and bounded. Readers must dedupe.

- `udp.tickets_raw`: ReplacingMergeTree. Use `SELECT DISTINCT event_id`,
  `argMax`, or `FINAL`.

- `udp.fast_decisions` and `udp.model_decisions`: MergeTree with no unique key.
  `checkpoint_limit 1024` bounds a crash replay to about 10 s of traffic.
  Expect a thin duplicate tail after a crash (measured ≤0.8%). Dedupe by
  `event_id` at read time.

- `udp.alerts`: the key is `decision_id` (`fast:<event_id>` or
  `model:<event_id>`). `count() == uniqExact(decision_id)` is an invariant.
  One ticket produces at most one alert. `count() == uniqExact(event_id)` also
  holds.

- `udp.app_users_cdc`: current state is `argMax(col, offset)` per id with
  `HAVING argMax(op, offset) != 'd'`. A plain `op != 'd'` filter brings deleted
  ids back. See `clickhouse/cdc.sql`.

## Cold start

The first franz fetch after a pipeline start takes about 2–4.4 s for chunk
warm-up. The first decisions of a fresh run arrive late. This is harmless. Do
not tune it.

## Topic creation

The one-shot `redpanda-init` service creates the topics. If you add a topic
and keep an existing volume, run `docker compose up -d redpanda-init` or create
the topic with `rpk`. The `pg_changes` topic must have one partition.

## Ports and retention

qBittorrent uses host port 8080 on this box, so the Console uses 8085. All host
ports bind to 127.0.0.1. The topics keep 7 days. The ClickHouse tables are
unbounded. Monitor disk use and set a TTL before production.
