"""Unit tests for the CDC mutation driver's pure helpers."""

from __future__ import annotations

import pytest

from udp.pg_mutator import _next_op, default_dsn


def test_default_dsn_prefers_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSTGRES_DSN wins over the component variables."""
    monkeypatch.setenv("POSTGRES_DSN", "postgres://explicit:db")
    monkeypatch.setenv("POSTGRES_USER", "ignored")
    assert default_dsn() == "postgres://explicit:db"


def test_default_dsn_composes_component_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """The component variables build a libpq URL with compose defaults."""
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("POSTGRES_USER", "user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pass")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "15432")
    monkeypatch.setenv("POSTGRES_DB", "warehouse")
    assert default_dsn() == "postgres://user:pass@db:15432/warehouse"


def test_default_dsn_falls_back_to_dev_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty environment yields the compose dev connection."""
    for key in (
        "POSTGRES_DSN",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    assert default_dsn() == "postgres://udp:udp-dev-password@localhost:5432/udp"


def test_next_op_cycles_mix_mode() -> None:
    """mix mode cycles insert, update, delete."""
    assert [_next_op("mix", i) for i in range(4)] == ["insert", "update", "delete", "insert"]


def test_next_op_passes_single_modes_through() -> None:
    """A single-op mode ignores the index."""
    assert _next_op("insert", 7) == "insert"
