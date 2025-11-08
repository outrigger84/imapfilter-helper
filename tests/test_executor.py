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

    def select(self, folder: str):
        return "OK", [b"1"]

    def uid(self, command: str, uid: str, *args):
        self.commands.append((command, uid))
        return "OK", [b""]

    def expunge(self):
        return "OK", []


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

    db.close()
