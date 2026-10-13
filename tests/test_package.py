"""Smoke tests for the udp package scaffold."""

import pytest

from udp import __version__
from udp.cli import main


def test_version_is_exposed() -> None:
    """The package exposes its version string."""
    assert __version__ == "0.1.0"


def test_cli_help_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    """Running the CLI without arguments prints help and exits cleanly."""
    assert main([]) == 0
    assert "usage: udp" in capsys.readouterr().out
