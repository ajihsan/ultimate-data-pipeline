"""Typed domain contracts for the realtime ticket pipeline.

Every payload that crosses a Kafka topic or lands in a ClickHouse row is one of
the four models in this module, and each carries a ``schema_version`` envelope
field so the wire format can evolve without silently breaking consumers.

Latency measurement contract: producers set ``Ticket.ingested_at`` when the
ticket enters the stream, and deciders stamp ``decided_at`` on their decision
payloads. End-to-end latency is measured as ``p95 = decided_at - ingested_at``
across the fast path and the model path alike, so keep both clocks producer-side
and timezone-aware.
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
"""Question types of the ``POST /v1/systemone`` decision head (S0 contract)."""

Channel = str
"""Alert delivery channel as recorded in udp.alerts: ``telegram``,
``discord``, the fan-out list ``telegram,discord``, or ``mock`` (the log-mode
value — alerts are ClickHouse-only rows, never a validated wire payload)."""

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

    Latency measurement contract: ``ingested_at`` is set producer-side at the
    moment the ticket enters the stream; p95 latency is measured end-to-end as
    ``p95 = decided_at - ingested_at``.

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
            Latency measurement contract: ``p95 = decided_at - ingested_at``.
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
        description=(
            "Producer-side moment the ticket entered the stream (UTC, aware). "
            "Latency measurement contract: p95 = decided_at - ingested_at."
        ),
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

    Latency measurement contract: ``decided_at`` is stamped when the rule
    verdict is recorded; p95 latency is measured end-to-end as
    ``p95 = decided_at - ingested_at``.

    Attributes:
        event_id: Inherited correlation key (same ticket as the decision).
        schema_version: Inherited wire schema version.
        category: Rule-assigned category, one of ``CATEGORIES``.
        confidence: Rule certainty in ``[0, 1]``.
        decided_at: Moment the fast path recorded its verdict (UTC, aware).
            Latency measurement contract: ``p95 = decided_at - ingested_at``.
    """

    category: Category = Field(description="Rule-assigned ticket category.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Rule certainty in [0, 1] derived from pattern match strength.",
    )
    decided_at: datetime = Field(
        description=(
            "Producer-side moment the fast-path verdict was recorded (UTC, "
            "aware). Latency measurement contract: p95 = decided_at - ingested_at."
        ),
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

    Emitted by the model stage for every ticket the fast path flags. The raw
    ``/v1/systemone`` answers block is preserved verbatim in ``answers`` for
    audit; ``decision_id`` is the per-verdict dedupe key used by the alert
    trigger so one verdict produces at most one alert.

    Latency measurement contract: ``decided_at`` is stamped when the model
    verdict (or its degraded fallback) is recorded; p95 latency is measured
    end-to-end as ``p95 = decided_at - ingested_at``.

    Attributes:
        event_id: Inherited correlation key (same ticket as the decision).
        schema_version: Inherited wire schema version.
        decision_id: Unique verdict identifier (uuid4 hex); the trigger
            dedupe key — one decision, at most one alert.
        question_type: Which ``/v1/systemone`` head answered: ``choice``,
            ``score``, or ``noul``.
        answers: Raw ``/v1/systemone`` answers block, preserved verbatim.
        escalate_probability: Calibrated escalation probability in ``[0, 1]``.
        scam_probability: Calibrated scam probability in ``[0, 1]``.
        degraded: ``True`` when the model endpoint failed and a fallback
            verdict was recorded instead.
        decided_at: Moment the verdict was recorded (UTC, aware). Latency
            measurement contract: ``p95 = decided_at - ingested_at``.
    """

    decision_id: str = Field(
        description="Unique verdict identifier (uuid4 hex); the alert-trigger dedupe key.",
    )
    question_type: QuestionType = Field(
        description="Which /v1/systemone head answered: choice, score, or noul.",
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
        description=(
            "Producer-side moment the model verdict was recorded (UTC, aware). "
            "Latency measurement contract: p95 = decided_at - ingested_at."
        ),
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
                    "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
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
    """A notification dispatched (or rate-limit-suppressed) after a decision.

    One row per alert attempt, keyed by ``decision_id``; ``suppressed=True``
    records rate-limit drops so the alert trail stays complete in ClickHouse
    without hammering real chat channels.

    Attributes:
        event_id: Inherited correlation key (same ticket as the alert).
        schema_version: Inherited wire schema version.
        decision_id: The originating verdict id; joins to
            ``ModelDecision.decision_id``.
        channel: Delivery channel: ``telegram``, ``discord``, or ``mock``.
        suppressed: ``True`` when the rate limiter dropped the alert but the
            drop was still recorded.
        created_at: Producer-side moment the alert was attempted (UTC, aware).
    """

    decision_id: str = Field(
        description="Originating ModelDecision.decision_id this alert belongs to.",
    )
    channel: Channel = Field(
        description="Delivery channel: telegram, discord, or mock.",
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
                    "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
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
