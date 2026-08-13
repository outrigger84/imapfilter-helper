"""Tests for the batch mbox-import feature: shared connection pool across
multiple mbox files, per-(file, folder) work-item chunking, connection
SELECT-skip/CREATE-branch fix, and per-file cleanup timing.
"""
from __future__ import annotations

import email.message
import json
import mailbox
import threading
from collections import defaultdict
from pathlib import Path

import pytest

from core.logging_utils import JsonLogger
from core.mbox_importer import (
    WorkItem,
    _build_work_items,
    _ensure_folder,
    _order_work_items,
    _progress_path_for,
    _ThreadMboxHandleCache,
    run_mbox_import,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _mk_message(subject: str, message_id: str) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = "sender@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 01 Dec 2025 12:00:00 +0000"
    msg.set_content("body")
    return msg


def _make_mbox_file(path: Path, specs: list[tuple[str, str]]) -> Path:
    """specs: list of (subject, message_id) tuples."""
    box = mailbox.mbox(str(path))
    for subject, message_id in specs:
        box.add(_mk_message(subject, message_id))
    box.flush()
    box.close()
    return path


def _write_rule(rules_dir: Path, name: str, contains: str, target: str, priority: int = 10) -> None:
    (rules_dir / f"{name}.json").write_text(json.dumps({
        "name": name,
        "priority": priority,
        "enabled": True,
        "conditions": {"header": "subject", "contains": contains},
        "action": {"type": "move", "target": target},
    }))


def _secrets_file(tmp_path: Path) -> Path:
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps({
        "imap": {"host": "imap.example.com", "port": 993, "username": "test@example.com", "password": "pw"}
    }))
    return secrets_path


class FakeIMAPServer:
    """Shared state visible to every FakeIMAPClient, simulating one IMAP account."""

    def __init__(self, existing_folders=None, always_reject_append_for=None):
        self.existing_folders: set[str] = set(existing_folders or {"INBOX"})
        self.appended: dict[str, list[bytes]] = defaultdict(list)
        self.always_reject_append_for: set[str] = set(always_reject_append_for or set())
        self.lock = threading.Lock()


class FakeIMAPClient:
    """Minimal fake standing in for imaplib.IMAP4_SSL, backed by a FakeIMAPServer."""

    def __init__(self, server: FakeIMAPServer):
        self.server = server
        self.select_calls: list[str] = []
        self.selected: str | None = None

    def select(self, mailbox_name: str, readonly: bool = True):
        name = mailbox_name.strip('"')
        self.select_calls.append(name)
        with self.server.lock:
            exists = name in self.server.existing_folders
        if exists:
            self.selected = name
            return "OK", [b"1"]
        return "NO", [b"Mailbox does not exist"]

    def create(self, mailbox_name: str):
        name = mailbox_name.strip('"')
        with self.server.lock:
            self.server.existing_folders.add(name)
        return "OK", [b"CREATE completed"]

    def append(self, mailbox_name: str, flags, date_time, payload: bytes):
        name = mailbox_name.strip('"')
        if name in self.server.always_reject_append_for:
            return "NO", [b"Rejected"]
        with self.server.lock:
            self.server.appended[name].append(payload)
        return "OK", [b"APPEND completed"]

    def logout(self):
        return "OK", [b"BYE"]

    def shutdown(self):
        pass

    def _simple_command(self, name, *args):
        return "OK", [b""]


def _patch_imap_login(monkeypatch: pytest.MonkeyPatch, server: FakeIMAPServer) -> None:
    factory = lambda *args, **kwargs: FakeIMAPClient(server)
    monkeypatch.setattr("core.connection_pool.imap_login", factory)
    monkeypatch.setattr("core.mbox_importer.imap_login", factory)


# ---------------------------------------------------------------------------
# _progress_path_for: ENAMETOOLONG fallback for very long mbox filenames
# ---------------------------------------------------------------------------

def test_progress_path_for_normal_name_just_appends_suffix(tmp_path):
    mbox_path = tmp_path / "Folder2.mbox"

    progress_path = _progress_path_for(mbox_path)

    assert progress_path == tmp_path / "Folder2.mbox.progress"


def test_progress_path_for_near_limit_name_falls_back_to_short_hash(tmp_path):
    # 254-byte .mbox name (as seen in the real dataset) — appending ".progress"
    # (9 bytes) would push the on-disk filename over the 255-byte POSIX limit
    # and raise ENAMETOOLONG when opened.
    long_stem = "2026-07-29-" * 12 + "converted_merged.70001-80000-" + "error-" * 12
    long_name = (long_stem + "error.mbox")[:254 - len(".mbox")] + ".mbox"
    assert len(long_name.encode()) <= 254
    mbox_path = tmp_path / long_name

    progress_path = _progress_path_for(mbox_path)

    assert len(progress_path.name.encode("utf-8")) <= 255
    assert progress_path.parent == tmp_path
    assert progress_path.name.startswith(".mbox_import_progress_")
    assert progress_path.suffix == ".progress"
    # deterministic: calling it again for the same source path gives the same result
    assert _progress_path_for(mbox_path) == progress_path
    # a different source path gets a different fallback name (no collisions)
    other_path = tmp_path / (long_name[:-5] + "X.mbox")
    assert _progress_path_for(other_path) != progress_path


def test_run_mbox_import_handles_extremely_long_mbox_filename(tmp_path, monkeypatch):
    """Regression test: a real-world filename whose .progress derivative
    exceeds the filesystem's 255-byte limit must not crash the batch — the
    file should still classify, upload, and clean up normally.
    """
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    long_name = (
        "2026-07-29-2026-07-29-2026-07-26-2026-07-25-2026-07-21-2026-07-19-"
        "2026-07-18-2026-07-18-2026-07-16-2026-07-15-2026-07-15-2026-07-15-"
        "2026-07-11-converted_merged.70001-80000-error-error-error-error-"
        "error-error-error-error-error-error-error-error-error.mbox"
    )
    assert len(long_name.encode()) == 254  # matches the real failing file exactly
    file_a = _make_mbox_file(tmp_path / long_name, [("hello", "<a1@x>")])

    server = FakeIMAPServer(existing_folders={"INBOX"})
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=False,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
        parallel_workers=1,
    )

    assert rc == 0
    assert len(server.appended["INBOX"]) == 1
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 0
    assert not _progress_path_for(file_a).exists()  # cleared on a clean run


# ---------------------------------------------------------------------------
# _ensure_folder: CREATE-branch fix + SELECT-skip
# ---------------------------------------------------------------------------

def test_ensure_folder_create_branch_selects_before_returning():
    logger = JsonLogger(Path("/dev/null"))
    server = FakeIMAPServer(existing_folders=set())
    client = FakeIMAPClient(server)

    ok = _ensure_folder(client, "NewFolder", logger)

    assert ok is True
    # First select fails (folder doesn't exist yet), CREATE happens, then a
    # real second select must occur before _ensure_folder trusts/caches state.
    assert client.select_calls == ["NewFolder", "NewFolder"]
    assert client._ih_selected_folder == "NewFolder"
    assert client.selected == "NewFolder"


def test_ensure_folder_select_skip_reuses_cached_state():
    logger = JsonLogger(Path("/dev/null"))
    server = FakeIMAPServer(existing_folders={"INBOX"})
    client = FakeIMAPClient(server)

    assert _ensure_folder(client, "INBOX", logger) is True
    assert _ensure_folder(client, "INBOX", logger) is True

    assert client.select_calls == ["INBOX"]  # second call skipped the round trip


def test_ensure_folder_reselects_on_different_folder():
    logger = JsonLogger(Path("/dev/null"))
    server = FakeIMAPServer(existing_folders={"INBOX", "Archive"})
    client = FakeIMAPClient(server)

    assert _ensure_folder(client, "INBOX", logger) is True
    assert _ensure_folder(client, "Archive", logger) is True

    assert client.select_calls == ["INBOX", "Archive"]
    assert client._ih_selected_folder == "Archive"


# ---------------------------------------------------------------------------
# _build_work_items / _order_work_items
# ---------------------------------------------------------------------------

def test_build_work_items_never_merges_across_files():
    file_a = Path("/tmp/a.mbox")
    file_b = Path("/tmp/b.mbox")
    per_file = {
        file_a: {"Archive": [0, 1, 2]},
        file_b: {"Archive": [0, 1]},
    }

    items = _build_work_items(per_file, chunk_size=100)

    assert len(items) == 2
    by_file = {item.mbox_path: item for item in items}
    assert by_file[file_a].indices == [0, 1, 2]
    assert by_file[file_b].indices == [0, 1]


def test_build_work_items_splits_large_folder_by_chunk_size():
    file_a = Path("/tmp/a.mbox")
    per_file = {file_a: {"Archive": list(range(10))}}

    items = _build_work_items(per_file, chunk_size=4)

    assert [item.indices for item in items] == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert all(item.mbox_path == file_a and item.folder == "Archive" for item in items)
    # chunks are disjoint and index-complete
    all_indices = sorted(i for item in items for i in item.indices)
    assert all_indices == list(range(10))


def test_build_work_items_zero_chunk_size_means_unlimited():
    file_a = Path("/tmp/a.mbox")
    per_file = {file_a: {"Archive": list(range(10))}}

    items = _build_work_items(per_file, chunk_size=0)

    assert len(items) == 1
    assert items[0].indices == list(range(10))


def test_order_work_items_aggregates_across_files_most_first():
    file_a = Path("/tmp/a.mbox")
    file_b = Path("/tmp/b.mbox")
    items = [
        WorkItem(file_a, "Small", [0]),
        WorkItem(file_a, "Big", [0, 1]),
        WorkItem(file_b, "Big", [0, 1, 2]),  # Big's aggregate (5) beats Small's own-file-looking size
    ]

    ordered = _order_work_items(items, "most-first", default_folder="INBOX")

    assert [item.folder for item in ordered] == ["Big", "Big", "Small"]


def test_order_work_items_least_first():
    file_a = Path("/tmp/a.mbox")
    items = [
        WorkItem(file_a, "Big", [0, 1, 2]),
        WorkItem(file_a, "Small", [0]),
    ]

    ordered = _order_work_items(items, "least-first", default_folder="INBOX")

    assert [item.folder for item in ordered] == ["Small", "Big"]


def test_order_work_items_alpha_puts_default_folder_first():
    file_a = Path("/tmp/a.mbox")
    items = [
        WorkItem(file_a, "Zebra", [0]),
        WorkItem(file_a, "INBOX", [0]),
        WorkItem(file_a, "Apple", [0]),
    ]

    ordered = _order_work_items(items, "alpha", default_folder="INBOX")

    assert [item.folder for item in ordered] == ["INBOX", "Apple", "Zebra"]


# ---------------------------------------------------------------------------
# _ThreadMboxHandleCache
# ---------------------------------------------------------------------------

def test_thread_local_mbox_cache_reused_across_chunks(tmp_path, monkeypatch):
    mbox_path = _make_mbox_file(tmp_path / "a.mbox", [
        ("s1", "<1@x>"), ("s2", "<2@x>"), ("s3", "<3@x>"),
    ])

    calls: list[str] = []
    real_mbox_cls = mailbox.mbox

    def counting_mbox(path, *args, **kwargs):
        calls.append(path)
        return real_mbox_cls(path, *args, **kwargs)

    monkeypatch.setattr(mailbox, "mbox", counting_mbox)

    cache = _ThreadMboxHandleCache()
    try:
        h1 = cache.get(mbox_path)
        h2 = cache.get(mbox_path)
        h3 = cache.get(mbox_path)
        assert h1 is h2 is h3
        assert calls == [str(mbox_path)]
    finally:
        cache.close_all()


def test_thread_local_mbox_cache_separate_handle_per_thread(tmp_path):
    mbox_path = _make_mbox_file(tmp_path / "a.mbox", [("s1", "<1@x>")])
    cache = _ThreadMboxHandleCache()
    handles: list[int] = []
    lock = threading.Lock()

    def worker():
        handle = cache.get(mbox_path)
        with lock:
            handles.append(id(handle))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cache.close_all()

    assert len(set(handles)) == 3  # each thread got its own handle


# ---------------------------------------------------------------------------
# End-to-end: run_mbox_import over multiple files
# ---------------------------------------------------------------------------

def test_run_mbox_import_single_file_smoke(tmp_path, monkeypatch):
    """Guards the Path -> list[Path] signature change against the N=1 case."""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    file_a = _make_mbox_file(tmp_path / "a.mbox", [("hello", "<a1@x>")])

    server = FakeIMAPServer(existing_folders={"INBOX"})
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=False,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
        parallel_workers=1,
    )

    assert rc == 0
    assert len(server.appended["INBOX"]) == 1
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 0
    assert not file_a.with_suffix(file_a.suffix + ".progress").exists()


def test_run_mbox_import_batch_two_files_shared_pool(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    _write_rule(rules_dir, "to-archive", "ARCHIVE", "Archive")

    file_a = _make_mbox_file(tmp_path / "a.mbox", [
        ("ARCHIVE msg 1", "<a1@x>"),
        ("ARCHIVE msg 2", "<a2@x>"),
        ("other msg", "<a3@x>"),
    ])
    file_b = _make_mbox_file(tmp_path / "b.mbox", [
        ("ARCHIVE msg 3", "<b1@x>"),
    ])

    server = FakeIMAPServer(existing_folders={"INBOX", "Archive"})
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a, file_b],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=False,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
        parallel_workers=2,
        chunk_size=2,
    )

    assert rc == 0
    # 3 Archive messages total across both files (2 from a, 1 from b)
    assert len(server.appended["Archive"]) == 3
    assert len(server.appended["INBOX"]) == 1
    # each source mbox loses only its own uploaded messages
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 0
    assert sum(1 for _ in mailbox.mbox(str(file_b))) == 0
    # clean run removes both per-file progress files
    assert not file_a.with_suffix(file_a.suffix + ".progress").exists()
    assert not file_b.with_suffix(file_b.suffix + ".progress").exists()


def test_run_mbox_import_zero_work_item_file_still_gets_cleanup(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    file_a = _make_mbox_file(tmp_path / "a.mbox", [("skip me", "<a1@x>")])
    progress_a = file_a.with_suffix(file_a.suffix + ".progress")
    progress_a.write_text("<a1@x>\n")  # pre-seed: already uploaded in a prior run

    file_b = _make_mbox_file(tmp_path / "b.mbox", [("normal", "<b1@x>")])

    server = FakeIMAPServer(existing_folders={"INBOX"})
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a, file_b],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=False,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
        parallel_workers=2,
        chunk_size=10,
    )

    assert rc == 0
    # file_a contributed zero work items (fully covered by its progress file)
    # but its already-uploaded message must still be swept out of the source mbox.
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 0
    assert not progress_a.exists()
    assert sum(1 for _ in mailbox.mbox(str(file_b))) == 0
    # only file_b's message was actually appended this run
    assert len(server.appended["INBOX"]) == 1


def test_run_mbox_import_combined_error_mbox_across_files(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    _write_rule(rules_dir, "to-bad", "BAD", "BadFolder")

    file_a = _make_mbox_file(tmp_path / "a.mbox", [("BAD msg 1", "<a1@x>")])
    file_b = _make_mbox_file(tmp_path / "b.mbox", [("BAD msg 2", "<b1@x>")])

    server = FakeIMAPServer(
        existing_folders={"INBOX", "BadFolder"},
        always_reject_append_for={"BadFolder"},
    )
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a, file_b],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=False,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
        parallel_workers=2,
        chunk_size=10,
    )

    assert rc == 1
    err_files = list(tmp_path.glob("*-mbox-import-batch-error.mbox"))
    assert len(err_files) == 1
    failed_msgs = list(mailbox.mbox(str(err_files[0])))
    assert len(failed_msgs) == 2
    # both source mboxes still get drained — failed messages live in the error mbox now
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 0
    assert sum(1 for _ in mailbox.mbox(str(file_b))) == 0
    # progress files are kept (not cleared) since each file had a failure
    assert file_a.with_suffix(file_a.suffix + ".progress").exists()
    assert file_b.with_suffix(file_b.suffix + ".progress").exists()


def test_run_mbox_import_dry_run_multiple_files_no_upload(tmp_path, monkeypatch):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    file_a = _make_mbox_file(tmp_path / "a.mbox", [("hello", "<a1@x>")])
    file_b = _make_mbox_file(tmp_path / "b.mbox", [("world", "<b1@x>")])

    server = FakeIMAPServer(existing_folders={"INBOX"})
    _patch_imap_login(monkeypatch, server)
    logger = JsonLogger(tmp_path / "log.json")

    rc = run_mbox_import(
        mbox_paths=[file_a, file_b],
        rules_dir=rules_dir,
        secrets_path=_secrets_file(tmp_path),
        default_folder="INBOX",
        dry_run=True,
        verbose=False,
        limit=None,
        preserve_flags=True,
        error_mbox_path=None,
        logger=logger,
    )

    assert rc == 0
    assert server.appended == {}
    # dry-run must not touch the source files or write progress files
    assert sum(1 for _ in mailbox.mbox(str(file_a))) == 1
    assert sum(1 for _ in mailbox.mbox(str(file_b))) == 1
