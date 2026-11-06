from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from extractors.mysql_extractor import MySQLExtractor
from extractors.postgres_extractor import PostgresExtractor
from loaders.parquet_loader import ParquetLoader
from transformers.clickhouse_transformer import ClickHouseConfig, ClickHouseTransformer


@dataclass(slots=True)
class BatchPipelineConfig:
    """Runtime configuration for one batch pipeline execution.

    Attributes:
        source_type: Source database type (`mysql` or `postgresql`).
        source_dsn: Source DSN string.
        source_query: Query for extraction.
        chunk_size: Chunk size for fetch/write processing.
        dataset: Dataset name used in parquet partition path.
        target_base_path: Root filesystem path for parquet files.
        clickhouse_url: ClickHouse HTTP endpoint.
        clickhouse_user: ClickHouse username.
        clickhouse_password: ClickHouse password.
        clickhouse_database: Default database for ClickHouse SQL execution.
    """

    source_type: str
    source_dsn: str
    source_query: str
    chunk_size: int
    dataset: str
    target_base_path: str
    clickhouse_url: str
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_database: str = "default"


def run_batch_pipeline(
    config: BatchPipelineConfig, execution_date: datetime | None = None
) -> dict[str, Any]:
    """Run end-to-end batch ELT from source to DWH.

    Args:
        config: Pipeline runtime configuration.
        execution_date: Optional execution date override.

    Returns:
        Dict[str, Any]: Summary metadata for processed files and row counts.
    """
    run_date = execution_date or datetime.utcnow()

    extractor = _build_extractor(config)
    loader = ParquetLoader(base_path=config.target_base_path)
    transformer = ClickHouseTransformer(
        ClickHouseConfig(
            url=config.clickhouse_url,
            username=config.clickhouse_user,
            password=config.clickhouse_password,
            database=config.clickhouse_database,
        )
    )

    transformer.bootstrap()

    parquet_files: list[str] = []
    total_rows = 0

    for part, rows in enumerate(extractor.extract(), start=1):
        file_path = loader.write_chunk(
            dataset=config.dataset,
            records=rows,
            execution_date=run_date,
            part_number=part,
        )
        parquet_files.append(str(file_path))
        total_rows += len(rows)
        transformer.load_parquet_chunk(_to_clickhouse_file_path(file_path))

    transformer.transform_to_dwh()

    return {
        "dataset": config.dataset,
        "execution_date": run_date.isoformat(),
        "partitions_written": len(parquet_files),
        "rows_processed": total_rows,
        "parquet_files": parquet_files,
    }


def _build_extractor(config: BatchPipelineConfig) -> PostgresExtractor | MySQLExtractor:
    """Build extractor implementation from source type.

    Args:
        config: Pipeline runtime configuration.

    Returns:
        PostgresExtractor | MySQLExtractor: Source extractor instance.

    Raises:
        ValueError: If source type is unsupported.
    """
    source = config.source_type.lower()
    if source == "postgresql":
        return PostgresExtractor(
            dsn=config.source_dsn,
            query=config.source_query,
            chunk_size=config.chunk_size,
        )
    if source == "mysql":
        return MySQLExtractor(
            dsn=config.source_dsn,
            query=config.source_query,
            chunk_size=config.chunk_size,
        )
    raise ValueError(f"Unsupported source_type: {config.source_type}")


def _to_clickhouse_file_path(local_path: Path) -> Path:
    """Translate Airflow-local path into ClickHouse `file(...)` path.

    Args:
        local_path: File path produced by the loader.

    Returns:
        Path: Path usable by ClickHouse in the container.
    """
    # Airflow writes to /opt/airflow/data; in ClickHouse this is mounted under user_files/batch_data.
    if "/opt/airflow/data" in str(local_path):
        return Path(str(local_path).replace("/opt/airflow/data/", "batch_data/"))
    return local_path
