"""Tests for the robustness fixes in commit ac79c77.

Covers: the UIDVALIDITY guard (record at cache time, refuse stale UIDs at
execute time), the COPY+STORE partial-failure retry (no duplicate COPY),
mbox failed-message preservation, resume-log UID normalization/batched
saves, and deferred stale-header deletes during evaluation.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

if "tqdm" not in sys.modules:  # pragma: no cover - test support
    tqdm_module = types.ModuleType("tqdm")

    class _StubTqdm:
        def __init__(self, iterable=None, *args, **kwargs) -> None:
            self._iterable = list(iterable or [])
            self.total = kwargs.get("total")

        def __iter__(self):
            return iter(self._iterable)

        def update(self, *args, **kwargs) -> None:
            return None

        def close(self) -> None:
            return None

        def set_postfix_str(self, *args, **kwargs) -> None:
            return None

    def _write(message: str) -> None:
        print(message)

    def _tqdm(*args, **kwargs):
        return _StubTqdm(*args, **kwargs)

    tqdm_module.tqdm = _tqdm
    tqdm_module.write = _write
    _tqdm.write = _write
    sys.modules["tqdm"] = tqdm_module

from core.cache_builder import build_cache, _read_uidvalidity_rows
from core.database import init_db
from core.executor import execute_actions
from core.executor.helpers import _uidvalidity_mismatch
from core.executor.operations import _perform_batch_move_operations
from core.imap_client import get_selected_uidvalidity
from core.logging_utils import JsonLogger
from core.mbox_importer import _preserve_failed_message
from core.rule_engine import evaluate_rules
from core.stream_resume import ResumeLog


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _BaseClient:
    """Minimal IMAP fake: OK for everything, records uid() commands."""

    def __init__(self, uidvalidity: str | None = None) -> None:
        self.uidvalidity = uidvalidity
        self.commands: list[tuple[str, str]] = []
        self.capabilities: tuple[bytes, ...] = (b"IMAP4REV1",)
        self.selected: str | None = None

    def select(self, folder: str, readonly: bool = False):
        self.selected = folder.strip('"')
        return "OK", [b"1"]

    def response(self, code: str):
        if code == "UIDVALIDITY" and self.uidvalidity is not None:
            return "UIDVALIDITY", [self.uidvalidity.encode()]
        return code, [None]

    def uid(self, command: str, uid, *args):
        uid_value = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
        self.commands.append((command, uid_value))
        return "OK", [b""]

    def expunge(self):
        return "OK", []

    def close(self):
        self.selected = None
        return "OK", []


class _CopyOkStoreFlakyClient(_BaseClient):
    """COPY always lands; batched STORE fails; single STOREs succeed per-UID."""

    def __init__(self, store_ok_uids: set[str], uidvalidity: str | None = None) -> None:
        super().__init__(uidvalidity)
        self.store_ok_uids = store_ok_uids

    def uid(self, command: str, uid, *args):
        uid_value = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
        self.commands.append((command, uid_value))
        if command == "STORE" and args and args[0] == "+FLAGS":
            if "," in uid_value:
                return "NO", [b"STORE failed"]
            if uid_value in self.store_ok_uids:
                return "OK", [b""]
            return "NO", [b"STORE failed"]
        return "OK", [b""]


def _make_db(tmp_path: Path):
    logger = JsonLogger(tmp_path / "log.jsonl")
    db = init_db(tmp_path / "db.sqlite3", logger=logger)
    return db, logger


def _insert_actions(db, actions: list[tuple[str, str, str, str, int]]) -> None:
    with db:
        db.executemany(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?, ?, ?, ?, ?, 'pending', '2026-01-01T00:00:00Z', 'move', NULL)",
            actions,
        )


def _set_cached_uidvalidity(db, folder: str, value: str) -> None:
    with db:
        db.execute(
            "INSERT OR REPLACE INTO folder_uidvalidity (folder, uidvalidity, updated_at) VALUES (?,?,?)",
            (folder, value, "2026-01-01T00:00:00Z"),
        )


# ---------------------------------------------------------------------------
# get_selected_uidvalidity / _uidvalidity_mismatch
# ---------------------------------------------------------------------------


def test_get_selected_uidvalidity_reads_response():
    assert get_selected_uidvalidity(_BaseClient(uidvalidity="424242")) == "424242"


def test_get_selected_uidvalidity_absent_or_unsupported():
    assert get_selected_uidvalidity(_BaseClient(uidvalidity=None)) is None
    assert get_selected_uidvalidity(object()) is None  # no response() at all


def test_uidvalidity_mismatch_detection(tmp_path: Path):
    db, _logger = _make_db(tmp_path)

    # No snapshot cached -> no mismatch (pre-guard caches keep working)
    assert _uidvalidity_mismatch(db, _BaseClient(uidvalidity="222"), "INBOX") is None

    _set_cached_uidvalidity(db, "INBOX", "111")
    assert _uidvalidity_mismatch(db, _BaseClient(uidvalidity="111"), "INBOX") is None
    assert _uidvalidity_mismatch(db, _BaseClient(uidvalidity="222"), "INBOX") == ("111", "222")

    # Server does not report UIDVALIDITY -> no mismatch
    assert _uidvalidity_mismatch(db, _BaseClient(uidvalidity=None), "INBOX") is None
    db.close()


# ---------------------------------------------------------------------------
# build_cache records UIDVALIDITY
# ---------------------------------------------------------------------------


class _FetchClient(_BaseClient):
    """Serves one header per UID in a batched FETCH."""

    def uid(self, command: str, uid, *args):
        if command == "FETCH":
            uid_set = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            response: list = []
            for seq, u in enumerate(uid_set.split(","), 1):
                header = b"Subject: Test\r\n\r\n"
                response.append((f"{seq} (UID {u} BODY[HEADER] {{{len(header)}}}".encode(), header))
                response.append(b")")
            return "OK", response
        return super().uid(command, uid, *args)


def test_build_cache_records_uidvalidity(monkeypatch, tmp_path: Path):
    db, logger = _make_db(tmp_path)
    client = _FetchClient(uidvalidity="424242")
    monkeypatch.setattr(
        "core.cache_builder.safe_search_all", lambda _client, **_kwargs: [b"1", b"2"]
    )

    _timer, folders, messages = build_cache(
        client, db, ["INBOX"], show_progress=False, logger=logger, limit=None, order="newest"
    )

    assert (folders, messages) == (1, 2)
    row = db.execute(
        "SELECT uidvalidity FROM folder_uidvalidity WHERE folder='INBOX'"
    ).fetchone()
    assert row == ("424242",)
    db.close()


def test_read_uidvalidity_rows_tolerates_missing_table(tmp_path: Path):
    import sqlite3

    bare = sqlite3.connect(tmp_path / "old.sqlite3")  # worker DB from an older build
    assert _read_uidvalidity_rows(bare) == []
    bare.close()

    db, _logger = _make_db(tmp_path)
    _set_cached_uidvalidity(db, "INBOX", "7")
    assert _read_uidvalidity_rows(db) == [("INBOX", "7", "2026-01-01T00:00:00Z")]
    db.close()


# ---------------------------------------------------------------------------
# execute_actions refuses stale UIDs on UIDVALIDITY mismatch
# ---------------------------------------------------------------------------


def test_execute_actions_blocks_on_uidvalidity_mismatch(tmp_path: Path):
    db, logger = _make_db(tmp_path)
    _insert_actions(db, [("1", "INBOX", "rule", "Archive", 100), ("2", "INBOX", "rule", "Archive", 100)])
    _set_cached_uidvalidity(db, "INBOX", "111")
    client = _BaseClient(uidvalidity="222")

    _timer, stats = execute_actions(
        client, db, show_progress=False, dry_run=False, strict=False, logger=logger, verbose=False
    )

    assert stats["failed"] == 2
    assert stats["done"] == 0
    rows = db.execute("SELECT status, error_message FROM actions").fetchall()
    for status, error_message in rows:
        assert status == "failed"
        assert "UIDVALIDITY changed" in error_message
    # No message was touched: nothing moved, copied, or flagged deleted
    touched = [cmd for cmd, _ in client.commands if cmd in ("MOVE", "COPY", "STORE")]
    assert touched == []
    db.close()


def test_execute_actions_proceeds_on_uidvalidity_match(tmp_path: Path):
    db, logger = _make_db(tmp_path)
    _insert_actions(db, [("1", "INBOX", "rule", "Archive", 100)])
    _set_cached_uidvalidity(db, "INBOX", "111")
    client = _BaseClient(uidvalidity="111")

    _timer, stats = execute_actions(
        client, db, show_progress=False, dry_run=False, strict=False, logger=logger, verbose=False
    )

    assert stats["done"] == 1
    assert stats["failed"] == 0
    db.close()


# ---------------------------------------------------------------------------
# COPY+STORE partial failure: STORE-only retry, never a second COPY
# ---------------------------------------------------------------------------


def test_serial_copy_store_retry_does_not_duplicate_copy(tmp_path: Path):
    db, logger = _make_db(tmp_path)
    _insert_actions(db, [("10", "INBOX", "rule", "Archive", 100), ("20", "INBOX", "rule", "Archive", 100)])
    client = _CopyOkStoreFlakyClient(store_ok_uids={"10"})

    _timer, stats = execute_actions(
        client, db, show_progress=False, dry_run=False, strict=False, logger=logger, verbose=False
    )

    # One COPY for the batch; the recovery retried only the STOREs
    copies = [uids for cmd, uids in client.commands if cmd == "COPY"]
    assert copies == ["10,20"]

    assert stats["done"] == 1
    assert stats["failed"] == 1
    status_by_uid = {
        uid: (status, error_message)
        for uid, status, error_message in db.execute(
            "SELECT uid, status, error_message FROM actions"
        )
    }
    assert status_by_uid["10"][0] == "done"
    assert status_by_uid["20"][0] == "failed"
    assert "both source and target" in status_by_uid["20"][1]
    db.close()


def test_worker_batch_move_store_retry(tmp_path: Path):
    db, logger = _make_db(tmp_path)
    _insert_actions(db, [("10", "INBOX", "rule", "Archive", 100), ("20", "INBOX", "rule", "Archive", 100)])
    actions = [
        {"id": row_id, "uid": uid, "folder": "INBOX"}
        for row_id, uid in db.execute("SELECT id, uid FROM actions ORDER BY uid")
    ]
    client = _CopyOkStoreFlakyClient(store_ok_uids={"10"})

    done, failed, _successful = _perform_batch_move_operations(
        client, db, "INBOX", "Archive", actions, supports_uid_move=False, logger=logger
    )

    copies = [uids for cmd, uids in client.commands if cmd == "COPY"]
    assert copies == ["10,20"]
    assert (done, failed) == (1, 1)
    status_by_uid = {
        uid: (status, error_message)
        for uid, status, error_message in db.execute(
            "SELECT uid, status, error_message FROM actions"
        )
    }
    assert status_by_uid["10"][0] == "done"
    assert status_by_uid["20"][0] == "failed"
    assert "both source and target" in status_by_uid["20"][1]
    db.close()


# ---------------------------------------------------------------------------
# mbox import: failed messages only marked handled once preserved
# ---------------------------------------------------------------------------


class _ErrMbox:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.added: list = []

    def add(self, msg) -> None:
        if self.fail:
            raise OSError("disk full")
        self.added.append(msg)


def test_preserve_failed_message(tmp_path: Path):
    logger = JsonLogger(tmp_path / "log.jsonl")
    msg = object()

    ok_mbox = _ErrMbox(fail=False)
    assert _preserve_failed_message(ok_mbox, None, msg, logger, "INBOX") is True
    assert ok_mbox.added == [msg]

    # Error mbox write fails -> message must NOT be marked handled
    assert _preserve_failed_message(_ErrMbox(fail=True), None, msg, logger, "INBOX") is False
    # No error mbox configured, or nothing to preserve -> not handled
    assert _preserve_failed_message(None, None, msg, logger, "INBOX") is False
    assert _preserve_failed_message(ok_mbox, None, None, logger, "INBOX") is False


# ---------------------------------------------------------------------------
# ResumeLog: bytes/str normalization and batched saves
# ---------------------------------------------------------------------------


def test_resume_log_normalizes_bytes_uids(tmp_path: Path):
    log = ResumeLog(tmp_path / "resume.json")
    log.mark_processed("INBOX", b"123")
    assert log.is_processed("INBOX", "123")
    assert log.is_processed("INBOX", b"123")
    assert log.is_processed("INBOX", 123)

    log.mark_processed_batch({"Archive": [b"7", "8"]})
    assert log.is_processed("Archive", "7")
    assert log.is_processed("Archive", b"8")


def test_resume_log_batches_disk_writes(tmp_path: Path):
    path = tmp_path / "resume.json"
    log = ResumeLog(path)
    for i in range(ResumeLog.SAVE_INTERVAL - 1):
        log.mark_processed("INBOX", str(i))
    assert not path.exists()  # below the interval: nothing persisted yet

    log.mark_processed("INBOX", "last")
    assert path.exists()  # interval reached: autosaved

    log.mark_processed("INBOX", "extra")
    log.flush()
    reloaded = ResumeLog(path)
    assert reloaded.is_processed("INBOX", "extra")
    assert sum(reloaded.stats().values()) == ResumeLog.SAVE_INTERVAL + 1

    log.clear()
    assert not path.exists()
    assert log.stats() == {}


# ---------------------------------------------------------------------------
# evaluate_rules: same-folder header deletes deferred past the scan
# ---------------------------------------------------------------------------


def test_evaluate_rules_defers_same_folder_header_deletes(tmp_path: Path):
    db, logger = _make_db(tmp_path)
    rule = {
        "name": "Keep in Archive",
        "priority": 100,
        "conditions": {"header": "subject", "contains": "Hello"},
        "action": {"type": "move", "target": "Archive"},
    }
    with db:
        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            [
                ("Archive", "1", json.dumps({"header": "Subject: Hello One\n\n"}), "2026-01-01T00:00:00Z"),
                ("Archive", "2", json.dumps({"header": "Subject: Hello Two\n\n"}), "2026-01-01T00:00:00Z"),
                ("INBOX", "3", json.dumps({"header": "Subject: Hello Three\n\n"}), "2026-01-01T00:00:00Z"),
            ],
        )

    _timer, _rule_count, matches = evaluate_rules(
        db, [rule], scope="all", dry_run=False, show_progress=False, logger=logger
    )

    assert matches == 3
    # Same-folder matches: header rows removed (after the scan), no action queued
    remaining = [f for (f,) in db.execute("SELECT folder FROM headers ORDER BY folder")]
    assert remaining == ["INBOX"]
    queued = db.execute("SELECT folder, uid, target FROM actions").fetchall()
    assert queued == [("INBOX", "3", "Archive")]
    db.close()
