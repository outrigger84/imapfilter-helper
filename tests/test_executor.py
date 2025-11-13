from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from typing import List, Tuple

if "tqdm" not in sys.modules:
    tqdm_module = types.ModuleType("tqdm")

    class _StubTqdm:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D401 - simple stub
            self.total = kwargs.get("total")

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

from core.database import init_db
from core.executor import execute_actions
from core.logging_utils import JsonLogger


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.capabilities: tuple[bytes, ...] = (b"IMAP4REV1",)
        self.selected: str | None = None

    def select(self, folder: str, readonly: bool = False):
        self.selected = folder.strip('"')
        return "OK", [b"1"]

    def uid(self, command: str, uid: str, *args):
        self.commands.append((command, uid))
        return "OK", [b""]

    def expunge(self):
        return "OK", []

    def close(self):
        self.selected = None
        return "OK", []


class MoveCapableClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.capabilities = (b"MOVE",)

    def uid(self, command: str, uid: str, *args):  # type: ignore[override]
        if command == "MOVE":
            self.commands.append((command, uid))
            return "OK", [b""]
        return super().uid(command, uid, *args)


class VerifyingClient(MoveCapableClient):
    def __init__(self, *, source_has: bool, dest_has: bool) -> None:
        super().__init__()
        self.source_has = source_has
        self.dest_has = dest_has

    def uid(self, command: str, uid, *args):  # type: ignore[override]
        if command == "SEARCH":
            mailbox = (self.selected or "").strip()
            if mailbox == "INBOX":
                return "OK", [b"1"] if self.source_has else [b""]
            if mailbox == "Archive":
                return "OK", [b"1"] if self.dest_has else [b""]
            return "NO", [b""]
        return super().uid(command, uid, *args)


def _prepare_db_with_actions(
    tmp_path: Path,
    actions: List[Tuple[str, str, str, str | None, int, str, str]],
):
    logger = JsonLogger(tmp_path / "log.jsonl")
    db = init_db(tmp_path / "db.sqlite3", logger=logger)
    with db:
        db.executemany(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            actions,
        )
    return db, logger


def _prepare_db(tmp_path: Path):
    return _prepare_db_with_actions(
        tmp_path,
        [
            ("msg-1", "INBOX", "rule-1", "Archive", 100, "pending", "2024-01-01T00:00:00Z"),
            ("msg-2", "INBOX", "rule-1", "Archive", 200, "pending", "2024-01-02T00:00:00Z"),
            ("msg-3", "INBOX", "rule-2", "Archive", 150, "pending", "2024-01-03T00:00:00Z"),
        ],
    )


def _cache_header(db, folder: str, uid: str, message_id: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
        (
            folder,
            uid,
            json.dumps({"header": f"Message-ID: <{message_id}>\n"}),
            "2024-01-10T00:00:00Z",
        ),
    )


class MissingFolderClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[str] = []
        self.copy_attempts: dict[str, int] = {}

    def uid(self, command: str, uid: str, *args):  # type: ignore[override]
        if command == "COPY":
            attempts = self.copy_attempts.get(uid, 0)
            self.copy_attempts[uid] = attempts + 1
            self.commands.append((command, uid))
            if attempts == 0:
                return "NO", [b"[TRYCREATE] Mailbox does not exist"]
            return "OK", [b""]
        return super().uid(command, uid, *args)

    def create(self, mailbox: str):
        self.created.append(mailbox)
        return "OK", [b""]


class MissingMessageClient(FakeClient):
    def uid(self, command: str, uid: str, *args):  # type: ignore[override]
        if command == "COPY":
            self.commands.append((command, uid))
            return "NO", [b"NO such message"]
        return super().uid(command, uid, *args)


def test_execute_actions_respects_limit(tmp_path: Path):
    db, logger = _prepare_db(tmp_path)
    client = FakeClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
        limit=2,
    )

    assert stats["done"] == 2
    copy_commands = [cmd for cmd in client.commands if cmd[0] == "COPY"]
    assert len(copy_commands) == 2

    remaining = db.execute(
        "SELECT uid, status FROM actions ORDER BY uid"
    ).fetchall()
    status_by_uid = {uid: status for uid, status in remaining}
    assert status_by_uid["msg-1"] == "pending"
    assert status_by_uid["msg-2"] == "done"
    assert status_by_uid["msg-3"] == "done"

    db.close()


def test_execute_actions_logs_uid_completion(tmp_path: Path):
    db, logger = _prepare_db(tmp_path)
    client = FakeClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
    )

    assert stats["done"] == 3

    log_entries = []
    with logger.log_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                log_entries.append(json.loads(line))

    done_entries = [entry for entry in log_entries if entry.get("message") == "execute_uid_done"]

    assert len(done_entries) == 3
    assert all("context" in entry for entry in done_entries)
    assert {entry["context"]["uid"] for entry in done_entries} == {"msg-1", "msg-2", "msg-3"}

    db.close()


def test_execute_actions_prefers_uid_move(tmp_path: Path):
    db, logger = _prepare_db(tmp_path)
    client = MoveCapableClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
    )

    assert stats["done"] == 3

    move_commands = [cmd for cmd in client.commands if cmd[0] == "MOVE"]
    copy_commands = [cmd for cmd in client.commands if cmd[0] == "COPY"]
    store_commands = [cmd for cmd in client.commands if cmd[0] == "STORE"]

    assert len(move_commands) == 3
    assert not copy_commands
    assert not store_commands

    db.close()


def test_execute_actions_creates_missing_folder(tmp_path: Path):
    db, logger = _prepare_db_with_actions(
        tmp_path,
        [("msg-10", "INBOX", "rule", "Archive", 100, "pending", "2024-01-04T00:00:00Z")],
    )
    client = MissingFolderClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
    )

    assert stats["done"] == 1
    assert client.created == ['"Archive"']
    copy_commands = [cmd for cmd in client.commands if cmd[0] == "COPY"]
    assert len(copy_commands) == 2  # first attempt fails, second retries after create
    store_commands = [cmd for cmd in client.commands if cmd[0] == "STORE"]
    assert len(store_commands) == 1

    status = db.execute(
        "SELECT status FROM actions WHERE uid='msg-10'",
    ).fetchone()[0]
    assert status == "done"


def test_execute_actions_filters_folders(tmp_path: Path):
    db, logger = _prepare_db_with_actions(
        tmp_path,
        [
            ("msg-1", "Archive", "rule-archive", "Processed", 100, "pending", "2024-01-01T00:00:00Z"),
            ("msg-2", "INBOX", "rule-inbox", "Processed", 100, "pending", "2024-01-02T00:00:00Z"),
        ],
    )
    client = FakeClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
        folders=["Archive"],
    )

    assert stats["done"] == 1

    remaining = db.execute(
        "SELECT uid, folder, status FROM actions ORDER BY uid",
    ).fetchall()
    status_by_uid = {uid: status for uid, _folder, status in remaining}
    assert status_by_uid["msg-1"] == "done"
    assert status_by_uid["msg-2"] == "pending"

    db.close()


def test_execute_actions_logs_missing_uid_skip(tmp_path: Path):
    db, logger = _prepare_db_with_actions(
        tmp_path,
        [("missing-uid", "INBOX", "rule", "Archive", 100, "pending", "2024-01-05T00:00:00Z")],
    )
    client = MissingMessageClient()

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=True,
    )

    assert stats["skipped"] == 1

    status = db.execute(
        "SELECT status FROM actions WHERE uid='missing-uid'",
    ).fetchone()[0]
    assert status == "skipped"

    log_entries = []
    with logger.log_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                log_entries.append(json.loads(line))

    skip_entries = [entry for entry in log_entries if entry.get("message") == "message_missing_skipped"]

    assert len(skip_entries) == 1
    skip_context = skip_entries[0]["context"]
    assert skip_context["uid"] == "missing-uid"
    assert skip_context["folder"] == "INBOX"
    assert skip_context["target"] == "Archive"
    assert "no such message" in skip_context["error"].lower()

    db.close()


def test_execute_actions_verifies_moves_success(tmp_path: Path):
    db, logger = _prepare_db_with_actions(
        tmp_path,
        [("verify-ok", "INBOX", "rule", "Archive", 100, "pending", "2024-02-01T00:00:00Z")],
    )
    with db:
        _cache_header(db, "INBOX", "verify-ok", "success@example.com")
    client = VerifyingClient(source_has=False, dest_has=True)

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
        verify_moves=True,
    )

    assert stats["done"] == 1
    status = db.execute(
        "SELECT status FROM actions WHERE uid='verify-ok'",
    ).fetchone()[0]
    assert status == "done"

    db.close()


def test_execute_actions_verifies_moves_failure(tmp_path: Path):
    db, logger = _prepare_db_with_actions(
        tmp_path,
        [("verify-missing", "INBOX", "rule", "Archive", 100, "pending", "2024-02-02T00:00:00Z")],
    )
    with db:
        _cache_header(db, "INBOX", "verify-missing", "missing@example.com")
    client = VerifyingClient(source_has=False, dest_has=False)

    _timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=True,
        verify_moves=True,
    )

    assert stats["failed"] == 1
    status = db.execute(
        "SELECT status FROM actions WHERE uid='verify-missing'",
    ).fetchone()[0]
    assert status == "failed"

    log_entries = []
    with logger.log_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                log_entries.append(json.loads(line))

    failure_entries = [
        entry for entry in log_entries if entry.get("message") == "execute_verify_failed"
    ]
    assert failure_entries
    issues = failure_entries[0]["context"].get("issues", [])
    assert "destination_missing" in issues

    db.close()
