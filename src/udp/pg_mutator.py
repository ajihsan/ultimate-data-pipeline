"""Deterministic Postgres mutation driver for the conduit bus CDC pipeline.

Applies INSERT / UPDATE / DELETE operations to ``app_users`` so the
postgres → Redpanda (``pg_changes``) → ClickHouse (``app_users_cdc``) path can
be exercised end-to-end and its latency measured: with the same ``--seed`` the
chosen operations (new emails, plans, statuses, target ids) are identical
across runs; only wall-clock timestamps differ. Standalone ``udp-pg-mutate``
script; wired into ``make mutate``.

DSN resolution: ``POSTGRES_DSN`` wins if set; otherwise it is composed from
the ``POSTGRES_*`` variables (see .env), falling back to the local development
defaults in .env.example.
"""

from __future__ import annotations

import argparse
import os
import random
import time

import psycopg

MODES = ("insert", "update", "delete", "mix")
"""Mutation kinds: three single-op modes plus ``mix`` (round-robin cycling)."""

DEFAULT_SEED = 42
"""Default random seed; the same seed always yields the same operations."""

DEFAULT_COUNT = 10
"""Default number of mutations to apply."""

DEFAULT_INTERVAL_S = 0.25
"""Default pause between mutations (seconds)."""

PLANS = ("free", "pro", "teams")
"""Valid plan values for UPDATEs and INSERTs (matches the seed vocabulary)."""

STATUSES = ("active", "trial", "cancelled", "churned")
"""Valid status values for UPDATEs and INSERTs (matches the seed vocabulary)."""


def default_dsn() -> str:
    """Resolve the app_users DSN from the environment.

    ``POSTGRES_DSN`` overrides everything; otherwise compose the URL from the
    ``POSTGRES_*`` variables (.env), with the compose dev defaults as fallback.
    """
    dsn = os.environ.get("POSTGRES_DSN", "")
    if dsn:
        return dsn
    user = os.environ.get("POSTGRES_USER", "udp")
    password = os.environ.get("POSTGRES_PASSWORD", "udp-dev-password")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "udp")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _next_op(mode: str, i: int) -> str:
    """Pick the i-th operation for ``mode`` (mix cycles insert→update→delete)."""
    if mode != "mix":
        return mode
    return ("insert", "update", "delete")[i % 3]


def _insert_one(cur: psycopg.Cursor, rng: random.Random, seq: int, seed: int) -> int:
    """Insert one app_user; returns the new id.

    Email embeds the seed + sequence so the same seed always produces the
    same emails (no unique constraint, but distinct is nicer to trace).
    """
    email = f"cdc.mutant.{seed:04d}.{seq:04d}@example.com"
    plan = rng.choice(PLANS)
    status = rng.choice(STATUSES)
    cur.execute(
        "INSERT INTO app_users (email, plan, status) VALUES (%s, %s, %s) RETURNING id",
        (email, plan, status),
    )
    row = cur.fetchone()
    if row is None:  # pragma: no cover - RETURNING always yields one row
        raise RuntimeError("INSERT ... RETURNING id returned no row")
    new_id = int(row[0])
    print(f"[insert] id={new_id} email={email} plan={plan} status={status}", flush=True)
    return new_id


def _update_one(cur: psycopg.Cursor, rng: random.Random, ids: list[int]) -> int | None:
    """Update one app_user's plan/status (touches updated_at); returns the id."""
    if not ids:
        return None
    target = rng.choice(ids)
    plan = rng.choice(PLANS)
    status = rng.choice(STATUSES)
    # updated_at = now() is what makes the row differ from its snapshot twin.
    cur.execute(
        "UPDATE app_users SET plan = %s, status = %s, updated_at = now() WHERE id = %s",
        (plan, status, target),
    )
    print(f"[update] id={target} plan={plan} status={status}", flush=True)
    return target


def _delete_one(cur: psycopg.Cursor, rng: random.Random, ids: list[int]) -> int | None:
    """Delete one app_user; returns the id (None when the table is empty)."""
    if not ids:
        return None
    target = rng.choice(ids)
    cur.execute("DELETE FROM app_users WHERE id = %s", (target,))
    print(f"[delete] id={target}", flush=True)
    return target


def apply_mutations(
    conn: psycopg.Connection, mode: str, count: int, rng: random.Random, seed: int, interval: float
) -> None:
    """Apply ``count`` mutations in one transaction and print each one.

    Update/delete targets are drawn from the live id set (refreshed as
    inserts/deletes land), which keeps mix-mode deterministic under --seed:
    the same seed against the same starting table applies identical ops.
    """
    with conn.cursor() as cur:
        ids: list[int] = [int(row[0]) for row in cur.execute("SELECT id FROM app_users")]
        for i in range(count):
            op = _next_op(mode, i)
            if op == "insert":
                ids.append(_insert_one(cur, rng, i, seed))
            elif op == "update":
                _update_one(cur, rng, ids)
            else:
                deleted = _delete_one(cur, rng, ids)
                if deleted is not None:
                    ids.remove(deleted)
            if interval > 0 and i < count - 1:
                time.sleep(interval)
    conn.commit()


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Build the ``udp-pg-mutate`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="udp-pg-mutate",
        add_help=add_help,
        description=(
            "Apply deterministic mutations to postgres app_users "
            "(CDC verification driver: insert|update|delete|mix)."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        metavar="N",
        help="total mutations to apply (default: %(default)s)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="random seed; same seed ⇒ same operations (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        metavar="SECONDS",
        help="pause between mutations (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="mix",
        help="mutation mode; mix cycles insert→update→delete (default: %(default)s)",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="postgres DSN override (default: POSTGRES_DSN env, else POSTGRES_* vars)",
    )
    return parser


def run_from_namespace(args: argparse.Namespace) -> int:
    """Run the mutator from a parsed CLI namespace. Returns the exit code."""
    dsn = args.dsn or default_dsn()
    rng = random.Random(args.seed)
    with psycopg.connect(dsn) as conn:
        apply_mutations(conn, args.mode, args.count, rng, args.seed, args.interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``udp-pg-mutate`` console script.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    return run_from_namespace(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
