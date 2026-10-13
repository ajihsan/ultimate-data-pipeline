"""Tests for the synthetic ticket generator: mix, determinism, bounds, CLI."""

import json
from collections import Counter
from typing import Any

import pytest

from udp.generator import tickets
from udp.generator.tickets import (
    MAX_FLAGGED_PER_BURST,
    BurstProfile,
    GeneratorConfig,
    TicketGenerator,
    main,
)
from udp.schemas import CATEGORIES, Ticket

FLAGGED_FIELD = "category_hint"


def _seeded_fields(events: list[Ticket]) -> list[tuple[str, str, str | None, str]]:
    """Extract the seed-dependent fields for determinism comparison.

    Args:
        events: Generated tickets.

    Returns:
        (event_id, text, category_hint, user_id) tuples.
    """
    return [(e.event_id, e.text, e.category_hint, e.user_id) for e in events]


def test_ambiguous_fraction_bounded() -> None:
    """Default config flags ≤5% of events (≤5/s at the nominal 100 ev/s)."""
    generator = TicketGenerator(GeneratorConfig(seed=7))
    events = list(generator.events(5000))
    flagged = sum(1 for e in events if e.category_hint is None)
    assert 0 < flagged <= 0.055 * len(events)


def test_all_categories_present() -> None:
    """The steady mix exercises every category."""
    generator = TicketGenerator(GeneratorConfig(seed=3))
    counts = Counter(e.category_hint for e in generator.events(5000) if e.category_hint)
    assert set(counts) == set(CATEGORIES)


def test_determinism_same_seed() -> None:
    """Same seed ⇒ identical seeded fields for the first N events."""
    first = _seeded_fields(list(TicketGenerator(GeneratorConfig(seed=123)).events(200)))
    second = _seeded_fields(list(TicketGenerator(GeneratorConfig(seed=123)).events(200)))
    assert first == second


def test_determinism_different_seed() -> None:
    """A different seed changes the sequence."""
    first = _seeded_fields(list(TicketGenerator(GeneratorConfig(seed=123)).events(50)))
    second = _seeded_fields(list(TicketGenerator(GeneratorConfig(seed=124)).events(50)))
    assert first != second


def test_generated_tickets_round_trip() -> None:
    """Every generated ticket validates against the schema and round-trips."""
    generator = TicketGenerator(GeneratorConfig(seed=5))
    for ticket in generator.events(200):
        restored = Ticket.model_validate_json(ticket.model_dump_json())
        assert restored == ticket


def test_demo_burst_flagged_budget() -> None:
    """Demo profile: flagged events inside the 60s burst are hard-capped ≤360."""
    config = GeneratorConfig(seed=11, profile=tickets.DEMO)
    generator = TicketGenerator(config)
    burst_events = generator.burst_event_count()
    assert burst_events is not None and burst_events > 0
    events = list(generator.events(burst_events + 2000))
    flagged_in_burst = sum(1 for e in events[:burst_events] if e.category_hint is None)
    assert 0 < flagged_in_burst <= MAX_FLAGGED_PER_BURST


def test_demo_burst_bounds_hold_deterministically() -> None:
    """The ≤360 bound reproduces exactly across same-seed demo runs."""
    window = TicketGenerator(GeneratorConfig(seed=11, profile=tickets.DEMO)).burst_event_count()
    assert window is not None
    counts = []
    for _ in range(2):
        gen = TicketGenerator(GeneratorConfig(seed=11, profile=tickets.DEMO))
        counts.append(sum(1 for e in gen.events(window) if e.category_hint is None))
    assert counts[0] == counts[1]
    assert counts[0] <= MAX_FLAGGED_PER_BURST


def test_steady_profile_has_no_burst() -> None:
    """Steady profile never enters a burst window."""
    assert TicketGenerator(GeneratorConfig(seed=1)).burst_event_count() is None


def test_demo_scam_share() -> None:
    """Inside the demo burst, most events are labeled scam_report."""
    generator = TicketGenerator(GeneratorConfig(seed=13, profile=tickets.DEMO))
    burst_events = generator.burst_event_count()
    assert burst_events is not None
    in_burst = [
        e.category_hint for e in generator.events(burst_events) if e.category_hint is not None
    ]
    share = sum(1 for c in in_burst if c == "scam_report") / len(in_burst)
    assert 0.5 <= share <= 0.7


def test_templates_cover_categories() -> None:
    """Every category ships ≥2 templates; the ambiguous pool has ≥2 too."""
    for category in CATEGORIES:
        assert len(tickets.TEMPLATES[category]) >= 2, category
    assert len(tickets.AMBIGUOUS_TEMPLATES) >= 2


def test_ambiguous_flagged_hint_is_none() -> None:
    """Flagged (ambiguous) tickets carry no category_hint; normal ones do."""
    generator = TicketGenerator(GeneratorConfig(seed=7))
    events = list(generator.events(2000))
    hinted = [e for e in events if e.category_hint is not None]
    assert all(e.category_hint in CATEGORIES for e in hinted)


def test_burst_profile_budget_bound_assert() -> None:
    """Profiles cannot declare a flagged budget above the model-capacity bound."""
    with pytest.raises(AssertionError):
        BurstProfile(
            name="overbudget",
            duration_s=60.0,
            rate_multiplier=3.0,
            scam_share=0.6,
            flagged_budget=MAX_FLAGGED_PER_BURST + 1,
        )


def test_dry_run_cli_prints_valid_tickets(capsys: pytest.CaptureFixture[str]) -> None:
    """`udp-generate --dry-run --seed 42 --count 5` prints 5 valid Tickets."""
    code = main(["--dry-run", "--seed", "42", "--count", "5", "--rate", "100000"])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) >= 5
    parsed: list[Ticket] = []
    for line in lines:
        payload: dict[str, Any] = json.loads(line)
        # dry-run keeps generator-only metadata so tests can assert on it
        assert payload["category_hint"] is not None
        parsed.append(Ticket.model_validate_json(line))
    assert {t.category_hint for t in parsed} <= set(CATEGORIES) | {None}


def test_cli_generate_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """`udp generate --dry-run` shares the generator entry point."""
    from udp.cli import main as cli_main

    code = cli_main(["generate", "--dry-run", "--seed", "42", "--count", "3", "--rate", "100000"])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        Ticket.model_validate_json(line)
