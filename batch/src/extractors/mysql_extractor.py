from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from urllib.parse import urlparse

import aiomysql


class MySQLExtractor:
    """Extract rows from MySQL using a server-side cursor.

    Args:
        dsn: MySQL DSN in URL or host-style format.
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        conn: aiomysql.Connection | None = None
        cursor: aiomysql.SSCursor | None = None
        try:
            conn = loop.run_until_complete(self._open_connection())
            cursor = loop.run_until_complete(conn.cursor(aiomysql.SSCursor))
            loop.run_until_complete(cursor.execute(self.query))
            columns = [item[0] for item in cursor.description]

            while True:
                rows = loop.run_until_complete(cursor.fetchmany(self.chunk_size))
                if not rows:
                    break
                yield [dict(zip(columns, row, strict=False)) for row in rows]
        finally:
            if cursor is not None:
                loop.run_until_complete(cursor.close())
            if conn is not None:
                conn.close()
            loop.close()

    async def _open_connection(self) -> aiomysql.Connection:
        """Open a MySQL connection from the configured DSN."""
        parsed = _parse_mysql_dsn(self.dsn)
        return await aiomysql.connect(
            host=parsed["host"],
            port=parsed["port"],
            user=parsed["user"],
            password=parsed["password"],
            db=parsed["database"],
        )


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """Parse a MySQL DSN into aiomysql connection kwargs.

    Args:
        dsn: DSN string in URL or shorthand format.

    Returns:
        Dict[str, Any]: Parsed connection attributes.

    Raises:
        ValueError: If DSN does not contain required host/database parts.
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
