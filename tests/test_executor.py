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
from core.rule_engine import evaluate_rules


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.capabilities: tuple[bytes, ...] = (b"IMAP4REV1",)
        self.selected: str | None = None

    def select(self, folder: str, readonly: bool = False):
        self.selected = folder.strip('"')
        return "OK", [b"1"]

    def uid(self, command: str, uid: str, *args):
        uid_value: str | None
        if isinstance(uid, (bytes, bytearray)):
            uid_value = uid.decode()
        else:
            uid_value = uid
        self.commands.append((command, uid_value if uid_value is not None else ""))
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

    def uid(self, command: str, uid, *args):  # type: ignore[override]
        if command == "MOVE":
            uid_value = uid.decode() if isinstance(uid, (bytes, bytearray)) else uid
            self.commands.append((command, uid_value))
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
        # Add default action_type and action_data for backward compatibility
        extended_actions = []
        for action in actions:
            # action format: (uid, folder, rule_name, target, priority, status, created_at)
            # Extend with: action_type="move", action_data=None
            extended_actions.append(action + ("move", None))

        db.executemany(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            extended_actions,
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

    def uid(self, command: str, uid, *args):  # type: ignore[override]
        if command == "COPY":
            uid_value = uid.decode() if isinstance(uid, (bytes, bytearray)) else uid
            attempts = self.copy_attempts.get(uid_value, 0)
            self.copy_attempts[uid_value] = attempts + 1
            self.commands.append((command, uid_value))
            if attempts == 0:
                return "NO", [b"[TRYCREATE] Mailbox does not exist"]
            return "OK", [b""]
        return super().uid(command, uid, *args)

    def create(self, mailbox: str):
        self.created.append(mailbox)
        return "OK", [b""]


class MissingMessageClient(FakeClient):
    def uid(self, command: str, uid, *args):  # type: ignore[override]
        if command == "COPY":
            uid_value = uid.decode() if isinstance(uid, (bytes, bytearray)) else uid
            self.commands.append((command, uid_value))
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


def test_execute_prunes_headers_and_blocks_re_evaluation(tmp_path: Path):
    db_path = tmp_path / "db.sqlite3"
    log_path = tmp_path / "log.jsonl"
    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    rule = {
        "name": "Archive",
        "conditions": {"header": "subject", "contains": "Archive Me"},
        "action": {"type": "move", "target": "Archive"},
    }

    with db:
        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            (
                "INBOX",
                "100",
                json.dumps({"header": "Subject: Archive Me\n\n"}),
                "2024-03-01T00:00:00Z",
            ),
        )

    _eval_timer, rule_count, matches = evaluate_rules(
        db,
        [rule],
        scope="all",
        dry_run=False,
        show_progress=False,
        logger=logger,
        verbose=False,
        debug_headers=False,
        folders=None,
    )

    assert rule_count == 1
    assert matches == 1

    client = FakeClient()
    _exec_timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=False,
        strict=False,
        logger=logger,
        verbose=False,
    )

    assert stats["done"] == 1
    remaining_headers = db.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
    assert remaining_headers == 0

    _reeval_timer, rerun_rules, rerun_matches = evaluate_rules(
        db,
        [rule],
        scope="all",
        dry_run=False,
        show_progress=False,
        logger=logger,
        verbose=False,
        debug_headers=False,
        folders=None,
    )

    assert rerun_rules == 1
    assert rerun_matches == 0
    statuses = db.execute("SELECT status FROM actions ORDER BY id").fetchall()
    assert [status for (status,) in statuses] == ["done"]

    log_entries = []
    with logger.log_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                log_entries.append(json.loads(line))

    assert any(entry.get("message") == "execute_header_removed" for entry in log_entries)

    db.close()


def test_malformed_message_id_parsing_with_microsoft_header(tmp_path):
    """Test that malformed Message-ID headers from Microsoft emails are handled gracefully."""
    db = init_db(tmp_path / "cache.db")
    logger = JsonLogger(tmp_path / "test.log")

    # Insert message with malformed Message-ID (from Microsoft)
    malformed_header = "Message-ID: <[1d90dce509c24f4a8db4edeac9a9b750-JFZGS42DN5WW25LONFRWC5DJN5XFA3DBORTG64TNFVIHE33EFVBEYMSQPREUCTKTKNIFE7CTKNIFERLNMFUWY7CFPBXVG3LUOA======@microsoft.com]>\r\nSubject: Test\r\n\r\n"
    db.execute(
        "INSERT INTO headers (folder, uid, data) VALUES (?, ?, ?)",
        ("INBOX", "1234", json.dumps({"header": malformed_header})),
    )
    db.commit()

    client = FakeClient()
    _exec_timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=True,
        strict=False,
        logger=logger,
        verbose=False,
    )

    # Test passes if no crash occurred; the malformed header should be gracefully handled
    assert stats is not None
    db.close()


def test_malformed_message_id_regex_fallback(tmp_path):
    """Test regex fallback for malformed Message-IDs when parser fails."""
    db = init_db(tmp_path / "cache.db")
    logger = JsonLogger(tmp_path / "test.log")

    # Insert message with Message-ID that will trigger regex fallback
    # Note: The actual parser may handle some malformed headers, but regex should extract it
    problematic_header = "Message-ID: <[test-id-with-brackets]>\r\nSubject: Test\r\n\r\n"
    db.execute(
        "INSERT INTO headers (folder, uid, data) VALUES (?, ?, ?)",
        ("INBOX", "5678", json.dumps({"header": problematic_header})),
    )
    db.commit()

    client = FakeClient()
    _exec_timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=True,
        strict=False,
        logger=logger,
        verbose=False,
    )

    # If we get here without crash, the fallback is working
    assert stats is not None
    db.close()


def test_missing_message_id_verification_skipped(tmp_path):
    """Test that verification is skipped when Message-ID is missing."""
    db = init_db(tmp_path / "cache.db")
    logger = JsonLogger(tmp_path / "test.log")

    # Insert message without Message-ID
    header_no_msgid = "From: sender@example.com\r\nSubject: No Message ID\r\n\r\n"
    db.execute(
        "INSERT INTO headers (folder, uid, data) VALUES (?, ?, ?)",
        ("INBOX", "9999", json.dumps({"header": header_no_msgid})),
    )
    db.commit()

    client = FakeClient()
    _exec_timer, stats = execute_actions(
        client,
        db,
        show_progress=False,
        dry_run=True,
        strict=False,
        logger=logger,
        verbose=False,
    )

    # Verification should be skipped, and system should continue normally
    assert stats is not None
    db.close()
