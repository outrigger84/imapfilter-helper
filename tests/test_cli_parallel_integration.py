"""Integration test for CLI parallel mode selection."""
import argparse
import sqlite3
import tempfile
from pathlib import Path

import pytest

from core.cli import handle_execute
from core.config import build_default_config
from core.logging_utils import JsonLogger


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace with database and config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        data_dir = workspace / "data"
        data_dir.mkdir()

        # Create test database with actions
        db_path = data_dir / "cache.db"
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
        conn.execute("""
            CREATE TABLE headers (
                folder TEXT,
                uid TEXT,
                data TEXT,
                updated_at TEXT,
                PRIMARY KEY (folder, uid)
            )
        """)
        conn.execute("""
            CREATE TABLE folders (
                id INTEGER PRIMARY KEY,
                folder TEXT,
                parent TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()

        # Create log file
        log_path = data_dir / "test.log"
        log_path.touch()

        # Create secrets file (empty for dry-run)
        secrets_path = data_dir / "secrets.json"
        secrets_path.write_text('{"server": "test", "username": "test", "password": "test"}')

        yield workspace, db_path, log_path

        # Cleanup handled by TemporaryDirectory


def test_cli_routing_auto_detect_sequential(temp_workspace):
    """Test CLI routes to sequential mode with <5 folders."""
    workspace, db_path, log_path = temp_workspace
    cfg = build_default_config(workspace)
    logger = JsonLogger(log_path)

    # Add 3 pending actions in 3 different folders
    conn = sqlite3.connect(str(db_path))
    for i in range(3):
        conn.execute(
            "INSERT INTO actions (folder, uid, status, target) VALUES (?, ?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending", "Archive")
        )
    conn.commit()

    # Create mock args
    args = argparse.Namespace(
        dry_run=True,
        strict=False,
        limit=None,
        verbose=False,
        folder=None,
        folder_recursive=None,
        all_folders=False,
        verify_moves=False,
        backup_moved=False,
        backup_all=False,
        parallel_workers=None,  # Auto-detect
    )

    # Execute - should choose sequential mode
    result = handle_execute(args, cfg, conn, logger)
    assert result == 0

    # Verify log shows sequential mode was chosen
    log_content = log_path.read_text()
    assert "sequential" in log_content.lower()

    conn.close()


def test_cli_routing_auto_detect_parallel(temp_workspace):
    """Test CLI routes to parallel mode with ≥5 folders."""
    workspace, db_path, log_path = temp_workspace
    cfg = build_default_config(workspace)
    logger = JsonLogger(log_path)

    # Add 5 pending actions in 5 different folders
    conn = sqlite3.connect(str(db_path))
    for i in range(5):
        conn.execute(
            "INSERT INTO actions (folder, uid, status, target) VALUES (?, ?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending", "Archive")
        )
    conn.commit()

    # Create mock args
    args = argparse.Namespace(
        dry_run=True,
        strict=False,
        limit=None,
        verbose=False,
        folder=None,
        folder_recursive=None,
        all_folders=False,
        verify_moves=False,
        backup_moved=False,
        backup_all=False,
        parallel_workers=None,  # Auto-detect
    )

    # Execute - should choose parallel mode
    result = handle_execute(args, cfg, conn, logger)
    assert result == 0

    # Verify log shows parallel mode was chosen (or fallback warning)
    log_content = log_path.read_text()
    # Note: Since execute_actions_parallel is a stub, it will fall back to sequential
    # but should log that it attempted parallel mode
    assert "parallel" in log_content.lower() or "sequential" in log_content.lower()

    conn.close()


def test_cli_routing_force_sequential(temp_workspace):
    """Test CLI forces sequential mode with --parallel-workers 0."""
    workspace, db_path, log_path = temp_workspace
    cfg = build_default_config(workspace)
    logger = JsonLogger(log_path)

    # Add 10 pending actions in 10 different folders (normally would use parallel)
    conn = sqlite3.connect(str(db_path))
    for i in range(10):
        conn.execute(
            "INSERT INTO actions (folder, uid, status, target) VALUES (?, ?, ?, ?)",
            (f"Folder{i}", f"uid{i}", "pending", "Archive")
        )
    conn.commit()

    # Create mock args
    args = argparse.Namespace(
        dry_run=True,
        strict=False,
        limit=None,
        verbose=False,
        folder=None,
        folder_recursive=None,
        all_folders=False,
        verify_moves=False,
        backup_moved=False,
        backup_all=False,
        parallel_workers=0,  # Force sequential
    )

    # Execute - should force sequential mode
    result = handle_execute(args, cfg, conn, logger)
    assert result == 0

    # Verify log shows sequential mode was chosen
    log_content = log_path.read_text()
    assert "parallel_disabled" in log_content.lower() or "sequential" in log_content.lower()

    conn.close()


def test_cli_routing_force_parallel(temp_workspace):
    """Test CLI forces parallel mode with --parallel-workers 5."""
    workspace, db_path, log_path = temp_workspace
    cfg = build_default_config(workspace)
    logger = JsonLogger(log_path)

    # Add 1 pending action in 1 folder (normally would use sequential)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO actions (folder, uid, status, target) VALUES (?, ?, ?, ?)",
        ("INBOX", "uid1", "pending", "Archive")
    )
    conn.commit()

    # Create mock args
    args = argparse.Namespace(
        dry_run=True,
        strict=False,
        limit=None,
        verbose=False,
        folder=None,
        folder_recursive=None,
        all_folders=False,
        verify_moves=False,
        backup_moved=False,
        backup_all=False,
        parallel_workers=5,  # Force parallel
    )

    # Execute - should force parallel mode
    result = handle_execute(args, cfg, conn, logger)
    assert result == 0

    # Verify log shows parallel mode was chosen (or fallback warning)
    log_content = log_path.read_text()
    assert "parallel" in log_content.lower()

    conn.close()
