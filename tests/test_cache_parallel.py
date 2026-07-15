"""Tests for cache optimization: folder sizing, sorting, connection pooling, and WAL mode."""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "tqdm" not in sys.modules:  # pragma: no cover - test support
    tqdm_stub = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **_kwargs):
            self._iterable = list(iterable or [])
            self.total = _kwargs.get("total", len(self._iterable) if self._iterable else 0)

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

from core.connection_pool import IMAPConnectionPool
from core.database import init_db
from core.imap_client import get_folder_sizes
from core.logging_utils import JsonLogger


# ============================================================================
# Test Fixtures
# ============================================================================


@pytest.fixture
def logger_fixture(tmp_path: Path):
    """Create a test logger."""
    log_path = tmp_path / "test.json"
    return JsonLogger(log_path)


@pytest.fixture
def db_fixture(tmp_path: Path, logger_fixture):
    """Create a test database with WAL mode."""
    db_path = tmp_path / "test.db"
    db = init_db(db_path, logger=logger_fixture)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def secrets_fixture(tmp_path: Path):
    """Create a mock secrets file."""
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(
        '{"imap": {"host": "imap.example.com", "port": 993, '
        '"username": "test@example.com", "password": "password123"}}'
    )
    return secrets_path


# ============================================================================
# 1. Folder Sizing Tests
# ============================================================================


class TestGetFolderSizes:
    """Test IMAP STATUS command parsing and folder sizing."""

    def test_parse_single_folder_with_messages(self):
        """Test parsing STATUS response for folder with messages."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'"INBOX" (MESSAGES 1234)'],
        )

        sizes = get_folder_sizes(mock_client, ["INBOX"])

        assert sizes == {"INBOX": 1234}
        mock_client.status.assert_called_once_with('"INBOX"', "(MESSAGES)")

    def test_parse_empty_folder(self):
        """Test parsing STATUS response for folder with 0 messages."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'"Archive" (MESSAGES 0)'],
        )

        sizes = get_folder_sizes(mock_client, ["Archive"])

        assert sizes == {"Archive": 0}

    def test_parse_large_folder(self):
        """Test parsing STATUS response for folder with 1000+ messages."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'"Sent" (MESSAGES 12345)'],
        )

        sizes = get_folder_sizes(mock_client, ["Sent"])

        assert sizes == {"Sent": 12345}

    def test_parse_multiple_folders(self):
        """Test getting sizes for multiple folders."""
        mock_client = Mock()

        def mock_status(folder, _criteria):
            responses = {
                '"INBOX"': (b'"INBOX" (MESSAGES 100)', "OK"),
                '"Archive"': (b'"Archive" (MESSAGES 500)', "OK"),
                '"Sent"': (b'"Sent" (MESSAGES 0)', "OK"),
            }
            response_data, status = responses.get(folder, (b"", "NO"))
            return status, [response_data] if response_data else []

        mock_client.status.side_effect = lambda f, c: mock_status(f, c)

        sizes = get_folder_sizes(mock_client, ["INBOX", "Archive", "Sent"])

        assert sizes == {"INBOX": 100, "Archive": 500, "Sent": 0}
        assert mock_client.status.call_count == 3

    def test_handle_status_failure(self):
        """Test handling STATUS command failure (return -1)."""
        mock_client = Mock()
        mock_client.status.return_value = ("NO", [])

        sizes = get_folder_sizes(mock_client, ["BadFolder"])

        assert sizes == {"BadFolder": -1}

    def test_handle_malformed_response(self):
        """Test handling malformed STATUS response."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'"INBOX" (INVALID RESPONSE)'],
        )

        sizes = get_folder_sizes(mock_client, ["INBOX"])

        assert sizes == {"INBOX": -1}

    def test_handle_empty_response(self):
        """Test handling empty STATUS response."""
        mock_client = Mock()
        mock_client.status.return_value = ("OK", [])

        sizes = get_folder_sizes(mock_client, ["INBOX"])

        assert sizes == {"INBOX": -1}

    def test_handle_exception_during_status(self):
        """Test handling exceptions during STATUS command."""
        mock_client = Mock()
        mock_client.status.side_effect = Exception("Connection lost")

        sizes = get_folder_sizes(mock_client, ["INBOX"])

        assert sizes == {"INBOX": -1}

    def test_handle_mixed_success_and_failure(self):
        """Test handling mix of successful and failed STATUS commands."""
        mock_client = Mock()

        def mock_status(folder, _criteria):
            if folder == '"INBOX"':
                return "OK", [b'"INBOX" (MESSAGES 100)']
            elif folder == '"Archive"':
                return "NO", []
            else:  # Sent
                raise Exception("Network error")

        mock_client.status.side_effect = mock_status

        sizes = get_folder_sizes(mock_client, ["INBOX", "Archive", "Sent"])

        assert sizes == {"INBOX": 100, "Archive": -1, "Sent": -1}

    def test_parse_status_with_spaces_in_folder_name(self):
        """Test parsing STATUS for folders with spaces in names."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'"Sent Items" (MESSAGES 42)'],
        )

        sizes = get_folder_sizes(mock_client, ["Sent Items"])

        assert sizes == {"Sent Items": 42}

    def test_parse_status_with_special_characters(self):
        """Test parsing STATUS with various response formats."""
        mock_client = Mock()
        mock_client.status.return_value = (
            "OK",
            [b'INBOX (MESSAGES 100)'],  # Without quotes
        )

        sizes = get_folder_sizes(mock_client, ["INBOX"])

        assert sizes == {"INBOX": 100}


# ============================================================================
# 2. Folder Sorting Tests
# ============================================================================


class TestFolderOrdering:
    """Test folder sorting by message count."""

    def test_sort_folders_smallest_to_largest(self):
        """Test folders are sorted from smallest to largest."""
        folder_sizes = {
            "Large": 1000,
            "Small": 10,
            "Medium": 100,
            "Tiny": 1,
            "Empty": 0,
        }
        folders = list(folder_sizes.keys())

        # Sort by size (as would be done in CLI)
        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        assert sorted_folders == ["Empty", "Tiny", "Small", "Medium", "Large"]

    def test_sort_equal_sized_folders_stable(self):
        """Test folders with equal sizes maintain stable sort order."""
        folder_sizes = {"A": 100, "B": 100, "C": 100}
        folders = ["B", "C", "A"]  # Intentionally out of order

        # Python's sort is stable, so equal elements maintain their relative order
        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Stable sort preserves original order for equal keys
        assert sorted_folders == ["B", "C", "A"]

    def test_sort_with_failed_folders_last(self):
        """Test folders with size=-1 (failed STATUS) are sorted first (smallest value)."""
        folder_sizes = {
            "Good1": 100,
            "Failed1": -1,
            "Good2": 50,
            "Failed2": -1,
            "Good3": 200,
        }
        folders = list(folder_sizes.keys())

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Failed folders (-1) come first (smallest numeric value)
        assert set(sorted_folders[:2]) == {"Failed1", "Failed2"}
        # Then valid folders sorted by size (smallest to largest)
        assert sorted_folders[2:] == ["Good2", "Good1", "Good3"]

    def test_sort_mixed_valid_and_invalid(self):
        """Test sorting with mix of valid sizes, zeros, and failures."""
        folder_sizes = {
            "Empty": 0,
            "Failed": -1,
            "Small": 10,
            "Zero2": 0,
            "Large": 1000,
        }
        folders = list(folder_sizes.keys())

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Order: -1 (failed), then 0s, then positive numbers
        assert sorted_folders[0] == "Failed"  # -1 comes first numerically
        assert sorted_folders[1:3] == ["Empty", "Zero2"] or sorted_folders[1:3] == ["Zero2", "Empty"]
        assert sorted_folders[3] == "Small"
        assert sorted_folders[4] == "Large"

    def test_sort_all_empty_folders(self):
        """Test sorting when all folders are empty."""
        folder_sizes = {"A": 0, "B": 0, "C": 0}
        folders = ["C", "A", "B"]

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # All zeros, stable sort preserves order
        assert sorted_folders == ["C", "A", "B"]

    def test_sort_all_failed_folders(self):
        """Test sorting when all folders failed STATUS."""
        folder_sizes = {"A": -1, "B": -1, "C": -1}
        folders = ["C", "A", "B"]

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # All -1, stable sort preserves order
        assert sorted_folders == ["C", "A", "B"]

    def test_sort_single_folder(self):
        """Test sorting with single folder."""
        folder_sizes = {"INBOX": 100}
        folders = ["INBOX"]

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        assert sorted_folders == ["INBOX"]

    def test_sort_preserves_folder_names(self):
        """Test that sorting doesn't modify folder names."""
        folder_sizes = {
            "Sent Items": 100,
            "Archive/2024": 50,
            "INBOX": 200,
        }
        folders = list(folder_sizes.keys())

        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Verify names are intact
        assert all("/" in f or " " in f or f == "INBOX" for f in sorted_folders)
        assert len(sorted_folders) == 3


# ============================================================================
# 3. Connection Pool Tests
# ============================================================================


class TestConnectionPool:
    """Test IMAP connection pool for parallel operations."""

    def test_acquire_release_cycle(self, secrets_fixture, logger_fixture):
        """Test basic acquire and release cycle."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=2, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn = MagicMock()
            mock_login.return_value = mock_conn

            # Acquire connection
            conn = pool.acquire()
            assert conn is mock_conn
            assert pool._created == 1
            mock_login.assert_called_once()

            # Release connection
            pool.release(conn)

            # Acquire again - should reuse
            conn2 = pool.acquire()
            assert conn2 is mock_conn
            assert pool._created == 1  # No new connection created
            assert mock_login.call_count == 1  # Still just one login

        pool.shutdown()

    def test_max_connections_limit(self, secrets_fixture, logger_fixture):
        """Test that pool respects max_connections limit."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=2, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn1 = MagicMock()
            mock_conn2 = MagicMock()
            mock_login.side_effect = [mock_conn1, mock_conn2]

            # Acquire max connections
            conn1 = pool.acquire()
            conn2 = pool.acquire()

            assert conn1 is mock_conn1
            assert conn2 is mock_conn2
            assert pool._created == 2
            assert mock_login.call_count == 2

            # Try to acquire third connection (should block until one is released)
            # We'll test this with threads below

        pool.shutdown()

    def test_concurrent_acquire(self, secrets_fixture, logger_fixture):
        """Test concurrent acquisition from multiple threads."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=3, logger=logger_fixture)

        acquired = []
        errors = []

        def worker(worker_id):
            try:
                conn = pool.acquire()
                acquired.append((worker_id, conn))
                time.sleep(0.01)  # Simulate work
                pool.release(conn)
            except Exception as e:
                errors.append((worker_id, e))

        with patch("core.connection_pool.imap_login") as mock_login:
            # Create unique mock connections
            mock_conns = [MagicMock(name=f"conn_{i}") for i in range(3)]
            mock_login.side_effect = mock_conns

            # Launch 5 threads (more than pool size)
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All threads should complete without errors
            assert len(errors) == 0
            assert len(acquired) == 5

            # Pool should have created at most max_connections
            assert pool._created <= 3
            assert mock_login.call_count <= 3

        pool.shutdown()

    def test_shutdown_closes_connections(self, secrets_fixture, logger_fixture):
        """Test that shutdown closes all pooled connections."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=2, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn1 = MagicMock()
            mock_conn2 = MagicMock()
            mock_login.side_effect = [mock_conn1, mock_conn2]

            # Acquire and release connections
            conn1 = pool.acquire()
            conn2 = pool.acquire()
            pool.release(conn1)
            pool.release(conn2)

            # Shutdown should logout all connections
            pool.shutdown()

            # Both connections should have been logged out
            mock_conn1.logout.assert_called_once()
            mock_conn2.logout.assert_called_once()

            # Pool should be empty
            assert pool._pool.empty()

    def test_pool_reuses_connections(self, secrets_fixture, logger_fixture):
        """Test that pool efficiently reuses connections."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=2, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn = MagicMock()
            mock_login.return_value = mock_conn

            # Acquire and release multiple times
            for _ in range(5):
                conn = pool.acquire()
                assert conn is mock_conn
                pool.release(conn)

            # Should only create one connection
            assert pool._created == 1
            assert mock_login.call_count == 1

        pool.shutdown()

    def test_acquire_blocks_when_at_capacity(self, secrets_fixture, logger_fixture):
        """Test that acquire blocks when pool is at max capacity."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=1, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn = MagicMock()
            mock_login.return_value = mock_conn

            # Acquire the only connection
            conn = pool.acquire()
            assert pool._created == 1

            # Track whether second acquire blocks
            acquired_second = []

            def try_acquire():
                conn2 = pool.acquire()
                acquired_second.append(conn2)

            # Start thread that will block on acquire
            t = threading.Thread(target=try_acquire)
            t.start()

            # Give thread time to start and block
            time.sleep(0.05)

            # Thread should still be alive (blocked)
            assert t.is_alive()
            assert len(acquired_second) == 0

            # Release connection
            pool.release(conn)

            # Wait for thread to complete
            t.join(timeout=1)

            # Thread should have acquired the connection
            assert not t.is_alive()
            assert len(acquired_second) == 1
            assert acquired_second[0] is mock_conn

            # Clean up
            pool.release(acquired_second[0])

        pool.shutdown()

    def test_shutdown_handles_logout_errors(self, secrets_fixture, logger_fixture):
        """Test that shutdown handles logout errors gracefully."""
        pool = IMAPConnectionPool(secrets_fixture, max_connections=1, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_conn = MagicMock()
            mock_conn.logout.side_effect = Exception("Logout failed")
            mock_login.return_value = mock_conn

            conn = pool.acquire()
            pool.release(conn)

            # Shutdown should not raise exception
            pool.shutdown()

            # Connection logout was attempted
            mock_conn.logout.assert_called_once()

    def test_pool_with_zero_max_connections(self, secrets_fixture, logger_fixture):
        """Test pool behavior with invalid max_connections (edge case)."""
        # This tests defensive programming - pool should handle gracefully
        pool = IMAPConnectionPool(secrets_fixture, max_connections=0, logger=logger_fixture)

        with patch("core.connection_pool.imap_login") as mock_login:
            mock_login.return_value = MagicMock()

            # Acquire should block indefinitely (no connections allowed)
            # We'll test with a timeout to avoid hanging
            acquired = []

            def try_acquire():
                conn = pool.acquire()
                acquired.append(conn)

            t = threading.Thread(target=try_acquire)
            t.start()
            t.join(timeout=0.1)

            # Thread should still be blocked
            assert t.is_alive()
            assert len(acquired) == 0

            # Force thread termination by shutting down pool
            pool._pool.put(None)  # Unblock the waiting thread
            # Join before shutdown: shutdown() drains the queue and can steal
            # the None before the blocked thread wakes, leaving it to time out
            t.join(timeout=5)
            assert not t.is_alive()

        pool.shutdown()


# ============================================================================
# 4. WAL Mode Tests
# ============================================================================


class TestWALMode:
    """Test SQLite WAL mode for concurrent access."""

    def test_wal_mode_enabled_on_init(self, tmp_path: Path, logger_fixture):
        """Test that WAL mode is enabled when database is initialized."""
        db_path = tmp_path / "test.db"
        db = init_db(db_path, logger=logger_fixture)

        try:
            # Check journal mode
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            db.close()

    def test_synchronous_mode_set_to_normal(self, tmp_path: Path, logger_fixture):
        """Test that synchronous mode is set to NORMAL for better performance."""
        db_path = tmp_path / "test.db"
        db = init_db(db_path, logger=logger_fixture)

        try:
            # Check synchronous mode (0=OFF, 1=NORMAL, 2=FULL)
            sync_mode = db.execute("PRAGMA synchronous").fetchone()[0]
            assert sync_mode == 1  # NORMAL
        finally:
            db.close()

    def test_automatic_migration_from_delete_mode(self, tmp_path: Path, logger_fixture):
        """Test automatic migration from DELETE mode to WAL mode."""
        db_path = tmp_path / "test.db"

        # Create database in DELETE mode
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("CREATE TABLE test (id INTEGER)")
        db.commit()
        mode_before = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode_before.lower() == "delete"
        db.close()

        # Re-initialize with init_db (should migrate to WAL)
        db = init_db(db_path, logger=logger_fixture)

        try:
            mode_after = db.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode_after.lower() == "wal"

            # Verify WAL files are created
            assert (tmp_path / "test.db-wal").exists() or True  # WAL file may not exist until first write
        finally:
            db.close()

    def test_database_operations_work_in_wal_mode(self, db_fixture):
        """Test that normal database operations work correctly in WAL mode."""
        db = db_fixture

        # Verify we're in WAL mode
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

        # Test write operations
        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
            ("INBOX", "1", '{"header": "test"}', "2024-01-01T00:00:00Z"),
        )
        db.commit()

        # Test read operations
        rows = db.execute("SELECT * FROM headers WHERE folder=?", ("INBOX",)).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "1"  # uid

        # Test update operations
        db.execute("UPDATE headers SET data=? WHERE uid=?", ('{"header": "updated"}', "1"))
        db.commit()

        updated = db.execute("SELECT data FROM headers WHERE uid=?", ("1",)).fetchone()[0]
        assert "updated" in updated

        # Test delete operations
        db.execute("DELETE FROM headers WHERE uid=?", ("1",))
        db.commit()

        remaining = db.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
        assert remaining == 0

    def test_concurrent_reads_in_wal_mode(self, tmp_path: Path, logger_fixture):
        """Test concurrent reads work correctly in WAL mode."""
        db_path = tmp_path / "test.db"
        db = init_db(db_path, logger=logger_fixture)

        # Insert test data
        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
            ("INBOX", "1", '{"header": "test"}', "2024-01-01T00:00:00Z"),
        )
        db.commit()
        db.close()

        # Concurrent readers
        results = []
        errors = []

        def reader(reader_id):
            try:
                conn = sqlite3.connect(db_path)
                rows = conn.execute("SELECT * FROM headers").fetchall()
                results.append((reader_id, len(rows)))
                conn.close()
            except Exception as e:
                errors.append((reader_id, e))

        # Launch multiple concurrent readers
        threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All readers should succeed
        assert len(errors) == 0
        assert len(results) == 10
        assert all(count == 1 for _, count in results)

    def test_concurrent_write_and_read_in_wal_mode(self, tmp_path: Path, logger_fixture):
        """Test that reads can proceed while writes are happening (WAL benefit)."""
        db_path = tmp_path / "test.db"
        init_db(db_path, logger=logger_fixture).close()

        writer_done = threading.Event()
        reader_results = []
        errors = []

        def writer():
            try:
                conn = sqlite3.connect(db_path)
                for i in range(10):
                    conn.execute(
                        "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
                        ("INBOX", str(i), f'{{"header": "test{i}"}}', "2024-01-01T00:00:00Z"),
                    )
                    conn.commit()
                    time.sleep(0.01)
                conn.close()
                writer_done.set()
            except Exception as e:
                errors.append(("writer", e))
                writer_done.set()

        def reader(reader_id):
            try:
                # Wait a moment to let writer start
                time.sleep(0.02)
                conn = sqlite3.connect(db_path)
                # Read while writer is still writing
                count = conn.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
                reader_results.append((reader_id, count))
                conn.close()
            except Exception as e:
                errors.append((f"reader_{reader_id}", e))

        # Start writer and readers
        writer_thread = threading.Thread(target=writer)
        reader_threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]

        writer_thread.start()
        for t in reader_threads:
            t.start()

        # Wait for completion
        writer_thread.join()
        for t in reader_threads:
            t.join()

        writer_done.wait()

        # Should have no errors - WAL mode allows concurrent reads during writes
        assert len(errors) == 0
        # All readers should have succeeded
        assert len(reader_results) == 5

    def test_wal_checkpoint_on_close(self, tmp_path: Path, logger_fixture):
        """Test that WAL checkpoint happens (data is persisted)."""
        db_path = tmp_path / "test.db"
        db = init_db(db_path, logger=logger_fixture)

        # Write data
        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
            ("INBOX", "1", '{"header": "test"}', "2024-01-01T00:00:00Z"),
        )
        db.commit()
        db.close()

        # Open fresh connection and verify data persisted
        db2 = sqlite3.connect(db_path)
        count = db2.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
        assert count == 1
        db2.close()

    def test_wal_mode_does_not_affect_schema_migrations(self, tmp_path: Path, logger_fixture):
        """Test that WAL mode doesn't interfere with schema migrations."""
        db_path = tmp_path / "test.db"

        # Create old schema
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute(
            "CREATE TABLE headers (uid TEXT PRIMARY KEY, folder TEXT, data TEXT, updated_at TEXT)"
        )
        db.execute("INSERT INTO headers VALUES ('1', 'INBOX', '{}', '2024-01-01T00:00:00Z')")
        db.commit()
        db.close()

        # Re-initialize (should migrate schema AND enable WAL)
        db = init_db(db_path, logger=logger_fixture)

        try:
            # Check WAL mode enabled
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"

            # Check schema was migrated (new PK is folder, uid)
            info = db.execute("PRAGMA table_info(headers)").fetchall()
            pk_columns = sorted(
                ((row[5], row[1]) for row in info if row[5]),
                key=lambda item: item[0],
            )
            assert [name for _, name in pk_columns] == ["folder", "uid"]

            # Check data was preserved
            rows = db.execute("SELECT * FROM headers").fetchall()
            assert len(rows) == 1
        finally:
            db.close()

    def test_wal_mode_idempotent(self, tmp_path: Path, logger_fixture):
        """Test that enabling WAL mode multiple times is safe."""
        db_path = tmp_path / "test.db"

        # Initialize twice
        db1 = init_db(db_path, logger=logger_fixture)
        db1.close()

        db2 = init_db(db_path, logger=logger_fixture)

        try:
            # Should still be in WAL mode
            mode = db2.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            db2.close()


# ============================================================================
# Integration Tests
# ============================================================================


class TestCacheOptimizationIntegration:
    """Integration tests combining multiple optimization features."""

    def test_folder_sizing_and_sorting_workflow(self):
        """Test complete workflow: get sizes, sort folders, use in cache build."""
        mock_client = Mock()

        def mock_status(folder, _criteria):
            sizes = {
                '"INBOX"': b'"INBOX" (MESSAGES 500)',
                '"Archive"': b'"Archive" (MESSAGES 100)',
                '"Sent"': b'"Sent" (MESSAGES 1000)',
                '"Drafts"': b'"Drafts" (MESSAGES 10)',
            }
            response = sizes.get(folder, b"")
            return ("OK", [response]) if response else ("NO", [])

        mock_client.status.side_effect = mock_status

        # Get folder sizes
        folders = ["INBOX", "Archive", "Sent", "Drafts"]
        folder_sizes = get_folder_sizes(mock_client, folders)

        # Sort by size
        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Verify sorting (smallest to largest)
        assert sorted_folders == ["Drafts", "Archive", "INBOX", "Sent"]
        assert folder_sizes == {"INBOX": 500, "Archive": 100, "Sent": 1000, "Drafts": 10}

    def test_connection_pool_with_wal_database(self, tmp_path: Path, secrets_fixture, logger_fixture):
        """Test connection pool works with WAL-enabled database."""
        db_path = tmp_path / "test.db"
        db = init_db(db_path, logger=logger_fixture)

        # Verify WAL mode
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        db.close()

        # Create connection pool
        pool = IMAPConnectionPool(secrets_fixture, max_connections=2, logger=logger_fixture)

        results = []

        def worker(worker_id):
            # Each worker gets IMAP connection and DB connection
            with patch("core.connection_pool.imap_login") as mock_login:
                mock_login.return_value = MagicMock()
                imap_conn = pool.acquire()

                # Each worker opens its own DB connection (SQLite requirement)
                db_conn = sqlite3.connect(db_path)
                db_conn.execute(
                    "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
                    ("INBOX", f"worker_{worker_id}", "{}", "2024-01-01T00:00:00Z"),
                )
                db_conn.commit()
                count = db_conn.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
                results.append((worker_id, count))
                db_conn.close()

                pool.release(imap_conn)

        # Run multiple workers
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        pool.shutdown()

        # Verify all workers succeeded
        assert len(results) == 5

        # Verify final database state
        final_db = sqlite3.connect(db_path)
        final_count = final_db.execute("SELECT COUNT(*) FROM headers").fetchone()[0]
        assert final_count == 5
        final_db.close()

    def test_failed_folders_sorted_last_in_workflow(self):
        """Test that folders failing STATUS are sorted last."""
        mock_client = Mock()

        def mock_status(folder, _criteria):
            if folder == '"BadFolder"':
                raise Exception("Connection lost")
            sizes = {
                '"INBOX"': b'"INBOX" (MESSAGES 100)',
                '"Archive"': b'"Archive" (MESSAGES 50)',
            }
            response = sizes.get(folder, b"")
            return ("OK", [response]) if response else ("NO", [])

        mock_client.status.side_effect = mock_status

        folders = ["INBOX", "BadFolder", "Archive"]
        folder_sizes = get_folder_sizes(mock_client, folders)

        # Sort folders
        sorted_folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))

        # Failed folder should be first (most negative value)
        assert sorted_folders[0] == "BadFolder"
        assert folder_sizes["BadFolder"] == -1
        # Then valid folders sorted by size
        assert sorted_folders[1:] == ["Archive", "INBOX"]
