from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "tqdm" not in sys.modules:  # pragma: no cover - test support
    tqdm_stub = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **_kwargs):
            self._iterable = list(iterable or [])

        def __iter__(self):
            return iter(self._iterable)

        def set_postfix_str(self, *_args, **_kwargs):
            return None

        def update(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    def _write(*_args, **_kwargs):
        return None

    def _tqdm(iterable=None, **kwargs):
        return _DummyTqdm(iterable, **kwargs)

    _tqdm.write = _write  # type: ignore[attr-defined]

    tqdm_stub.tqdm = _tqdm
    tqdm_stub.write = _write
    sys.modules["tqdm"] = tqdm_stub

from core.cache_builder import build_cache
from core.database import init_db
from core.logging_utils import JsonLogger


def test_init_db_migrates_headers_table(tmp_path: Path):
    db_path = tmp_path / "imap.db"
    log_path = tmp_path / "log.json"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE headers (uid TEXT PRIMARY KEY, folder TEXT, data TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
        ("1", "Inbox", "{}", "2024-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)
    try:
        info = db.execute("PRAGMA table_info(headers)").fetchall()
        pk_columns = sorted(
            ((row[5], row[1]) for row in info if row[5]),
            key=lambda item: item[0],
        )
        assert [name for _, name in pk_columns] == ["folder", "uid"]

        rows = db.execute(
            "SELECT folder, uid, data, updated_at FROM headers ORDER BY folder, uid"
        ).fetchall()
        assert rows == [("Inbox", "1", "{}", "2024-01-01T00:00:00Z")]

        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
            ("Archive", "1", "{}", "2024-01-02T00:00:00Z"),
        )
        db.commit()
    finally:
        db.close()


def test_build_cache_stores_headers_per_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "imap.db"
    log_path = tmp_path / "log.json"
    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    folder_uids = {"Inbox": [b"1"], "Archive": [b"1"]}

    class FakeClient:
        def __init__(self) -> None:
            self.current_folder = ""

        def select(self, folder: str, readonly: bool = True):
            self.current_folder = folder.strip('"')
            return "OK", []

        def fetch(self, uid: bytes, _query: str):
            header = f"Subject: {self.current_folder} {uid.decode()}\r\n"
            return "OK", [(None, header.encode())]

    fake_client = FakeClient()

    def fake_safe_search_all(client):
        return folder_uids.get(client.current_folder, [])

    monkeypatch.setattr("core.cache_builder.safe_search_all", fake_safe_search_all)

    try:
        build_cache(
            fake_client,
            db,
            ["Inbox", "Archive"],
            show_progress=False,
            logger=logger,
            limit=None,
            order="newest",
            backup_enabled=False,
            backup_dir=tmp_path / "backups",
        )

        rows = db.execute(
            "SELECT folder, uid, data FROM headers ORDER BY folder, uid"
        ).fetchall()
        assert rows == [
            (
                "Archive",
                "1",
                '{"header": "Subject: Archive 1\\r\\n"}',
            ),
            (
                "Inbox",
                "1",
                '{"header": "Subject: Inbox 1\\r\\n"}',
            ),
        ]
    finally:
        db.close()


def test_build_cache_respects_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "imap.db"
    log_path = tmp_path / "log.json"
    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    class FakeClient:
        def __init__(self) -> None:
            self.current_folder = ""

        def select(self, folder: str, readonly: bool = True):
            self.current_folder = folder.strip('"')
            return "OK", []

        def fetch(self, uid: bytes, _query: str):
            header = f"Subject: {self.current_folder} {uid.decode()}\r\n"
            return "OK", [(None, header.encode())]

    fake_client = FakeClient()

    def fake_safe_search_all(client):
        return [b"1", b"2", b"3", b"4"]

    monkeypatch.setattr("core.cache_builder.safe_search_all", fake_safe_search_all)

    try:
        build_cache(
            fake_client,
            db,
            ["Inbox"],
            show_progress=False,
            logger=logger,
            limit=2,
            order="oldest",
            backup_enabled=False,
            backup_dir=tmp_path / "backups",
        )

        rows = db.execute(
            "SELECT folder, uid FROM headers ORDER BY uid"
        ).fetchall()
        assert rows == [("Inbox", "1"), ("Inbox", "2")]
    finally:
        db.close()
