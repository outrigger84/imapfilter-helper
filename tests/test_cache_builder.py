from __future__ import annotations

import re
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
from core.config import build_default_config
from core.database import init_db
from core.logging_utils import JsonLogger


class _FakeClient:
    def __init__(self, message_bytes: bytes):
        self._message_bytes = message_bytes
        self.fetch_calls: list[tuple[bytes, str]] = []

    def select(self, mailbox: str, readonly: bool = True):
        assert readonly is True
        return "OK", None

    def fetch(self, uid: bytes, query: str):
        self.fetch_calls.append((uid, query))
        if query == "(BODY.PEEK[HEADER])":
            header = b"Subject: Test\n\n"
            return "OK", [(None, header)]
        if query == "(BODY.PEEK[])":
            return "OK", [(None, self._message_bytes)]
        raise AssertionError(f"Unexpected query {query}")


@pytest.fixture()
def cache_context(tmp_path: Path):
    cfg = build_default_config(tmp_path)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = JsonLogger(cfg.paths.log_file)
    db = init_db(cfg.paths.db_file, logger=logger)
    try:
        yield cfg, db, logger
    finally:
        db.close()


def test_build_cache_without_backup(monkeypatch, cache_context):
    cfg, db, logger = cache_context
    client = _FakeClient(b"Subject: Test\n\nBody")

    monkeypatch.setattr("core.cache_builder.safe_search_all", lambda _client: [b"1", b"2"])

    timer, folders, messages = build_cache(
        client,
        db,
        ["INBOX"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
        backup_enabled=False,
        backup_dir=cfg.paths.backup_dir,
    )

    assert folders == 1
    assert messages == 2
    assert timer.count == 2
    assert all(query == "(BODY.PEEK[HEADER])" for _, query in client.fetch_calls)
    assert not cfg.paths.backup_dir.exists()

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM headers WHERE folder='INBOX'")
    (count,) = cur.fetchone()
    assert count == 2


def test_build_cache_with_backup(monkeypatch, cache_context):
    cfg, db, logger = cache_context
    client = _FakeClient(b"Subject: Backup\n\nSaved body")

    monkeypatch.setattr("core.cache_builder.safe_search_all", lambda _client: [b"1"])

    timer, folders, messages = build_cache(
        client,
        db,
        ["INBOX"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
        backup_enabled=True,
        backup_dir=cfg.paths.backup_dir,
    )

    assert folders == 1
    assert messages == 1
    assert timer.count == 1
    assert any(query == "(BODY.PEEK[])" for _, query in client.fetch_calls)

    files = list(cfg.paths.backup_dir.glob("INBOX_*.mbox"))
    assert len(files) == 1
    assert re.fullmatch(r"INBOX_\d{8}T\d{6}Z\.mbox", files[0].name)
    assert files[0].stat().st_size > 0

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM headers WHERE folder='INBOX'")
    (count,) = cur.fetchone()
    assert count == 1
