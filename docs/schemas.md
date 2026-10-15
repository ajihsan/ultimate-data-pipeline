# Domain schemas

Every payload on a Kafka topic or ClickHouse row is one of the four pydantic
models in `src/udp/schemas.py` plus the mapping-added `answers_json`
serialization field on model_decisions (real topic payloads are therefore not
bare-model-validatable), stamped `schema_version` (currently `1.0.0`)
with `extra="forbid"` — unknown fields are a producer bug. For the full
machine-readable schema, run `python -c "from udp.schemas import Ticket;
import json; print(json.dumps(Ticket.model_json_schema()))"` (same for the
other models).

**Validation status:** the generator produces pydantic-validated `Ticket`
payloads (`--dry-run` output too). Decisions and alerts are produced by the
bloblang mappings in `connect/decide.yaml` / `connect/notify.yaml` — their
shape is the contract documented here and enforced by the ClickHouse table
definitions plus the smoke/chaos SQL asserts, not by pydantic at runtime.

**Latency contract:** producers set `Ticket.ingested_at` when the ticket
enters the stream; deciders stamp `decided_at`. End-to-end latency is
`p95 = decided_at - ingested_at`.

The four `clickhouse/init.sql` tables also carry an `ingested_batch_ts
DateTime64(3) DEFAULT now()` landing-provenance column (never written by the
pipelines); `udp.app_users_cdc`'s equivalent is `landed_at`.

## Ticket — topic `tickets` → `udp.tickets_raw`

| field | type | notes |
|---|---|---|
| `event_id` | str | uuid4 hex; the correlation key joining ticket, decisions, alerts (required) |
| `schema_version` | str | default `"1.0.0"` |
| `text` | str | verbatim ticket text, non-empty (required) |
| `user_id` | str | Kafka partition key → per-user ordering (required) |
| `created_at` | datetime | producer-side creation moment, tz-aware (required) |
| `ingested_at` | datetime | producer-side stream-entry moment, tz-aware (required) |
| `category_hint` | str \| null | generator-only metadata; stripped before publish |

```json
{
  "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
  "schema_version": "1.0.0",
  "text": "my app keeps logging me out every 2 minutes, been paying premium for nothing!!",
  "user_id": "usr_kgarcia84",
  "created_at": "2026-10-13T09:12:41+00:00",
  "ingested_at": "2026-10-13T09:12:42.113000+00:00"
}
```

## FastDecision — `udp.fast_decisions` + topic `fast_decisions`

| field | type | notes |
|---|---|---|
| `event_id` | str | same ticket as the decision (required) |
| `schema_version` | str | default `"1.0.0"` |
| `category` | str | one of `complaint`, `scam_report`, `billing`, `bug`, `access`, `feature_request` (required) |
| `confidence` | float | rule certainty in [0, 1] (required) |
| `decided_at` | datetime | fast-path verdict moment, tz-aware (required) |

```json
{
  "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
  "schema_version": "1.0.0",
  "category": "scam_report",
  "confidence": 0.94,
  "decided_at": "2026-10-13T09:12:42.389000+00:00"
}
```

## ModelDecision — `udp.model_decisions` + topic `model_decisions`

| field | type | notes |
|---|---|---|
| `event_id` | str | same ticket as the decision (required) |
| `schema_version` | str | default `"1.0.0"` |
| `decision_id` | str | uuid4 hex; the notify dedupe key (required) |
| `question_type` | str | which `/v1/systemone` head answered: `choice`, `score`, or `noul` (required) |
| `answers` | object | raw `/v1/systemone` answers block, preserved verbatim (required) |
| `escalate_probability` | float | in [0, 1] (required) |
| `scam_probability` | float | in [0, 1] (required) |
| `degraded` | bool | true = model endpoint failed, fallback verdict recorded (default false) |
| `decided_at` | datetime | verdict moment, tz-aware (required) |
| `answers_json` | str | stringified audit copy of `answers`; also the `model_decisions` CH column (mapping-added in `decide.yaml`, not a pydantic field) |

`answers` on the wire — keys per question id, exactly what
`decide.yaml` maps (`answers.escalate_now.noul`,
`answers.category.probabilities.scam_report`):

```json
{
  "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
  "schema_version": "1.0.0",
  "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
  "question_type": "choice",
  "answers": {
    "escalate_now": {"type": "noul", "noul": 0.91, "confidence": 0.91},
    "category": {
      "type": "choice",
      "choice": "scam_report",
      "probabilities": {
        "complaint": 0.02, "scam_report": 0.78, "billing": 0.03,
        "bug": 0.02, "access": 0.02, "feature_request": 0.13
      },
      "confidence": 0.78
    }
  },
  "escalate_probability": 0.91,
  "scam_probability": 0.78,
  "degraded": false,
  "decided_at": "2026-10-13T09:12:43.640000+00:00"
}
```

On a degraded fallback, `answers` is `{"error": <message>}` and both
probabilities are 0.

## AlertEvent — `udp.alerts` (ClickHouse-only, never a Kafka payload)

| field | type | notes |
|---|---|---|
| `event_id` | str | same ticket as the alert (required) |
| `schema_version` | str | default `"1.0.0"` |
| `decision_id` | str | originating verdict; fast path synthesizes `fast:<event_id>` (required) |
| `channel` | str | `telegram`, `discord`, the fan-out list `telegram,discord`, or `mock` (the value recorded when no chat provider is configured: `log` or `mock` mode) (required) |
| `suppressed` | bool | true = rate limiter dropped delivery, row still recorded (default false) |
| `created_at` | datetime | alert attempt moment, tz-aware (required) |

```json
{
  "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
  "schema_version": "1.0.0",
  "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
  "channel": "telegram,discord",
  "suppressed": false,
  "created_at": "2026-10-13T09:12:43.812000+00:00"
}
```
