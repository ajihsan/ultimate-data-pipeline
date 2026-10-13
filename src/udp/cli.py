"""Command-line entry point for the udp realtime pipeline."""

from __future__ import annotations

import argparse

from udp import __version__
from udp.generator.tickets import build_generate_parser, run_from_namespace


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``udp`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="udp",
        description="Ultimate Data Pipeline (realtime): ingest → decide → store → trigger.",
    )
    parser.add_argument("--version", action="version", version=f"udp {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser(
        "generate",
        parents=[build_generate_parser(add_help=False)],
        description="Generate synthetic app-user tickets onto Kafka.",
        help="generate synthetic tickets onto Kafka (use --dry-run to print instead)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return run_from_namespace(args)
    parser.print_help()
    return 0
