"""Database helpers for the IMAPFilter helper."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from core.logging_utils import JsonLogger


def init_db(path: Path, *, logger: Optional[JsonLogger] = None) -> sqlite3.Connection:
    """Initialise the sqlite database and apply lightweight migrations."""
    db = sqlite3.connect(path, timeout=30.0)

    # Enable WAL mode for better concurrency (production-standard SQLite configuration)
    current_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    if current_mode.lower() != "wal":
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")  # Faster than FULL, still safe for modern filesystems
        if logger:
            logger.log(
                "INFO",
                "database_wal_enabled",
                {"from": current_mode, "to": "wal"},
                console="📊 Enabled WAL mode for concurrent database access"
            )

    db.execute(
        "CREATE TABLE IF NOT EXISTS folders "
        "(id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)"
    )
    _ensure_headers_table(db, logger=logger)
    db.execute(
        "CREATE TABLE IF NOT EXISTS actions ("
        "id INTEGER PRIMARY KEY, uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
        "priority INTEGER, status TEXT, created_at TEXT, executed_at TEXT)"
    )
    _ensure_column(db, "actions", "priority", "INTEGER", default=100, logger=logger)
    _ensure_column(db, "actions", "executed_at", "TEXT", default=None, logger=logger)
    _ensure_column(db, "actions", "action_type", "TEXT", default="move", logger=logger)
    _ensure_column(db, "actions", "action_data", "TEXT", default=None, logger=logger)
    _ensure_column(db, "actions", "error_message", "TEXT", default=None, logger=logger)
    db.execute(
        "CREATE TABLE IF NOT EXISTS folder_uidvalidity "
        "(folder TEXT PRIMARY KEY, uidvalidity TEXT, updated_at TEXT)"
    )
    db.commit()
    return db


def _ensure_headers_table(db: sqlite3.Connection, *, logger: Optional[JsonLogger] = None) -> None:
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='headers'"
    )
    if cur.fetchone() is None:
        db.execute(
            "CREATE TABLE headers "
            "(folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
            "PRIMARY KEY (folder, uid))"
        )
        db.commit()
        if logger:
            logger.log("INFO", "schema_table_created", {"table": "headers"})
        return

    info = db.execute("PRAGMA table_info(headers)").fetchall()
    existing_columns = {row[1] for row in info}
    pk_columns = sorted(
        ((row[5], row[1]) for row in info if row[5]),
        key=lambda item: item[0],
    )
    if (
        {"folder", "uid", "data", "updated_at"}.issubset(existing_columns)
        and [name for _, name in pk_columns] == ["folder", "uid"]
    ):
        return

    select_columns = []
    for column in ("folder", "uid", "data", "updated_at"):
        if column in existing_columns:
            select_columns.append(column)
        else:
            select_columns.append(f"NULL AS {column}")
    select_clause = ", ".join(select_columns)

    with db:
        db.execute("ALTER TABLE headers RENAME TO headers_old")
        db.execute(
            "CREATE TABLE headers "
            "(folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
            "PRIMARY KEY (folder, uid))"
        )
        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) "
            f"SELECT {select_clause} FROM headers_old"
        )
        db.execute("DROP TABLE headers_old")

    if logger:
        logger.log(
            "INFO",
            "schema_table_migrated",
            {"table": "headers", "primary_key": ["folder", "uid"]},
        )


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
