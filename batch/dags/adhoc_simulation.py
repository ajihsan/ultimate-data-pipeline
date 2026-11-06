"""Adhoc DAGs to simulate incoming batch and realtime data."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from urllib.parse import urlparse

import aiomysql
import psycopg2
from airflow import DAG
from airflow.operators.python import PythonOperator


def _parse_mysql_dsn(dsn: str) -> dict[str, object]:
    """Parse MySQL DSN into connection attributes.

    Args:
        dsn: DSN string in URL or shorthand format.

    Returns:
        Dict[str, object]: Parsed connection values.
    """
    normalized = dsn if "://" in dsn else f"mysql://{dsn}"
    parsed = urlparse(normalized)
    if not parsed.hostname or not parsed.path:
        raise ValueError("Invalid MySQL DSN format")

    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "database": parsed.path.removeprefix("/"),
    }


def _insert_mysql_transaction() -> None:
    """Insert one synthetic transaction row into MySQL source tables."""
    dsn = os.getenv("MYSQL_DSN", "root:root@mysql:3306/app")
    parsed = _parse_mysql_dsn(dsn)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_insert_mysql_transaction_async(parsed))
    finally:
        loop.close()


async def _insert_mysql_transaction_async(parsed: dict[str, object]) -> None:
    """Async insertion implementation for MySQL.

    Args:
        parsed: Parsed DSN values.
    """
    conn = await aiomysql.connect(
        host=parsed["host"],
        port=int(parsed["port"]),
        user=str(parsed["user"]),
        password=str(parsed["password"]),
        db=str(parsed["database"]),
        autocommit=False,
    )

    suffix = int(time.time())
    email = f"adhoc_mysql_{suffix}@example.com"
    product_name = f"Adhoc Product {suffix}"

    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s)",
                (f"Adhoc MySQL User {suffix}", email),
            )
            user_id = cursor.lastrowid

            await cursor.execute(
                "INSERT INTO products (name, category, unit_price) VALUES (%s, %s, %s)",
                (product_name, "Adhoc", 49.90),
            )
            product_id = cursor.lastrowid

            await cursor.execute(
                """
                INSERT INTO transactions
                (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (user_id, product_id, 2, "credit_card", "completed", "USD"),
            )

        await conn.commit()
    finally:
        conn.close()


def _insert_postgres_transaction() -> None:
    """Insert one synthetic transaction row into PostgreSQL source tables."""
    dsn = os.getenv("POSTGRES_DSN", "postgresql://postgres:postgres@postgres:5432/app")

    suffix = int(time.time())
    email = f"adhoc_pg_{suffix}@example.com"
    product_name = f"Adhoc PG Product {suffix}"

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                (f"Adhoc PG User {suffix}", email),
            )
            user_id = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO products (name, category, unit_price)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (product_name, "Adhoc", 79.50),
            )
            product_id = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO transactions
                (user_id, product_id, quantity, payment_method, status, currency, transaction_ts)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (user_id, product_id, 1, "e_wallet", "completed", "USD"),
            )
        conn.commit()
    finally:
        conn.close()


def _simulate_batch_push() -> None:
    """Run adhoc data insertion for both batch source databases."""
    _insert_mysql_transaction()
    _insert_postgres_transaction()


def _simulate_realtime_push() -> None:
    """Run adhoc push to realtime CDC source database.

    This inserts a fresh MySQL transaction row that should be picked up
    by the MySQL CDC connector and propagated to the streaming stack.
    """
    _insert_mysql_transaction()


BATCH_ADHOC_DAG = DAG(
    dag_id="adhoc_push_batch_data",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["adhoc", "batch"],
)
adhoc_push_batch_data = BATCH_ADHOC_DAG

REALTIME_ADHOC_DAG = DAG(
    dag_id="adhoc_push_realtime_data",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["adhoc", "realtime"],
)
adhoc_push_realtime_data = REALTIME_ADHOC_DAG

PythonOperator(
    task_id="insert_batch_source_rows",
    python_callable=_simulate_batch_push,
    dag=BATCH_ADHOC_DAG,
)

PythonOperator(
    task_id="push_stream_event",
    python_callable=_simulate_realtime_push,
    dag=REALTIME_ADHOC_DAG,
)
