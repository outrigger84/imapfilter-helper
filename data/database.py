"""Database helpers for the IMAPFilter helper."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from core.logging_utils import JsonLogger


def init_db(path: Path) -> sqlite3.Connection:
    """Initialise the sqlite database and apply lightweight migrations."""
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS folders "
        "(id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS headers "
        "(uid TEXT PRIMARY KEY, folder TEXT, data TEXT, updated_at TEXT)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS actions ("
        "id INTEGER PRIMARY KEY, uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
        "priority INTEGER, status TEXT, created_at TEXT, executed_at TEXT)"
    )
    _ensure_column(db, "actions", "priority", "INTEGER", default=100)
    _ensure_column(db, "actions", "executed_at", "TEXT", default=None)
    db.commit()
    return db


def _ensure_column(
    db: sqlite3.Connection,
    table: str,
    column: str,
    coltype: str,
    *,
    default: Optional[int | str],
    logger: Optional[JsonLogger] = None,
) -> None:
    cur = db.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cur.fetchall()}
    if column in columns:
        return

    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    if default is not None:
        db.execute(f"UPDATE {table} SET {column}=? WHERE {column} IS NULL", (default,))
    db.commit()
    if logger:
        logger.log("INFO", "schema_column_added", {"table": table, "column": column})
