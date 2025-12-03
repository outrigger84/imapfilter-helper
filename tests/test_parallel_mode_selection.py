"""Test parallel mode selection logic for execute phase."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from core.executor import should_use_parallel_mode, _count_unique_source_folders


@pytest.fixture
def temp_db():
    """Create a temporary database with test data."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db') as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE actions (
            id INTEGER PRIMARY KEY,
            folder TEXT,
            uid TEXT,
            status TEXT,
            target TEXT,
            rule_name TEXT,
            priority INTEGER,
            created_at TEXT,
            action_type TEXT,
            action_data TEXT
        )
    """)
    conn.commit()

    yield db_path

    conn.close()
    db_path.unlink()


def test_count_unique_source_folders_empty(temp_db):
    """Test counting with no pending actions."""
    count = _count_unique_source_folders(temp_db)
    assert count == 0


def test_count_unique_source_folders_single(temp_db):
    """Test counting with single folder."""
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
        ("INBOX", "123", "pending")
    )
    conn.commit()
    conn.close()

    count = _count_unique_source_folders(temp_db)
    assert count == 1


def test_count_unique_source_folders_multiple(temp_db):
    """Test counting with multiple unique folders."""
    conn = sqlite3.connect(str(temp_db))
    for i in range(6):
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    conn.commit()
    conn.close()

    count = _count_unique_source_folders(temp_db)
    assert count == 6


def test_count_unique_source_folders_duplicates(temp_db):
    """Test counting ignores duplicate folders."""
    conn = sqlite3.connect(str(temp_db))
    # Same folder multiple times
    for i in range(5):
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            ("INBOX", f"uid{i}", "pending")
        )
    # Different folder
    conn.execute(
        "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
        ("Sent", "uid10", "pending")
    )
    conn.commit()
    conn.close()

    count = _count_unique_source_folders(temp_db)
    assert count == 2  # INBOX and Sent


def test_count_unique_source_folders_ignores_non_pending(temp_db):
    """Test counting ignores non-pending actions."""
    conn = sqlite3.connect(str(temp_db))
    # Pending actions in 3 folders
    for i in range(3):
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    # Done actions in 2 more folders
    for i in range(3, 5):
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "done")
        )
    conn.commit()
    conn.close()

    count = _count_unique_source_folders(temp_db)
    assert count == 3  # Only pending folders counted


def test_should_use_parallel_mode_force_sequential(temp_db):
    """Test forcing sequential mode with parallel_workers=0."""
    # Even with 10 folders, should force sequential
    conn = sqlite3.connect(str(temp_db))
    for i in range(10):
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    conn.commit()
    conn.close()

    result = should_use_parallel_mode(temp_db, parallel_workers=0)
    assert result is False


def test_should_use_parallel_mode_force_parallel(temp_db):
    """Test forcing parallel mode with parallel_workers>0."""
    # Even with 1 folder, should force parallel
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
        ("INBOX", "uid1", "pending")
    )
    conn.commit()
    conn.close()

    result = should_use_parallel_mode(temp_db, parallel_workers=5)
    assert result is True


def test_should_use_parallel_mode_auto_below_threshold(temp_db):
    """Test auto-detect with <5 folders (should be sequential)."""
    conn = sqlite3.connect(str(temp_db))
    for i in range(4):  # 4 folders
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    conn.commit()
    conn.close()

    result = should_use_parallel_mode(temp_db, parallel_workers=None)
    assert result is False


def test_should_use_parallel_mode_auto_at_threshold(temp_db):
    """Test auto-detect with exactly 5 folders (should be parallel)."""
    conn = sqlite3.connect(str(temp_db))
    for i in range(5):  # 5 folders
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    conn.commit()
    conn.close()

    result = should_use_parallel_mode(temp_db, parallel_workers=None)
    assert result is True


def test_should_use_parallel_mode_auto_above_threshold(temp_db):
    """Test auto-detect with >5 folders (should be parallel)."""
    conn = sqlite3.connect(str(temp_db))
    for i in range(10):  # 10 folders
        conn.execute(
            "INSERT INTO actions (folder, uid, status) VALUES (?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending")
        )
    conn.commit()
    conn.close()

    result = should_use_parallel_mode(temp_db, parallel_workers=None)
    assert result is True


def test_should_use_parallel_mode_auto_empty(temp_db):
    """Test auto-detect with no pending actions (should be sequential)."""
    result = should_use_parallel_mode(temp_db, parallel_workers=None)
    assert result is False
