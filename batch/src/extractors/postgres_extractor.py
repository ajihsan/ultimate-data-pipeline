from __future__ import annotations

from collections.abc import Generator
from typing import Any

import psycopg2


class PostgresExtractor:
    """Extract rows from PostgreSQL using a server-side cursor.

    Args:
        dsn: PostgreSQL DSN string.
        query: SQL query to execute.
        chunk_size: Number of rows per fetch operation.
    """

    def __init__(self, dsn: str, query: str, chunk_size: int = 10_000) -> None:
        self.dsn = dsn
        self.query = query
        self.chunk_size = chunk_size

    def extract(self) -> Generator[list[dict[str, Any]], None, None]:
        """Yield query results in chunks.

        Yields:
            List[Dict[str, Any]]: A chunk of rows represented as dictionaries.
        """
        conn = psycopg2.connect(self.dsn)
        try:
            with conn.cursor(name="batch_server_cursor") as cursor:
                cursor.itersize = self.chunk_size
                cursor.execute(self.query)
                columns = [desc[0] for desc in cursor.description]

                while True:
                    rows = cursor.fetchmany(self.chunk_size)
                    if not rows:
                        break
                    yield [dict(zip(columns, row, strict=False)) for row in rows]
        finally:
            conn.close()
