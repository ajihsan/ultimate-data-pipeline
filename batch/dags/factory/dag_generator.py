from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from airflow import DAG
from airflow.operators.python import PythonOperator

BATCH_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BATCH_SRC) not in sys.path:
    sys.path.append(str(BATCH_SRC))

from pipeline import BatchPipelineConfig, run_batch_pipeline  # noqa: E402


@dataclass(slots=True)
class DagDefinition:
    """Configuration object for a generated batch DAG.

    Attributes:
        dag_id: Airflow DAG id.
        schedule: Airflow schedule expression.
        source_type: Source type (`mysql` or `postgresql`).
        source_dsn_env: Environment variable for source DSN.
        source_query: Extraction query.
        chunk_size: Extraction chunk size.
        dataset: Output dataset name.
        target_base_path: Output parquet base path.
        clickhouse_url_env: Environment variable for ClickHouse URL.
        clickhouse_user_env: Environment variable for ClickHouse user.
        clickhouse_password_env: Environment variable for ClickHouse password.
        clickhouse_database: Default ClickHouse database.
    """

    dag_id: str
    schedule: str
    source_type: str
    source_dsn_env: str
    source_query: str
    chunk_size: int
    dataset: str
    target_base_path: str
    clickhouse_url_env: str
    clickhouse_user_env: str
    clickhouse_password_env: str
    clickhouse_database: str


class DagFactory:
    """Factory that builds Airflow DAGs from TOML definitions."""

    def __init__(self, definitions_dir: Path) -> None:
        """Initialize the factory.

        Args:
            definitions_dir: Directory containing TOML DAG definitions.
        """
        self.definitions_dir = definitions_dir

    def load_definitions(self) -> list[DagDefinition]:
        """Load all TOML definitions from the definitions directory.

        Returns:
            List[DagDefinition]: Parsed DAG definitions.
        """
        definitions: list[DagDefinition] = []
        for definition_file in sorted(self.definitions_dir.glob("*.toml")):
            with definition_file.open("rb") as fp:
                payload = tomllib.load(fp)

            definitions.append(
                DagDefinition(
                    dag_id=payload["dag_id"],
                    schedule=payload["schedule"],
                    source_type=payload["source"]["type"],
                    source_dsn_env=payload["source"]["dsn_env"],
                    source_query=payload["source"]["query"],
                    chunk_size=int(payload["source"].get("chunk_size", 10_000)),
                    dataset=payload["target"]["dataset"],
                    target_base_path=payload["target"]["base_path"],
                    clickhouse_url_env=payload["clickhouse"]["url_env"],
                    clickhouse_user_env=payload["clickhouse"].get("user_env", "CLICKHOUSE_USER"),
                    clickhouse_password_env=payload["clickhouse"].get(
                        "password_env", "CLICKHOUSE_PASSWORD"
                    ),
                    clickhouse_database=payload["clickhouse"].get("database", "default"),
                )
            )
        return definitions

    def build_dags(self) -> dict[str, DAG]:
        """Build Airflow DAG objects keyed by DAG id.

        Returns:
            Dict[str, DAG]: Generated DAGs.
        """
        dags: dict[str, DAG] = {}
        for definition in self.load_definitions():
            dags[definition.dag_id] = _build_dag(definition)
        return dags


def _build_dag(definition: DagDefinition) -> DAG:
    """Create a single Airflow DAG from one definition.

    Args:
        definition: Parsed DAG definition.

    Returns:
        DAG: Constructed Airflow DAG instance.
    """
    default_args = {"owner": "data-platform", "retries": 1}

    dag = DAG(
        dag_id=definition.dag_id,
        default_args=default_args,
        start_date=datetime(2026, 1, 1),
        schedule=definition.schedule,
        catchup=False,
        tags=["batch", definition.source_type],
    )

    def _run() -> dict[str, Any]:
        config = BatchPipelineConfig(
            source_type=definition.source_type,
            source_dsn=os.environ[definition.source_dsn_env],
            source_query=definition.source_query,
            chunk_size=definition.chunk_size,
            dataset=definition.dataset,
            target_base_path=definition.target_base_path,
            clickhouse_url=os.environ[definition.clickhouse_url_env],
            clickhouse_user=os.getenv(definition.clickhouse_user_env, "default"),
            clickhouse_password=os.getenv(definition.clickhouse_password_env, ""),
            clickhouse_database=definition.clickhouse_database,
        )
        return run_batch_pipeline(config)

    PythonOperator(
        task_id="run_batch_elt",
        python_callable=_run,
        dag=dag,
    )

    return dag
