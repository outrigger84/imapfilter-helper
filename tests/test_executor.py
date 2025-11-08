from __future__ import annotations

from pathlib import Path

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


def _prepare_db(tmp_path: Path):
    logger = JsonLogger(tmp_path / "log.jsonl")
    db = init_db(tmp_path / "db.sqlite3", logger=logger)
    with db:
        db.executemany(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("msg-1", "INBOX", "rule-1", "Archive", 100, "pending", "2024-01-01T00:00:00Z"),
                ("msg-2", "INBOX", "rule-1", "Archive", 200, "pending", "2024-01-02T00:00:00Z"),
                ("msg-3", "INBOX", "rule-2", "Archive", 150, "pending", "2024-01-03T00:00:00Z"),
            ],
        )
    return db, logger


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
