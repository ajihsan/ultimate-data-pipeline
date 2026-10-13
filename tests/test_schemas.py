"""Contract tests for the udp domain schemas."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from udp.schemas import SCHEMA_VERSION, AlertEvent, FastDecision, ModelDecision, Ticket
from udp.schemas_doc import MODELS, render_schemas_doc

REPO_ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 10, 13, 9, 12, 42, tzinfo=UTC)

TICKET_KWARGS: dict[str, Any] = {
    "event_id": "9f0c8b7e6d5a4940b2f1c3d4e5f60718",
    "text": "my app keeps logging me out every 2 minutes",
    "user_id": "usr_kgarcia84",
    "created_at": NOW,
    "ingested_at": NOW,
}


def fast_decision_kwargs() -> dict[str, Any]:
    """Return a valid FastDecision payload."""
    return {
        "event_id": TICKET_KWARGS["event_id"],
        "category": "scam_report",
        "confidence": 0.94,
        "decided_at": NOW,
    }


def model_decision_kwargs() -> dict[str, Any]:
    """Return a valid ModelDecision payload."""
    return {
        "event_id": TICKET_KWARGS["event_id"],
        "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
        "question_type": "choice",
        "answers": {"choice": "scam_report", "scores": {"escalate": 0.21, "scam": 0.78}},
        "escalate_probability": 0.21,
        "scam_probability": 0.78,
        "decided_at": NOW,
    }


def alert_event_kwargs() -> dict[str, Any]:
    """Return a valid AlertEvent payload."""
    return {
        "event_id": TICKET_KWARGS["event_id"],
        "decision_id": "4f9c2a8e7b3d4a1f8c6e2d5b0a9f7e34",
        "channel": "mock",
        "created_at": NOW,
    }


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (Ticket, TICKET_KWARGS),
        (FastDecision, fast_decision_kwargs()),
        (ModelDecision, model_decision_kwargs()),
        (AlertEvent, alert_event_kwargs()),
    ],
)
def test_json_round_trip(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    """Every payload survives model_validate_json(model_dump_json())."""
    instance = model.model_validate(kwargs)
    restored = model.model_validate_json(instance.model_dump_json())
    assert restored == instance


def test_examples_from_schema_validate_and_round_trip() -> None:
    """Every documented example payload validates and round-trips."""
    for model in MODELS:
        example = model.model_json_schema()["examples"][0]
        instance = model.model_validate(example)
        assert model.model_validate_json(instance.model_dump_json()) == instance


def test_documented_examples_are_valid_json_examples() -> None:
    """Each model's JSON schema carries exactly one usable example."""
    for model in MODELS:
        examples = model.model_json_schema().get("examples") or []
        assert len(examples) == 1, model.__name__
        assert model.model_validate(examples[0]) is not None


def test_default_envelope_values() -> None:
    """schema_version defaults to the current wire version; flags default off."""
    assert Ticket.model_validate(TICKET_KWARGS).schema_version == SCHEMA_VERSION
    assert ModelDecision.model_validate(model_decision_kwargs()).degraded is False
    assert AlertEvent.model_validate(alert_event_kwargs()).suppressed is False


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (Ticket, TICKET_KWARGS),
        (FastDecision, fast_decision_kwargs()),
        (ModelDecision, model_decision_kwargs()),
        (AlertEvent, alert_event_kwargs()),
    ],
)
def test_blank_event_id_rejected(model: type[BaseModel], kwargs: dict[str, Any]) -> None:
    """Empty or whitespace-only event_id is a producer bug, not silent data loss."""
    for bad in ("", "   "):
        with pytest.raises(ValidationError, match="event_id"):
            model.model_validate({**kwargs, "event_id": bad})


def test_confidence_bounds() -> None:
    """FastDecision confidence is constrained to [0, 1]."""
    for bad in (-0.1, 1.1):
        with pytest.raises(ValidationError):
            FastDecision.model_validate({**fast_decision_kwargs(), "confidence": bad})
    assert (
        FastDecision.model_validate({**fast_decision_kwargs(), "confidence": 0.0}).confidence == 0.0
    )
    assert (
        FastDecision.model_validate({**fast_decision_kwargs(), "confidence": 1.0}).confidence == 1.0
    )


def test_probability_bounds() -> None:
    """ModelDecision probabilities are constrained to [0, 1]."""
    for field in ("escalate_probability", "scam_probability"):
        for bad in (-0.01, 1.01):
            with pytest.raises(ValidationError):
                ModelDecision.model_validate({**model_decision_kwargs(), field: bad})


def test_enum_fields_reject_unknown_values() -> None:
    """category, question_type, and channel only accept their documented values."""
    with pytest.raises(ValidationError):
        FastDecision.model_validate({**fast_decision_kwargs(), "category": "shouting"})
    with pytest.raises(ValidationError):
        ModelDecision.model_validate({**model_decision_kwargs(), "question_type": "essay"})
    with pytest.raises(ValidationError):
        AlertEvent.model_validate({**alert_event_kwargs(), "channel": "sms"})


def test_naive_datetimes_rejected() -> None:
    """Naive timestamps would break p95 = decided_at - ingested_at arithmetic."""
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="aware"):
        Ticket.model_validate({**TICKET_KWARGS, "ingested_at": naive})
    with pytest.raises(ValidationError, match="aware"):
        FastDecision.model_validate({**fast_decision_kwargs(), "decided_at": naive})
    with pytest.raises(ValidationError, match="aware"):
        ModelDecision.model_validate({**model_decision_kwargs(), "decided_at": naive})
    with pytest.raises(ValidationError, match="aware"):
        AlertEvent.model_validate({**alert_event_kwargs(), "created_at": naive})


def test_extra_fields_forbidden() -> None:
    """Unknown fields on the wire are rejected (producer bug, not data)."""
    with pytest.raises(ValidationError, match="extra"):
        Ticket.model_validate({**TICKET_KWARGS, "surprise": 1})


def test_ticket_wire_json_strips_category_hint() -> None:
    """wire_json removes generator-only metadata but stays a valid Ticket."""
    ticket = Ticket.model_validate({**TICKET_KWARGS, "category_hint": "complaint"})
    wire = json.loads(ticket.wire_json())
    assert "category_hint" not in wire
    restored = Ticket.model_validate_json(ticket.wire_json())
    assert restored.category_hint is None
    assert restored.event_id == ticket.event_id
    assert restored == ticket.model_copy(update={"category_hint": None})


def test_wire_excluded_fields_declaration() -> None:
    """Only Ticket declares wire-excluded fields; the decision/alert payloads carry all fields."""
    assert frozenset({"category_hint"}) == Ticket.WIRE_EXCLUDED_FIELDS
    for model in (FastDecision, ModelDecision, AlertEvent):
        assert getattr(model, "WIRE_EXCLUDED_FIELDS", None) in (None, frozenset())


def test_schemas_doc_matches_models() -> None:
    """docs/schemas.md is generated and must not drift from the models."""
    doc_path = REPO_ROOT / "docs" / "schemas.md"
    assert doc_path.is_file(), "run: uv run python -m udp.schemas_doc"
    assert doc_path.read_text(encoding="utf-8") == render_schemas_doc()
