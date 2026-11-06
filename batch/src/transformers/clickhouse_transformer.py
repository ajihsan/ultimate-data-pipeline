from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib import request


@dataclass(slots=True)
class ClickHouseConfig:
    """Connection settings for ClickHouse HTTP API.

    Attributes:
        url: Base ClickHouse HTTP endpoint.
        username: ClickHouse username.
        password: ClickHouse password.
        database: Default database for query execution.
    """

    url: str
    username: str = "default"
    password: str = ""
    database: str = "default"


class ClickHouseTransformer:
    """Load parquet chunks and transform staging data to DWH."""

    def __init__(self, config: ClickHouseConfig) -> None:
        self.config = config

    def bootstrap(self) -> None:
        """Create required staging and DWH objects if they do not exist."""
        statements = [
            "CREATE DATABASE IF NOT EXISTS staging",
            "CREATE DATABASE IF NOT EXISTS dwh",
            """
            CREATE TABLE IF NOT EXISTS staging.staging_transactions
            (
                transaction_id UInt64,
                user_id UInt64,
                user_name String,
                user_email String,
                product_id UInt64,
                product_name String,
                product_category String,
                quantity UInt32,
                unit_price Decimal(18, 2),
                gross_amount Decimal(18, 2),
                currency FixedString(3),
                payment_method String,
                transaction_status String,
                transaction_ts DateTime,
                source_system String,
                extracted_at DateTime,
                batch_date Date
            )
            ENGINE = MergeTree
            ORDER BY (batch_date, source_system, transaction_id)
            PARTITION BY batch_date
            """,
            """
            CREATE TABLE IF NOT EXISTS dwh.dim_user
            (
                user_key UInt64,
                user_id UInt64,
                source_system String,
                user_name String,
                user_email String,
                _updated_at DateTime DEFAULT now()
            )
            ENGINE = ReplacingMergeTree(_updated_at)
            ORDER BY user_key
            """,
            """
            CREATE TABLE IF NOT EXISTS dwh.dim_product
            (
                product_key UInt64,
                product_id UInt64,
                source_system String,
                product_name String,
                product_category String,
                unit_price Decimal(18, 2),
                _updated_at DateTime DEFAULT now()
            )
            ENGINE = ReplacingMergeTree(_updated_at)
            ORDER BY product_key
            """,
            """
            CREATE TABLE IF NOT EXISTS dwh.dim_payment_method
            (
                payment_method_key UInt64,
                payment_method String,
                _updated_at DateTime DEFAULT now()
            )
            ENGINE = ReplacingMergeTree(_updated_at)
            ORDER BY payment_method_key
            """,
            """
            CREATE TABLE IF NOT EXISTS dwh.dim_date
            (
                date_key UInt32,
                full_date Date,
                year UInt16,
                quarter UInt8,
                month UInt8,
                day UInt8,
                day_of_week UInt8
            )
            ENGINE = ReplacingMergeTree
            ORDER BY date_key
            """,
            """
            CREATE TABLE IF NOT EXISTS dwh.fact_transactions
            (
                transaction_key UInt64,
                transaction_id UInt64,
                user_key UInt64,
                product_key UInt64,
                payment_method_key UInt64,
                date_key UInt32,
                quantity UInt32,
                unit_price Decimal(18, 2),
                gross_amount Decimal(18, 2),
                currency FixedString(3),
                transaction_status String,
                source_system String,
                transaction_ts DateTime,
                extracted_at DateTime,
                _inserted_at DateTime DEFAULT now()
            )
            ENGINE = ReplacingMergeTree(_inserted_at)
            ORDER BY (source_system, transaction_id)
            PARTITION BY toYYYYMM(transaction_ts)
            """,
        ]

        for statement in statements:
            self.execute(statement)

    def load_parquet_chunk(self, parquet_file: Path) -> None:
        """Insert one parquet chunk into `staging.staging_transactions`.

        Args:
            parquet_file: Path accessible by ClickHouse `file(...)`.
        """
        # ClickHouse reads from files available under `user_files`.
        statement = f"""
        INSERT INTO staging.staging_transactions
        SELECT
            toUInt64(transaction_id) AS transaction_id,
            toUInt64(user_id) AS user_id,
            toString(user_name) AS user_name,
            toString(user_email) AS user_email,
            toUInt64(product_id) AS product_id,
            toString(product_name) AS product_name,
            toString(product_category) AS product_category,
            toUInt32(quantity) AS quantity,
            toDecimal64(toString(unit_price), 2) AS unit_price,
            toDecimal64(toString(gross_amount), 2) AS gross_amount,
            upper(toString(currency)) AS currency,
            toString(payment_method) AS payment_method,
            toString(transaction_status) AS transaction_status,
            parseDateTimeBestEffort(toString(transaction_ts)) AS transaction_ts,
            toString(source_system) AS source_system,
            parseDateTimeBestEffort(toString(extracted_at)) AS extracted_at,
            toDate(parseDateTimeBestEffort(toString(extracted_at))) AS batch_date
        FROM file('{parquet_file}', 'Parquet')
        """
        self.execute(statement)

    def transform_to_dwh(self) -> None:
        """Run SQL transformations from staging to DWH tables."""
        statements = [
            """
            INSERT INTO dwh.dim_user (user_key, user_id, source_system, user_name, user_email)
            SELECT
                cityHash64(concat(source_system, ':', toString(user_id))) AS user_key,
                user_id,
                source_system,
                anyLast(user_name) AS user_name,
                anyLast(user_email) AS user_email
            FROM staging.staging_transactions
            GROUP BY source_system, user_id
            """,
            """
            INSERT INTO dwh.dim_product (
                product_key, product_id, source_system, product_name, product_category, unit_price
            )
            SELECT
                cityHash64(concat(source_system, ':', toString(product_id))) AS product_key,
                product_id,
                source_system,
                anyLast(product_name) AS product_name,
                anyLast(product_category) AS product_category,
                anyLast(unit_price) AS unit_price
            FROM staging.staging_transactions
            GROUP BY source_system, product_id
            """,
            """
            INSERT INTO dwh.dim_payment_method (payment_method_key, payment_method)
            SELECT
                cityHash64(lower(payment_method)) AS payment_method_key,
                lower(payment_method) AS payment_method
            FROM staging.staging_transactions
            GROUP BY lower(payment_method)
            """,
            """
            INSERT INTO dwh.dim_date (date_key, full_date, year, quarter, month, day, day_of_week)
            SELECT
                toUInt32(toYYYYMMDD(toDate(transaction_ts))) AS date_key,
                toDate(transaction_ts) AS full_date,
                toUInt16(toYear(transaction_ts)) AS year,
                toUInt8(toQuarter(transaction_ts)) AS quarter,
                toUInt8(toMonth(transaction_ts)) AS month,
                toUInt8(toDayOfMonth(transaction_ts)) AS day,
                toUInt8(toDayOfWeek(transaction_ts)) AS day_of_week
            FROM staging.staging_transactions
            GROUP BY toDate(transaction_ts)
            """,
            """
            INSERT INTO dwh.fact_transactions (
                transaction_key,
                transaction_id,
                user_key,
                product_key,
                payment_method_key,
                date_key,
                quantity,
                unit_price,
                gross_amount,
                currency,
                transaction_status,
                source_system,
                transaction_ts,
                extracted_at
            )
            SELECT
                cityHash64(concat(source_system, ':', toString(transaction_id))) AS transaction_key,
                transaction_id,
                cityHash64(concat(source_system, ':', toString(user_id))) AS user_key,
                cityHash64(concat(source_system, ':', toString(product_id))) AS product_key,
                cityHash64(lower(payment_method)) AS payment_method_key,
                toUInt32(toYYYYMMDD(toDate(transaction_ts))) AS date_key,
                quantity,
                unit_price,
                gross_amount,
                currency,
                transaction_status,
                source_system,
                transaction_ts,
                extracted_at
            FROM staging.staging_transactions
            """,
        ]
        for statement in statements:
            self.execute(statement)

    def execute(self, statement: str) -> None:
        """Execute a SQL statement through ClickHouse HTTP endpoint.

        Args:
            statement: SQL statement to execute.
        """
        query = statement.strip()
        data = query.encode("utf-8")
        target = f"{self.config.url}/?database={self.config.database}"

        req = request.Request(target, data=data, method="POST")
        if self.config.username:
            credentials = f"{self.config.username}:{self.config.password}"
            token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            req.add_header("Authorization", f"Basic {token}")

        with request.urlopen(req, timeout=30) as response:
            response.read()
