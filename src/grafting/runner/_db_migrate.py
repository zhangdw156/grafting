"""Shared SQLite schema migration helpers."""

from __future__ import annotations

import re
import sqlite3

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_TYPE = re.compile(r"^[A-Za-z0-9_ (),.]+$")


def _check_ident(name: str, label: str) -> None:
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Unsafe SQL identifier for {label}: {name!r}")


def add_columns_if_missing(
    conn: sqlite3.Connection,
    table: str,
    columns: list[tuple[str, str]],
) -> None:
    """Add columns to *table*, silently skipping any that already exist."""
    _check_ident(table, "table")
    for col_name, col_type in columns:
        _check_ident(col_name, "column name")
        if not _SAFE_TYPE.match(col_type):
            raise ValueError(f"Unsafe SQL type expression: {col_type!r}")
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    conn.commit()
