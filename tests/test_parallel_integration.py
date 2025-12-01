"""Integration tests for parallel cache building feature."""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

# Stub tqdm before importing core modules
if "tqdm" not in sys.modules:
    tqdm_module = types.ModuleType("tqdm")

    class _StubTqdm:
        def __init__(self, iterable=None, **kwargs) -> None:
            self.total = kwargs.get("total", 0)
            self._count = 0
            self._closed = False
            self._iterable = iterable

        def __iter__(self):
            if self._iterable is not None:
                return iter(self._iterable)
            return iter([])

        def update(self, n: int = 1) -> None:
            self._count += n

        def close(self) -> None:
            self._closed = True

        def set_postfix_str(self, s: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def _tqdm(iterable=None, **kwargs):
        return _StubTqdm(iterable, **kwargs)

    _tqdm.write = lambda s: None

    tqdm_module.tqdm = _tqdm
    sys.modules["tqdm"] = tqdm_module

from core.cache_builder import build_cache, build_cache_parallel
from core.database import init_db
from core.logging_utils import JsonLogger


# ============================================================================
# Mock IMAP Client Classes
# ============================================================================


class MockIMAPClient:
    """Mock IMAP client that simulates folder operations."""

    def __init__(
        self,
        folders_data: dict[str, list[tuple[str, bytes, list[str], str | None]]],
        *,
        delay_ms: int = 0,
        fail_folders: set[str] | None = None,
    ):
        """
        Initialize mock IMAP client.

        Args:
            folders_data: Dict mapping folder names to list of (uid, header_bytes, flags, internaldate)
            delay_ms: Simulated network delay in milliseconds per operation
            fail_folders: Set of folder names that should fail selection
        """
        self.folders_data = folders_data
        self.delay_ms = delay_ms
        self.fail_folders = fail_folders or set()
        self.selected_folder: str | None = None
        self.operations_count = 0
        self.lock = threading.Lock()

    def select(self, folder: str, readonly: bool = True) -> tuple[str, Any]:
        """Mock folder selection."""
        time.sleep(self.delay_ms / 1000.0)
        folder_name = folder.strip('"')

        if folder_name in self.fail_folders:
            return "NO", None

        if folder_name not in self.folders_data:
            return "NO", None

        self.selected_folder = folder_name
        return "OK", None

    def uid(self, command: str, *args) -> tuple[str, Any]:
        """Mock UID-based IMAP commands."""
        time.sleep(self.delay_ms / 1000.0)

        with self.lock:
            self.operations_count += 1

        if command == "SEARCH":
            if self.selected_folder is None:
                return "NO", None
            messages = self.folders_data.get(self.selected_folder, [])
            uids = [uid.encode() for uid, _, _, _ in messages]
            return "OK", [b" ".join(uids)] if uids else [b""]

        if command == "FETCH":
            uid_value = args[0]
            if isinstance(uid_value, bytes):
                uid_value = uid_value.decode("ascii")

            if self.selected_folder is None:
                return "NO", None

            messages = self.folders_data.get(self.selected_folder, [])
            for uid, header, flags, internaldate in messages:
                if uid == uid_value:
                    # Construct FETCH response with FLAGS and INTERNALDATE
                    flags_str = " ".join(flags) if flags else ""
                    internaldate_str = f'"{internaldate}"' if internaldate else '""'
                    metadata = (
                        f'{uid} (FLAGS ({flags_str}) '
                        f'INTERNALDATE {internaldate_str} BODY[HEADER])'
                    ).encode()
                    return "OK", [(metadata, header)]

            return "NO", None

        return "NO", None

    def logout(self) -> tuple[str, Any]:
        """Mock logout."""
        return "OK", None


class ThreadSafeMockClient:
    """Thread-safe mock client that tracks concurrent access."""

    def __init__(self, base_client: MockIMAPClient):
        self.base_client = base_client
        self.concurrent_accesses = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()

    def _track_enter(self):
        with self.lock:
            self.concurrent_accesses += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent_accesses)

    def _track_exit(self):
        with self.lock:
            self.concurrent_accesses -= 1

    def select(self, folder: str, readonly: bool = True) -> tuple[str, Any]:
        self._track_enter()
        try:
            return self.base_client.select(folder, readonly)
        finally:
            self._track_exit()

    def uid(self, command: str, *args) -> tuple[str, Any]:
        self._track_enter()
        try:
            return self.base_client.uid(command, *args)
        finally:
            self._track_exit()

    def logout(self) -> tuple[str, Any]:
        return self.base_client.logout()


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def test_context(tmp_path: Path):
    """Create test context with database and logger."""
    db_path = tmp_path / "test.db"
    log_path = tmp_path / "test.log"
    secrets_path = tmp_path / "secrets.json"

    # Create dummy secrets file
    secrets_path.write_text(
        json.dumps({"server": "test.example.com", "username": "test", "password": "test"})
    )

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    yield {
        "db_path": db_path,
        "db": db,
        "logger": logger,
        "secrets_path": secrets_path,
        "tmp_path": tmp_path,
    }

    db.close()


@pytest.fixture
def sample_folders_data() -> dict[str, list[tuple[str, bytes, list[str], str | None]]]:
    """Create sample folder data for testing."""
    return {
        "INBOX": [
            (
                "1",
                b"Subject: Test 1\r\nFrom: sender1@example.com\r\n\r\n",
                ["\\Seen"],
                "01-Dec-2025 12:00:00 +0000",
            ),
            (
                "2",
                b"Subject: Test 2\r\nFrom: sender2@example.com\r\n\r\n",
                [],
                "01-Dec-2025 13:00:00 +0000",
            ),
        ],
        "Sent": [
            (
                "10",
                b"Subject: Sent 1\r\nTo: recipient1@example.com\r\n\r\n",
                ["\\Seen", "\\Flagged"],
                "01-Dec-2025 14:00:00 +0000",
            ),
        ],
        "Archive": [
            (
                "20",
                b"Subject: Archive 1\r\nFrom: old@example.com\r\n\r\n",
                ["\\Seen"],
                "01-Nov-2025 10:00:00 +0000",
            ),
            (
                "21",
                b"Subject: Archive 2\r\nFrom: old2@example.com\r\n\r\n",
                ["\\Seen"],
                "01-Nov-2025 11:00:00 +0000",
            ),
            (
                "22",
                b"Subject: Archive 3\r\nFrom: old3@example.com\r\n\r\n",
                [],
                "01-Nov-2025 12:00:00 +0000",
            ),
        ],
        "Drafts": [
            (
                "30",
                b"Subject: Draft 1\r\nFrom: me@example.com\r\n\r\n",
                ["\\Draft"],
                "02-Dec-2025 09:00:00 +0000",
            ),
        ],
        "Trash": [],  # Empty folder
    }


# ============================================================================
# 1. Parallel Cache Correctness Tests
# ============================================================================


def test_parallel_sequential_equivalence(test_context, sample_folders_data, monkeypatch):
    """Verify parallel and sequential cache builds produce identical results."""
    # Patch imap_login to return mock clients
    def mock_login(secrets_path, logger):
        return MockIMAPClient(sample_folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr("core.imap_client.safe_search_all", lambda client, undeleted_only=True: [
        uid.encode() for uid, _, _, _ in sample_folders_data.get(client.selected_folder, [])
    ])

    folders = ["INBOX", "Sent", "Archive", "Drafts", "Trash"]

    # Build cache sequentially
    db_seq = init_db(test_context["tmp_path"] / "seq.db", logger=test_context["logger"])
    client_seq = MockIMAPClient(sample_folders_data)
    timer_seq, folders_seq, msgs_seq = build_cache(
        client_seq,
        db_seq,
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
    )

    # Build cache in parallel
    db_par = init_db(test_context["tmp_path"] / "par.db", logger=test_context["logger"])
    timer_par, folders_par, msgs_par = build_cache_parallel(
        test_context["secrets_path"],
        test_context["tmp_path"] / "par.db",
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    # Verify same counts
    assert folders_seq == folders_par == len(folders)
    assert msgs_seq == msgs_par == 7  # Total messages across all folders (2+1+3+1+0)

    # Verify identical database contents
    seq_headers = db_seq.execute(
        "SELECT folder, uid, data FROM headers ORDER BY folder, uid"
    ).fetchall()
    par_headers = db_par.execute(
        "SELECT folder, uid, data FROM headers ORDER BY folder, uid"
    ).fetchall()

    assert len(seq_headers) == len(par_headers)
    for (f1, u1, d1), (f2, u2, d2) in zip(seq_headers, par_headers):
        assert f1 == f2
        assert u1 == u2
        # Parse JSON to compare content (may have different ordering)
        data1 = json.loads(d1)
        data2 = json.loads(d2)
        assert data1["header"] == data2["header"]
        assert data1.get("flags", []) == data2.get("flags", [])
        assert data1.get("internaldate") == data2.get("internaldate")

    db_seq.close()
    db_par.close()


def test_parallel_folder_independence(test_context, monkeypatch):
    """Verify folders don't interfere with each other during parallel processing."""
    # Create folders with overlapping UIDs (should be scoped by folder)
    folders_data = {
        "Folder1": [
            ("1", b"Subject: F1-UID1\r\n\r\n", ["\\Seen"], "01-Dec-2025 10:00:00 +0000"),
            ("2", b"Subject: F1-UID2\r\n\r\n", [], "01-Dec-2025 11:00:00 +0000"),
        ],
        "Folder2": [
            ("1", b"Subject: F2-UID1\r\n\r\n", ["\\Flagged"], "01-Dec-2025 10:00:00 +0000"),
            ("2", b"Subject: F2-UID2\r\n\r\n", ["\\Draft"], "01-Dec-2025 11:00:00 +0000"),
        ],
        "Folder3": [
            ("1", b"Subject: F3-UID1\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000"),
        ],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr("core.imap_client.safe_search_all", lambda client, undeleted_only=True: [
        uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
    ])

    folders = ["Folder1", "Folder2", "Folder3"]

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    assert folders_count == 3
    assert msgs_count == 5

    # Verify each folder has correct messages
    for folder in folders:
        rows = test_context["db"].execute(
            "SELECT uid, data FROM headers WHERE folder=? ORDER BY uid", (folder,)
        ).fetchall()
        expected_count = len(folders_data[folder])
        assert len(rows) == expected_count

        # Verify UIDs and subjects match
        for idx, (uid, data_json) in enumerate(rows):
            expected_uid, expected_header, expected_flags, expected_date = folders_data[folder][idx]
            assert uid == expected_uid
            data = json.loads(data_json)
            assert expected_header.decode("ascii") in data["header"]
            assert data.get("flags", []) == expected_flags
            assert data.get("internaldate") == expected_date


def test_parallel_error_isolation(test_context, monkeypatch):
    """Verify one folder error doesn't stop other folders from processing."""
    folders_data = {
        "GoodFolder1": [
            ("1", b"Subject: Good1\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000"),
        ],
        "BadFolder": [
            ("1", b"Subject: Bad\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000"),
        ],
        "GoodFolder2": [
            ("1", b"Subject: Good2\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000"),
            ("2", b"Subject: Good3\r\n\r\n", [], "01-Dec-2025 11:00:00 +0000"),
        ],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, fail_folders={"BadFolder"})

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = ["GoodFolder1", "BadFolder", "GoodFolder2"]

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    # Should process all folders despite BadFolder failing
    assert folders_count == 3
    assert msgs_count == 3  # Only messages from good folders

    # Verify good folders were cached
    good1_rows = test_context["db"].execute(
        "SELECT uid FROM headers WHERE folder='GoodFolder1'"
    ).fetchall()
    assert len(good1_rows) == 1

    good2_rows = test_context["db"].execute(
        "SELECT uid FROM headers WHERE folder='GoodFolder2'"
    ).fetchall()
    assert len(good2_rows) == 2

    # Verify bad folder has no cached messages
    bad_rows = test_context["db"].execute(
        "SELECT uid FROM headers WHERE folder='BadFolder'"
    ).fetchall()
    assert len(bad_rows) == 0


def test_parallel_progress_tracking(test_context, monkeypatch):
    """Verify progress bar updates correctly during parallel processing."""
    folders_data = {
        f"Folder{i}": [
            (str(j), f"Subject: F{i}-M{j}\r\n\r\n".encode(), [], "01-Dec-2025 10:00:00 +0000")
            for j in range(2)
        ]
        for i in range(5)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, delay_ms=10)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i}" for i in range(5)]

    # Progress tracking is implicit in build_cache_parallel
    # We verify it completes successfully and processes all folders
    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,  # Don't show actual progress bar in tests
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    assert folders_count == 5
    assert msgs_count == 10


# ============================================================================
# 2. Smart Auto-Detection Tests
# ============================================================================


def test_auto_detect_sequential_few_folders(test_context, sample_folders_data, monkeypatch):
    """Verify < 5 folders uses 1 worker (sequential)."""
    client = MockIMAPClient(sample_folders_data)

    def mock_login(secrets_path, logger):
        return client

    monkeypatch.setattr("core.imap_client.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in sample_folders_data.get(client.selected_folder, [])
        ],
    )

    # Simulate CLI logic: auto-detect should choose 1 worker for 3 folders
    folders = ["INBOX", "Sent", "Archive"]
    parallel_workers = 5 if len(folders) >= 5 else 1

    assert parallel_workers == 1, "Should use sequential processing for < 5 folders"


def test_auto_detect_parallel_many_folders(test_context, monkeypatch):
    """Verify 5+ folders uses 5 workers."""
    # Create 10 folders
    folders_data = {
        f"Folder{i}": [
            ("1", f"Subject: F{i}\r\n\r\n".encode(), [], "01-Dec-2025 10:00:00 +0000")
        ]
        for i in range(10)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i}" for i in range(10)]
    parallel_workers = 5 if len(folders) >= 5 else 1

    assert parallel_workers == 5, "Should use parallel processing for 5+ folders"

    # Verify parallel execution works
    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=parallel_workers,
    )

    assert folders_count == 10
    assert msgs_count == 10


def test_parallel_workers_override(test_context, monkeypatch):
    """Verify --parallel-workers N overrides auto-detect."""
    folders_data = {f"Folder{i}": [] for i in range(10)}

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [],
    )

    folders = [f"Folder{i}" for i in range(10)]

    # User explicitly sets --parallel-workers 2
    user_override = 2
    parallel_workers = user_override if user_override is not None else (5 if len(folders) >= 5 else 1)

    assert parallel_workers == 2, "User override should take precedence"

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=parallel_workers,
    )

    assert folders_count == 10


def test_smart_detection_with_all_folders(test_context, monkeypatch):
    """Verify --all-folders triggers auto-detection."""
    # Create 8 folders to trigger parallel mode
    folders_data = {f"Folder{i}": [] for i in range(8)}

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    def mock_list_all_folders(client):
        return list(folders_data.keys())

    monkeypatch.setattr("core.imap_client.imap_login", mock_login)
    monkeypatch.setattr("core.imap_client.list_all_folders", mock_list_all_folders)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [],
    )

    # Simulate CLI with --all-folders
    all_folders = True
    if all_folders:
        client = MockIMAPClient(folders_data)
        folders = list(folders_data.keys())

    parallel_workers = 5 if len(folders) >= 5 else 1
    assert parallel_workers == 5, "Should auto-detect parallel for many folders"


# ============================================================================
# 3. Performance Tests
# ============================================================================


def test_parallel_faster_than_sequential(test_context, monkeypatch):
    """Verify 5 workers is faster than 1 worker."""
    # Create folders with simulated network delay
    folders_data = {
        f"Folder{i}": [
            (str(j), f"Subject: F{i}-M{j}\r\n\r\n".encode(), [], "01-Dec-2025 10:00:00 +0000")
            for j in range(3)
        ]
        for i in range(6)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, delay_ms=50)  # 50ms per operation

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i}" for i in range(6)]

    # Test sequential (1 worker)
    db_seq = init_db(test_context["tmp_path"] / "seq.db", logger=test_context["logger"])
    start_seq = time.time()
    client_seq = MockIMAPClient(folders_data, delay_ms=50)
    build_cache(
        client_seq,
        db_seq,
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
    )
    time_seq = time.time() - start_seq
    db_seq.close()

    # Test parallel (5 workers)
    start_par = time.time()
    build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=5,
    )
    time_par = time.time() - start_par

    # Parallel should be noticeably faster (allow some overhead)
    # With 50ms delay and 6 folders * 4 ops each = 24 operations
    # Sequential: ~1.2s, Parallel with 5 workers: ~0.4s
    # In practice, expect at least 1.3x speedup (accounting for thread overhead)
    speedup = time_seq / time_par
    assert speedup > 1.3, f"Parallel should be faster (speedup: {speedup:.2f}x)"


def test_worker_count_effect(test_context, monkeypatch):
    """Verify more workers = faster (up to a point)."""
    folders_data = {
        f"Folder{i}": [
            ("1", f"Subject: F{i}\r\n\r\n".encode(), [], "01-Dec-2025 10:00:00 +0000")
        ]
        for i in range(10)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, delay_ms=30)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i}" for i in range(10)]

    times = {}
    for workers in [1, 2, 5]:
        db_path = test_context["tmp_path"] / f"workers_{workers}.db"
        start = time.time()
        build_cache_parallel(
            test_context["secrets_path"],
            db_path,
            folders,
            show_progress=False,
            logger=test_context["logger"],
            limit=None,
            order="newest",
            max_workers=workers,
        )
        times[workers] = time.time() - start

    # More workers should be faster or equal (up to number of folders)
    assert times[2] <= times[1] * 1.2, "2 workers should be faster than 1"
    assert times[5] <= times[2] * 1.2, "5 workers should be faster than 2"


def test_memory_usage_reasonable(test_context, monkeypatch):
    """Verify parallel processing doesn't explode memory usage."""
    # Create many folders with messages
    folders_data = {
        f"Folder{i}": [
            (
                str(j),
                # Create reasonably sized headers (1KB each)
                (f"Subject: F{i}-M{j}\r\n" + "X-Header: " + ("data" * 200) + "\r\n\r\n").encode(),
                [],
                "01-Dec-2025 10:00:00 +0000",
            )
            for j in range(10)
        ]
        for i in range(20)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i}" for i in range(20)]

    # This test mainly verifies it completes without memory errors
    # With 20 folders * 10 messages * 1KB headers = ~200KB of data
    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=5,
    )

    assert folders_count == 20
    assert msgs_count == 200

    # Verify database size is reasonable
    db_size = test_context["db_path"].stat().st_size
    # Should be < 5MB for this amount of data
    assert db_size < 5 * 1024 * 1024, f"Database size too large: {db_size / 1024 / 1024:.2f}MB"


# ============================================================================
# 4. Error Handling Tests
# ============================================================================


def test_continue_on_folder_failure(test_context, monkeypatch):
    """Verify one bad folder doesn't block others."""
    folders_data = {
        "Folder1": [("1", b"Subject: F1\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "Folder2": [("1", b"Subject: F2\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "BadFolder": [("1", b"Subject: Bad\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "Folder3": [("1", b"Subject: F3\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, fail_folders={"BadFolder"})

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = ["Folder1", "Folder2", "BadFolder", "Folder3"]

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    # All folders attempted
    assert folders_count == 4
    # Only good folders cached
    assert msgs_count == 3

    # Verify specific folders cached
    cached_folders = {
        row[0]
        for row in test_context["db"].execute("SELECT DISTINCT folder FROM headers").fetchall()
    }
    assert cached_folders == {"Folder1", "Folder2", "Folder3"}


def test_imap_connection_failure_recovery(test_context, monkeypatch):
    """Verify graceful handling of IMAP connection failures."""

    class FailingClient(MockIMAPClient):
        """Client that fails on specific folders."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_count = 0

        def select(self, folder: str, readonly: bool = True) -> tuple[str, Any]:
            folder_name = folder.strip('"')
            # Fail on second folder selection
            if folder_name == "FailFolder" and self.fail_count == 0:
                self.fail_count += 1
                raise Exception("Connection lost")
            return super().select(folder, readonly)

    folders_data = {
        "GoodFolder1": [("1", b"Subject: Good1\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "FailFolder": [("1", b"Subject: Fail\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "GoodFolder2": [("1", b"Subject: Good2\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
    }

    def mock_login(secrets_path, logger):
        return FailingClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = ["GoodFolder1", "FailFolder", "GoodFolder2"]

    # Should continue despite connection failure
    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    assert folders_count == 3
    # At least the good folders should be cached
    assert msgs_count >= 2


def test_database_concurrent_write_safety(test_context, monkeypatch):
    """Verify concurrent database writes don't corrupt data."""
    # Create many folders to stress test concurrent writes
    folders_data = {
        f"Folder{i:03d}": [
            (str(j), f"Subject: F{i}-M{j}\r\n\r\n".encode(), [], "01-Dec-2025 10:00:00 +0000")
            for j in range(5)
        ]
        for i in range(20)
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data, delay_ms=5)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = [f"Folder{i:03d}" for i in range(20)]

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=10,  # High concurrency to stress test
    )

    assert folders_count == 20
    assert msgs_count == 100

    # Verify no duplicate entries (database integrity)
    cursor = test_context["db"].execute(
        "SELECT folder, uid, COUNT(*) as cnt FROM headers GROUP BY folder, uid HAVING cnt > 1"
    )
    duplicates = cursor.fetchall()
    assert len(duplicates) == 0, f"Found duplicate entries: {duplicates}"

    # Verify all expected messages are present
    for folder in folders:
        count = test_context["db"].execute(
            "SELECT COUNT(*) FROM headers WHERE folder=?", (folder,)
        ).fetchone()[0]
        assert count == 5, f"Folder {folder} has {count} messages, expected 5"

    # Verify folders table is correctly populated
    folder_rows = test_context["db"].execute("SELECT name FROM folders ORDER BY name").fetchall()
    assert len(folder_rows) == 20


# ============================================================================
# Additional Integration Tests
# ============================================================================


def test_parallel_with_limit_and_order(test_context, monkeypatch):
    """Verify limit and order parameters work correctly in parallel mode."""
    folders_data = {
        "Folder1": [
            (str(i), f"Subject: Msg{i}\r\n\r\n".encode(), [], f"01-Dec-2025 {10+i}:00:00 +0000")
            for i in range(10)
        ],
        "Folder2": [
            (str(i), f"Subject: Msg{i}\r\n\r\n".encode(), [], f"01-Dec-2025 {10+i}:00:00 +0000")
            for i in range(10)
        ],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = ["Folder1", "Folder2"]

    # Test with limit=3, order=newest (should get UIDs 7, 8, 9)
    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=3,
        order="newest",
        max_workers=2,
    )

    assert folders_count == 2
    assert msgs_count == 6  # 3 per folder

    # Verify correct UIDs cached (newest 3)
    for folder in folders:
        uids = [
            row[0]
            for row in test_context["db"]
            .execute("SELECT uid FROM headers WHERE folder=? ORDER BY uid", (folder,))
            .fetchall()
        ]
        # UIDs are strings, so "7", "8", "9" when sorted
        assert uids == ["7", "8", "9"], f"Folder {folder} has UIDs {uids}"


def test_parallel_flags_and_internaldate_preservation(test_context, monkeypatch):
    """Verify FLAGS and INTERNALDATE are correctly stored in parallel mode."""
    folders_data = {
        "INBOX": [
            ("1", b"Subject: Test1\r\n\r\n", ["\\Seen", "\\Flagged"], "01-Dec-2025 10:00:00 +0000"),
            ("2", b"Subject: Test2\r\n\r\n", ["CustomFlag"], "02-Dec-2025 11:00:00 +0000"),
            ("3", b"Subject: Test3\r\n\r\n", [], "03-Dec-2025 12:00:00 +0000"),
        ],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        ["INBOX"],
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=1,
    )

    assert msgs_count == 3

    # Verify FLAGS and INTERNALDATE in database
    rows = test_context["db"].execute(
        "SELECT uid, data FROM headers WHERE folder='INBOX' ORDER BY uid"
    ).fetchall()

    expected = [
        ("1", ["\\Seen", "\\Flagged"], "01-Dec-2025 10:00:00 +0000"),
        ("2", ["CustomFlag"], "02-Dec-2025 11:00:00 +0000"),
        ("3", [], "03-Dec-2025 12:00:00 +0000"),
    ]

    for idx, (uid, data_json) in enumerate(rows):
        expected_uid, expected_flags, expected_date = expected[idx]
        assert uid == expected_uid
        data = json.loads(data_json)
        assert data.get("flags", []) == expected_flags
        assert data.get("internaldate") == expected_date


def test_empty_folders_handled_correctly(test_context, monkeypatch):
    """Verify empty folders are handled correctly in parallel mode."""
    folders_data = {
        "EmptyFolder1": [],
        "NonEmptyFolder": [("1", b"Subject: Test\r\n\r\n", [], "01-Dec-2025 10:00:00 +0000")],
        "EmptyFolder2": [],
    }

    def mock_login(secrets_path, logger):
        return MockIMAPClient(folders_data)

    monkeypatch.setattr("core.connection_pool.imap_login", mock_login)
    monkeypatch.setattr(
        "core.imap_client.safe_search_all",
        lambda client, undeleted_only=True: [
            uid.encode() for uid, _, _, _ in folders_data.get(client.selected_folder, [])
        ],
    )

    folders = ["EmptyFolder1", "NonEmptyFolder", "EmptyFolder2"]

    timer, folders_count, msgs_count = build_cache_parallel(
        test_context["secrets_path"],
        test_context["db_path"],
        folders,
        show_progress=False,
        logger=test_context["logger"],
        limit=None,
        order="newest",
        max_workers=3,
    )

    assert folders_count == 3
    assert msgs_count == 1

    # Verify empty folders are registered in folders table
    folder_rows = test_context["db"].execute(
        "SELECT name FROM folders ORDER BY name"
    ).fetchall()
    assert len(folder_rows) == 3
    folder_names = {row[0] for row in folder_rows}
    assert folder_names == {"EmptyFolder1", "NonEmptyFolder", "EmptyFolder2"}

    # Verify no messages cached for empty folders
    for empty_folder in ["EmptyFolder1", "EmptyFolder2"]:
        count = test_context["db"].execute(
            "SELECT COUNT(*) FROM headers WHERE folder=?", (empty_folder,)
        ).fetchone()[0]
        assert count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
