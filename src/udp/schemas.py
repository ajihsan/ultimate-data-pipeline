"""Typed domain contracts for the realtime ticket pipeline.

``Ticket`` is the only model the runtime validates. The generator produces it,
and the ingest pipeline mirrors its required fields in Bloblang. The other
three models (FastDecision, ModelDecision, AlertEvent) are contract fixtures:
the Connect pipelines build the same shapes as JSON, and the tests validate
these examples. Runtime code does not import them.

Every payload carries a ``schema_version`` envelope field. ``model_decisions``
adds the mapping-serialized ``answers_json`` field, so a real topic payload is
not bare-model-validatable.

Latency contract: producers set ``Ticket.ingested_at`` when the ticket enters
the stream, and deciders stamp ``decided_at``. End-to-end p95 is
``decided_at - ingested_at``. Keep both clocks producer-side and aware.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0.0"
"""Current wire schema version stamped on every payload by default."""

Category = Literal["complaint", "scam_report", "billing", "bug", "access", "feature_request"]
"""Ticket categories the rule fast path can assign."""

QuestionType = Literal["choice", "score", "noul"]
"""Question types of the ``POST /v1/systemone`` decision head."""

Channel = Literal["log", "mock", "telegram", "discord", "telegram,discord"]
"""Alert delivery channel as recorded in udp.alerts. The value names the real
sink; ``log`` means the alert was logged and no HTTP request was made."""

CATEGORIES: tuple[Category, ...] = (
    "complaint",
    "scam_report",
    "billing",
    "bug",
    "access",
    "feature_request",
)
"""Canonical category order shared by the schema docs and the generator."""


def _reject_blank(value: str, field_name: str) -> str:
    """Validate that an identifier field is a non-empty, non-blank string.

    Args:
        value: The raw field value.
        field_name: Field name used in the error message.

    Returns:
        The validated value, unchanged.

    Raises:
        ValueError: If the value is empty or whitespace-only.
    """
    if not value.strip():
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _require_aware(value: datetime, field_name: str) -> datetime:
    """Validate that a timestamp is timezone-aware.

    Args:
        value: The raw timestamp.
        field_name: Field name used in the error message.

    Returns:
        The validated value, unchanged.

    Raises:
        ValueError: If the timestamp is naive (no timezone attached).
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"{field_name} must be timezone-aware (UTC recommended)"
        raise ValueError(msg)
    return value


class DomainEnvelope(BaseModel):
    """Envelope fields shared by every wire payload.

    All payload models inherit ``event_id`` (the end-to-end correlation key) and
    ``schema_version`` (the wire format version), plus the blanket
    ``extra="forbid"`` contract: unknown fields on a topic are a producer bug,
    not something to swallow silently.

    Attributes:
        event_id: Unique ticket-event identifier (uuid4 hex) that ties the
            ticket and every decision/alert derived from it together.
        schema_version: Wire schema version, e.g. ``"1.0.0"``.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        description=(
            "Unique ticket-event identifier (uuid4 hex); the correlation key "
            "joining the ticket, its decisions, and its alerts."
        ),
    )
    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Wire schema version of this payload type.",
    )

    @field_validator("event_id")
    @classmethod
    def _event_id_non_empty(cls, value: str) -> str:
        """Reject empty or whitespace-only event ids."""
        return _reject_blank(value, "event_id")


class Ticket(DomainEnvelope):
    """An app-user ticket as produced onto the ``tickets`` Kafka topic.

    ``text`` is the verbatim user-submitted ticket text; everything downstream
    (rule fast path, model fallback, alerts) keys off it. ``category_hint`` is
    generator-only metadata: real events may carry ``None``, and the generator
    strips it before publishing (see ``WIRE_EXCLUDED_FIELDS``) while keeping it
    in ``--dry-run`` output for testability.

    ``ingested_at`` is set producer-side when the ticket enters the stream.

    Attributes:
        event_id: Inherited correlation key.
        schema_version: Inherited wire schema version.
        text: Verbatim user ticket text (non-empty).
        category_hint: Generator-only routing metadata; ``None`` for real
            events and for ambiguous tickets. Never consumed downstream.
        user_id: Stable app-user identifier; used as the Kafka partition key
            so a user's tickets land on one partition in order.
        created_at: Producer-side moment the user created the ticket; may lag
            ``ingested_at`` by a small backlog jitter.
        ingested_at: Producer-side moment the ticket entered the stream.
    """

    text: str = Field(min_length=1, description="Verbatim user-submitted ticket text.")
    category_hint: str | None = Field(
        default=None,
        description=(
            "Generator-only routing metadata (may be None in real events); "
            "stripped before publish, kept in dry-run output."
        ),
    )
    user_id: str = Field(
        description=(
            "Stable app-user identifier; the Kafka partition key for ordered per-user consumption."
        ),
    )
    created_at: datetime = Field(
        description=("Producer-side moment the user created the ticket (UTC, aware)."),
    )
    ingested_at: datetime = Field(
        description="Producer-side moment the ticket entered the stream (UTC, aware).",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "text": (
                        "my app keeps logging me out every 2 minutes, "
                        "been paying premium for nothing!!"
                    ),
                    "category_hint": "complaint",
                    "user_id": "usr_kgarcia84",
                    "created_at": "2026-10-13T09:12:41+00:00",
                    "ingested_at": "2026-10-13T09:12:42.113000+00:00",
                    "schema_version": "1.0.0",
                }
            ]
        },
    )

    WIRE_EXCLUDED_FIELDS: ClassVar[frozenset[str]] = frozenset({"category_hint"})
    """Fields stripped from the payload before it is published to Kafka."""

    @field_validator("created_at", "ingested_at")
    @classmethod
    def _timestamps_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps (they break latency arithmetic)."""
        return _require_aware(value, "timestamp")

    @field_validator("user_id")
    @classmethod
    def _user_id_non_empty(cls, value: str) -> str:
        """Reject empty or whitespace-only user ids."""
        return _reject_blank(value, "user_id")

    def wire_json(self) -> str:
        """Serialize the payload exactly as it is published to Kafka.

        Returns:
            JSON string with ``WIRE_EXCLUDED_FIELDS`` removed, i.e. the body
            that lands on the topic (and later in ClickHouse).
        """
        return self.model_dump_json(exclude=set(self.WIRE_EXCLUDED_FIELDS))


class FastDecision(DomainEnvelope):
    """Rule fast-path verdict for one ticket.

    The cascade's cheap deterministic stage assigns a category straight from
    pattern certainty and never calls the model. ``confidence`` carries the
    rule's certainty in ``[0, 1]`` so the cascade can be tuned and audited.

    ``decided_at`` is stamped when the rule verdict is recorded.

    Attributes:
        event_id: Inherited correlation key (same ticket as the decision).
        schema_version: Inherited wire schema version.
        category: Rule-assigned category, one of ``CATEGORIES``.
        confidence: Rule certainty in ``[0, 1]``.
        decided_at: Moment the fast path recorded its verdict (UTC, aware).
    """

    category: Category = Field(description="Rule-assigned ticket category.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Rule certainty in [0, 1] derived from pattern match strength.",
    )
    decided_at: datetime = Field(
        description="Producer-side moment the fast-path verdict was recorded (UTC, aware).",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "category": "scam_report",
                    "confidence": 0.94,
                    "decided_at": "2026-10-13T09:12:42.389000+00:00",
                    "schema_version": "1.0.0",
                }
            ]
        },
    )

    @field_validator("decided_at")
    @classmethod
    def _decided_at_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps (they break latency arithmetic)."""
        return _require_aware(value, "decided_at")


class ModelDecision(DomainEnvelope):
    """Laya decision-model verdict for a flagged ticket.

    Emitted for every ticket the fast path flags. The raw ``/v1/systemone``
    answers block is preserved verbatim in ``answers`` for audit.
    ``decision_id`` comes from the ticket, so a replay produces the same alert
    key and one ticket gets at most one model alert.

    Latency contract: ``decided_at`` is stamped when the model verdict (or its
    degraded fallback) is recorded. End-to-end p95 is
    ``decided_at - ingested_at``.

    Attributes:
        event_id: Inherited correlation key (same ticket as the decision).
        schema_version: Inherited wire schema version.
        decision_id: Stable alert key, ``model:<event_id>``.
        question_type: Primary decision head. The pipeline writes ``choice``
            and also asks ``escalate_now``.
        answers: Raw ``/v1/systemone`` answers block, preserved verbatim.
        escalate_probability: Calibrated escalation probability in ``[0, 1]``.
        scam_probability: Calibrated scam probability in ``[0, 1]``.
        degraded: ``True`` when the model endpoint failed and a fallback
            verdict was recorded instead.
        decided_at: Moment the verdict was recorded (UTC, aware).
    """

    decision_id: str = Field(
        description="Stable alert dedupe key derived from the ticket: model:<event_id>.",
    )
    question_type: QuestionType = Field(
        description="Primary head; the pipeline writes choice and also asks escalate_now.",
    )
    answers: dict[str, Any] = Field(
        description="Raw /v1/systemone answers block, preserved verbatim for audit.",
    )
    escalate_probability: float = Field(
        ge=0.0,
        le=1.0,
        description="Calibrated escalation probability in [0, 1].",
    )
    scam_probability: float = Field(
        ge=0.0,
        le=1.0,
        description="Calibrated scam probability in [0, 1].",
    )
    degraded: bool = Field(
        default=False,
        description=(
            "True when the model endpoint failed and a fallback verdict was recorded instead."
        ),
    )
    decided_at: datetime = Field(
        description="Producer-side moment the model verdict was recorded (UTC, aware).",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "decision_id": "model:9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "question_type": "choice",
                    "answers": {
                        "escalate_now": {"type": "noul", "noul": 0.91, "confidence": 0.91},
                        "category": {
                            "type": "choice",
                            "choice": "scam_report",
                            "probabilities": {
                                "complaint": 0.02,
                                "scam_report": 0.78,
                                "billing": 0.03,
                                "bug": 0.02,
                                "access": 0.02,
                                "feature_request": 0.13,
                            },
                            "confidence": 0.78,
                        },
                    },
                    "escalate_probability": 0.91,
                    "scam_probability": 0.78,
                    "degraded": False,
                    "decided_at": "2026-10-13T09:12:43.640000+00:00",
                    "schema_version": "1.0.0",
                }
            ]
        },
    )

    @field_validator("decision_id")
    @classmethod
    def _decision_id_non_empty(cls, value: str) -> str:
        """Reject empty or whitespace-only decision ids."""
        return _reject_blank(value, "decision_id")

    @field_validator("decided_at")
    @classmethod
    def _decided_at_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps (they break latency arithmetic)."""
        return _require_aware(value, "decided_at")


class AlertEvent(DomainEnvelope):
    """One alert attempt after a decision.

    ``suppressed=True`` records a rate-limit drop. ``suppressed=False`` means
    the alert was not rate-limited; it does not prove delivery. Delivery
    failures are counted in ``notify_delivery_failed_total``.

    Attributes:
        event_id: Inherited correlation key (same ticket as the alert).
        schema_version: Inherited wire schema version.
        decision_id: ``fast:<event_id>`` or ``model:<event_id>``; the dedupe
            key and the join to the decision row.
        channel: Delivery channel: ``log``, ``mock``, ``telegram``, ``discord``,
            or the fan-out list ``telegram,discord``.
        suppressed: ``True`` when the rate limiter dropped the alert but the
            drop was still recorded.
        created_at: Producer-side moment the alert was attempted (UTC, aware).
    """

    decision_id: str = Field(
        description="Originating decision key: fast:<event_id> or model:<event_id>.",
    )
    channel: Channel = Field(
        description=(
            "Delivery channel: log, mock, telegram, discord, or the fan-out list telegram,discord."
        ),
    )
    suppressed: bool = Field(
        default=False,
        description="True when the rate limiter dropped the alert (drop-and-record).",
    )
    created_at: datetime = Field(
        description="Producer-side moment the alert was attempted (UTC, aware).",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "decision_id": "model:9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "channel": "mock",
                    "suppressed": False,
                    "created_at": "2026-10-13T09:12:43.812000+00:00",
                    "schema_version": "1.0.0",
                }
            ]
        },
    )

    @field_validator("decision_id")
    @classmethod
    def _decision_id_non_empty(cls, value: str) -> str:
        """Reject empty or whitespace-only decision ids."""
        return _reject_blank(value, "decision_id")

    @field_validator("created_at")
    @classmethod
    def _created_at_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps (they break latency arithmetic)."""
        return _require_aware(value, "created_at")
