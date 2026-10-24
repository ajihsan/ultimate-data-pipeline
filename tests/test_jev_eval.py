"""Unit tests for the eval harness's pure statistics helpers."""

from __future__ import annotations

import pytest

from udp.jev_eval import Record, _brier_choice, _percentile, _stats, _sweep


def _record(
    label: str,
    choice: str,
    probabilities: dict[str, float],
    p_escalate: float,
) -> Record:
    """Build a successful record with the given label and answers."""
    return Record(
        label=label,
        status=200,
        latency_ms=10.0,
        infer_ms=5.0,
        choice=choice,
        probabilities=probabilities,
        p_escalate=p_escalate,
    )


def test_percentile_is_nearest_rank() -> None:
    """The p95 of 20 ordered values is the 19th value."""
    values = [float(i) for i in range(1, 21)]
    assert _percentile(values, 0.95) == 19.0
    assert _percentile([], 0.95) == 0.0


def test_brier_choice_is_squared_error_over_the_distribution() -> None:
    """A perfect one-hot answer has Brier score 0."""
    assert _brier_choice({"a": 1.0, "b": 0.0}, "a") == 0.0
    assert _brier_choice({"a": 0.8, "b": 0.2}, "a") == pytest.approx(0.08)


def test_sweep_reports_confusion_and_f1() -> None:
    """One scam record and one other record separate at cut 0.5."""
    records = [
        _record("scam_report", "scam_report", {"scam_report": 0.8}, 0.9),
        _record("billing", "billing", {"scam_report": 0.1}, 0.2),
    ]
    sweep = _sweep(records, "scam", [0.5])
    point = sweep["0.50"]
    assert (point["tp"], point["fp"], point["fn"], point["tn"]) == (1.0, 0.0, 0.0, 1.0)
    assert point["precision"] == 1.0
    assert point["recall"] == 1.0
    assert point["f1"] == 1.0


def test_stats_aggregates_accuracy_and_latency() -> None:
    """The aggregate uses only status 200 records and reports p50/p95."""
    records = [
        _record("scam_report", "scam_report", {"scam_report": 0.8}, 0.9),
        _record("billing", "billing", {"scam_report": 0.1}, 0.2),
    ]
    stats = _stats(records, wall_s=1.0)
    assert stats.accuracy == 1.0
    assert stats.errors == 0
    assert stats.throughput == 2.0
    assert stats.latency_p50 == 10.0
    assert stats.latency_p95 == 10.0
    assert stats.stop is False
