#!/usr/bin/env python3
"""Test the complete workflow: rule evaluation -> action creation -> execution simulation."""

import json
import tempfile
from pathlib import Path

from core.database import init_db
from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules


def test_keyword_rule_workflow():
    """Test creating and evaluating rules with keyword actions."""
    print("\n=== Testing Keyword Rule Workflow ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        log_path = Path(tmpdir) / "test.log"

        logger = JsonLogger(log_path)
        db = init_db(db_path, logger=logger)

        # Create a test message header
        test_header = json.dumps({
            "header": "From: newsletter@example.com\r\nSubject: Daily Newsletter\r\nMessage-ID: <test123@example.com>\r\n"
        })

        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            ("INBOX", "1001", test_header, "2025-01-01T00:00:00")
        )

        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            ("INBOX", "1002", test_header, "2025-01-01T00:00:00")
        )

        db.commit()

        # Define rules with keyword actions
        rules = [
            {
                "name": "Mark newsletters as seen",
                "priority": 100,
                "conditions": [
                    {"header": "from", "contains": "newsletter@example.com"}
                ],
                "action": {
                    "type": "set_keywords",
                    "keywords": ["newsletter", "\\Seen"]
                }
            },
            {
                "name": "Remove work flag from newsletters",
                "priority": 90,
                "conditions": [
                    {"header": "subject", "contains": "Newsletter"}
                ],
                "action": {
                    "type": "remove_keywords",
                    "keywords": ["work", "urgent"]
                }
            }
        ]

        # Evaluate rules
        print("\nEvaluating rules...")
        timer, count, matches = evaluate_rules(
            db,
            rules,
            scope="all",
            dry_run=False,
            show_progress=False,
            logger=logger,
            verbose=True
        )

        print("\nEvaluation results:")
        print(f"  Messages processed: {count}")
        print(f"  Total matches: {matches}")

        # Check created actions
        cursor = db.execute(
            "SELECT uid, folder, rule_name, action_type, action_data, status "
            "FROM actions ORDER BY priority DESC, created_at ASC"
        )
        actions = cursor.fetchall()

        print(f"\nCreated {len(actions)} actions:")
        for uid, folder, rule_name, action_type, action_data, status in actions:
            keywords = []
            if action_data:
                data = json.loads(action_data)
                keywords = data.get("keywords", [])
            print(f"  - {folder}/{uid}: {rule_name} ({action_type}) - keywords: {keywords} [{status}]")

        # Verify actions. The engine is first-match-wins: each message gets one
        # action from the winning rule. Both rules match both messages, and
        # "Remove work flag from newsletters" (priority 90, lower number =
        # higher precedence) beats "Mark newsletters as seen" (priority 100).
        assert len(actions) == 2, f"Expected 2 actions (one per message, first match wins), got {len(actions)}"

        assert {a[0] for a in actions} == {"1001", "1002"}, "Each message should get exactly one action"
        for uid, folder, rule_name, action_type, action_data, status in actions:
            assert rule_name == "Remove work flag from newsletters", "Winning rule mismatch"
            assert action_type == "remove_keywords", "Action should be remove_keywords"
            data = json.loads(action_data)
            assert data["keywords"] == ["work", "urgent"], "Remove keywords mismatch"

        print("\n✅ Keyword rule workflow test passed")
        db.close()


def test_move_and_keyword_actions():
    """Test that move and keyword actions can coexist."""
    print("\n=== Testing Mixed Action Types ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        log_path = Path(tmpdir) / "test.log"

        logger = JsonLogger(log_path)
        db = init_db(db_path, logger=logger)

        # Create test headers
        newsletter_header = json.dumps({
            "header": "From: newsletter@example.com\r\nSubject: Newsletter\r\n"
        })

        spam_header = json.dumps({
            "header": "From: spam@example.com\r\nSubject: Buy now!\r\n"
        })

        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            ("INBOX", "2001", newsletter_header, "2025-01-01T00:00:00")
        )

        db.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            ("INBOX", "2002", spam_header, "2025-01-01T00:00:00")
        )

        db.commit()

        # Define mixed rules
        rules = [
            {
                "name": "Move spam to Junk",
                "priority": 100,
                "conditions": [
                    {"header": "from", "contains": "spam@example.com"}
                ],
                "action": {
                    "type": "move",
                    "target": "Junk"
                }
            },
            {
                "name": "Tag newsletters",
                "priority": 90,
                "conditions": [
                    {"header": "from", "contains": "newsletter@example.com"}
                ],
                "action": {
                    "type": "set_keywords",
                    "keywords": ["newsletter"]
                }
            }
        ]

        # Evaluate rules
        print("\nEvaluating mixed rules...")
        timer, count, matches = evaluate_rules(
            db,
            rules,
            scope="all",
            dry_run=False,
            show_progress=False,
            logger=logger,
            verbose=False
        )

        # Check actions
        cursor = db.execute(
            "SELECT uid, action_type, target, action_data FROM actions ORDER BY uid"
        )
        actions = cursor.fetchall()

        print(f"\nCreated {len(actions)} actions:")
        for uid, action_type, target, action_data in actions:
            if action_type == "move":
                print(f"  - UID {uid}: {action_type} to {target}")
            else:
                data = json.loads(action_data)
                keywords = data.get("keywords", [])
                print(f"  - UID {uid}: {action_type} with keywords {keywords}")

        assert len(actions) == 2, f"Expected 2 actions, got {len(actions)}"
        assert actions[0][1] == "set_keywords", "First action should be set_keywords"
        assert actions[1][1] == "move", "Second action should be move"
        assert actions[1][2] == "Junk", "Move target should be Junk"

        print("\n✅ Mixed action types test passed")
        db.close()


if __name__ == "__main__":
    print("Testing Complete Keyword Actions Workflow")
    print("=" * 60)

    try:
        test_keyword_rule_workflow()
        test_move_and_keyword_actions()

        print("\n" + "=" * 60)
        print("✅ All workflow tests passed successfully!")
        print("\nSummary:")
        print("  - Database schema supports action_type and action_data")
        print("  - Rule engine stores keyword actions correctly")
        print("  - Actions are created with proper JSON serialization")
        print("  - Move and keyword actions coexist properly")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
