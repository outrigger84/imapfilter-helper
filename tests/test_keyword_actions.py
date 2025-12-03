#!/usr/bin/env python3
"""Test script for keyword actions implementation."""

import json
import sqlite3
import tempfile
from pathlib import Path

from core.database import init_db
from core.logging_utils import JsonLogger


def test_database_schema():
    """Test that the database schema includes action_type and action_data columns."""
    print("\n=== Testing Database Schema ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        log_path = Path(tmpdir) / "test.log"

        logger = JsonLogger(log_path)
        db = init_db(db_path, logger=logger)

        # Check that actions table has the new columns
        cursor = db.execute("PRAGMA table_info(actions)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        print(f"Actions table columns: {list(columns.keys())}")

        assert "action_type" in columns, "action_type column missing"
        assert "action_data" in columns, "action_data column missing"

        print("✅ Database schema test passed")
        db.close()


def test_action_insertion():
    """Test inserting actions with different action types."""
    print("\n=== Testing Action Insertion ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        log_path = Path(tmpdir) / "test.log"

        logger = JsonLogger(log_path)
        db = init_db(db_path, logger=logger)

        # Insert a move action
        db.execute(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("1", "INBOX", "test_move", "Archive", 100, "pending", "2025-01-01T00:00:00", "move", None),
        )

        # Insert a set_keywords action
        keywords_data = json.dumps({"keywords": ["newsletter", "\\Seen"]})
        db.execute(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2", "INBOX", "test_set_keywords", "", 100, "pending", "2025-01-01T00:00:00", "set_keywords", keywords_data),
        )

        # Insert a remove_keywords action
        remove_data = json.dumps({"keywords": ["work", "urgent"]})
        db.execute(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("3", "INBOX", "test_remove_keywords", "", 100, "pending", "2025-01-01T00:00:00", "remove_keywords", remove_data),
        )

        db.commit()

        # Verify actions were inserted
        cursor = db.execute(
            "SELECT uid, rule_name, action_type, action_data FROM actions ORDER BY uid"
        )
        actions = cursor.fetchall()

        print(f"\nInserted {len(actions)} actions:")
        for uid, rule_name, action_type, action_data in actions:
            data_str = ""
            if action_data:
                data = json.loads(action_data)
                data_str = f" with data: {data}"
            print(f"  - UID {uid}: {rule_name} ({action_type}){data_str}")

        assert len(actions) == 3, f"Expected 3 actions, got {len(actions)}"
        assert actions[0][2] == "move", "First action should be move"
        assert actions[1][2] == "set_keywords", "Second action should be set_keywords"
        assert actions[2][2] == "remove_keywords", "Third action should be remove_keywords"

        # Verify action_data parsing
        set_keywords_data = json.loads(actions[1][3])
        assert set_keywords_data["keywords"] == ["newsletter", "\\Seen"], "Keywords data mismatch"

        remove_keywords_data = json.loads(actions[2][3])
        assert remove_keywords_data["keywords"] == ["work", "urgent"], "Keywords data mismatch"

        print("✅ Action insertion test passed")
        db.close()


def test_action_selection():
    """Test selecting actions with action_type and action_data."""
    print("\n=== Testing Action Selection ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        log_path = Path(tmpdir) / "test.log"

        logger = JsonLogger(log_path)
        db = init_db(db_path, logger=logger)

        # Insert test data
        keywords_data = json.dumps({"keywords": ["test1", "test2"]})
        db.execute(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("100", "INBOX", "test_rule", "", 100, "pending", "2025-01-01T00:00:00", "set_keywords", keywords_data),
        )
        db.commit()

        # Select with the same query structure as executor
        cursor = db.execute(
            "SELECT id, uid, folder, target, rule_name, priority, created_at, action_type, action_data "
            "FROM actions WHERE status='pending'"
        )

        row = cursor.fetchone()
        assert row is not None, "No action found"

        action_id, uid, folder, target, rule_name, priority, created_at, action_type, action_data = row

        print(f"\nSelected action:")
        print(f"  ID: {action_id}")
        print(f"  UID: {uid}")
        print(f"  Folder: {folder}")
        print(f"  Action Type: {action_type}")
        print(f"  Action Data: {action_data}")

        assert action_type == "set_keywords", "Wrong action type"
        assert action_data is not None, "Action data is None"

        data = json.loads(action_data)
        assert "keywords" in data, "Keywords missing from action_data"
        assert data["keywords"] == ["test1", "test2"], "Keywords mismatch"

        print("✅ Action selection test passed")
        db.close()


if __name__ == "__main__":
    print("Testing Phase 4 - Executor Keyword Actions Implementation")
    print("=" * 60)

    try:
        test_database_schema()
        test_action_insertion()
        test_action_selection()

        print("\n" + "=" * 60)
        print("✅ All tests passed successfully!")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
