from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class ParquetLoader:
    """Write extracted chunks to date-partitioned parquet files.

    Args:
        base_path: Root directory for dataset partitions.
    """

    def __init__(self, base_path: str) -> None:
        self.base_path = Path(base_path)

    def write_chunk(
        self,
        dataset: str,
        records: list[dict[str, Any]],
        execution_date: datetime,
        part_number: int,
    ) -> Path:
        """Write a chunk to parquet within a date partition.

        Args:
            dataset: Dataset name used in the directory path.
            records: Chunk of extracted rows.
            execution_date: Batch execution date for partitioning.
            part_number: Sequential chunk number.

        Returns:
            Path: Absolute path of the written parquet file.
        """
        partition = (
            self.base_path
            / dataset
            / f"year={execution_date:%Y}"
            / f"month={execution_date:%m}"
            / f"day={execution_date:%d}"
        )
        partition.mkdir(parents=True, exist_ok=True)

        file_path = partition / f"part-{part_number:05d}.parquet"
        table = pa.Table.from_pylist(records)
        pq.write_table(table, file_path)
        return file_path
