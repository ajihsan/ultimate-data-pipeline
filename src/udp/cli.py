"""Command-line entry point for the udp realtime pipeline."""

from __future__ import annotations

import argparse

from udp import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``udp`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="udp",
        description="Ultimate Data Pipeline (realtime): ingest → decide → store → trigger.",
    )
    parser.add_argument("--version", action="version", version=f"udp {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser(
        "ingest",
        help="run the ingest → decide → store loop (wired in S2)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ingest":
        print("udp ingest is wired in S2 (schema + generator)")
        return 0
    parser.print_help()
    return 0
