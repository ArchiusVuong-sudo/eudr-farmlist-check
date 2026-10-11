"""All SQL lives in sql/*.sql and is loaded here by aiosql (no ORM)."""

from __future__ import annotations

from typing import Any

import aiosql

from . import SQL_DIR

Q: Any = aiosql.from_path(SQL_DIR, "psycopg", mandatory_parameters=False)


def returned_id(value: Any) -> int:
    """aiosql's insert-returning gives a row; with dict rows that is {'id': n}."""
    if isinstance(value, dict):
        return int(value["id"])
    if isinstance(value, (tuple, list)):
        return int(value[0])
    return int(value)
