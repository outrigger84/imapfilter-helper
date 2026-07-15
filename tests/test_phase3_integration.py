"""
Integration test for Phase 3: Keyword and Age-Based Conditions
Tests the full rule evaluation pipeline with flags and dates.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules


def test_phase3_integration_flags_and_age():
    """
    Integration test demonstrating keyword and age-based rules working together.
    """
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        log_path = tmp_path / "test.log"

        # Create database
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE headers ("
            "folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
            "PRIMARY KEY (folder, uid))"
        )
        db.execute(
            "CREATE TABLE actions ("
            "uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
            "priority INTEGER, status TEXT, created_at TEXT, "
            "action_type TEXT, action_data TEXT)"
        )

        # Create test messages with different characteristics
        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)
        recent_date = now - timedelta(days=30)

        messages = [
            # Message 1: Old newsletter (should match archive rule)
            (
                "INBOX",
                "1",
                json.dumps({
                    "header": "From: news@example.com\nSubject: Monthly Newsletter\n\n",
                    "flags": ["\\Seen", "newsletter"],
                    "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 2: Recent newsletter (should not match archive rule)
            (
                "INBOX",
                "2",
                json.dumps({
                    "header": "From: news@example.com\nSubject: Latest News\n\n",
                    "flags": ["\\Seen", "newsletter"],
                    "internaldate": recent_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 3: Unread important (should match priority rule)
            (
                "INBOX",
                "3",
                json.dumps({
                    "header": "From: boss@company.com\nSubject: Urgent Action Required\n\n",
                    "flags": ["\\Flagged"],  # Flagged but not seen
                    "internaldate": recent_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 4: Old spam (should match delete rule)
            (
                "INBOX",
                "4",
                json.dumps({
                    "header": "From: spam@junk.com\nSubject: Amazing Offer!!!\n\n",
                    "flags": ["\\Seen", "Junk"],
                    "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 5: Old format (header only, no flags/date) - backward compatibility
            (
                "INBOX",
                "5",
                json.dumps({
                    "header": "From: old@example.com\nSubject: Legacy Email\n\n"
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 6: Plain old format (just header string) - backward compatibility
            (
                "INBOX",
                "6",
                "From: ancient@example.com\nSubject: Ancient Email\n\n",
                "2024-01-01T00:00:00Z"
            ),
        ]

        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            messages
        )
        db.commit()

        # Define rules using new condition types
        rules = [
            # Rule 1: Archive old newsletters (uses flags + age)
            {
                "name": "Archive Old Newsletters",
                "priority": 100,
                "conditions": {
                    "all": [
                        {"has_keyword": "newsletter"},
                        {"age_days_gt": 365}
                    ]
                },
                "action": {"type": "move", "target": "Archive/Newsletters"}
            },
            # Rule 2: Move unread important to priority folder (uses flags)
            {
                "name": "Priority Unread",
                "priority": 90,
                "conditions": {
                    "all": [
                        {"has_keyword": "\\Flagged"},
                        {"lacks_keyword": "\\Seen"}
                    ]
                },
                "action": {"type": "move", "target": "Priority"}
            },
            # Rule 3: Delete old spam (uses flags + age)
            {
                "name": "Delete Old Spam",
                "priority": 80,
                "conditions": {
                    "all": [
                        {"has_keyword": "Junk"},
                        {"age_days_gt": 90}
                    ]
                },
                "action": {"type": "move", "target": "[Gmail]/Trash"}
            },
            # Rule 4: Traditional header-only rule (backward compatibility)
            {
                "name": "Legacy Rule",
                "priority": 70,
                "conditions": {
                    "header": "subject",
                    "contains": "Legacy"
                },
                "action": {"type": "move", "target": "Archive"}
            },
        ]

        # Run evaluation
        logger = JsonLogger(log_path)
        timer, rule_count, match_count = evaluate_rules(
            db,
            rules,
            scope="all",
            dry_run=True,
            show_progress=False,
            logger=logger,
            verbose=False
        )

        # Verify results
        cur = db.cursor()
        cur.execute("SELECT uid, rule_name, target FROM actions ORDER BY uid")
        actions = cur.fetchall()

        # Expected matches:
        # UID 1: Old newsletter -> Archive Old Newsletters
        # UID 3: Unread important -> Priority Unread
        # UID 4: Old spam -> Delete Old Spam
        # UID 5: Legacy format -> Legacy Rule

        assert len(actions) == 4, f"Expected 4 matches, got {len(actions)}"

        action_dict = {uid: (rule, target) for uid, rule, target in actions}

        # Verify each match
        assert "1" in action_dict
        assert action_dict["1"] == ("Archive Old Newsletters", "Archive/Newsletters")

        assert "3" in action_dict
        assert action_dict["3"] == ("Priority Unread", "Priority")

        assert "4" in action_dict
        assert action_dict["4"] == ("Delete Old Spam", "[Gmail]/Trash")

        assert "5" in action_dict
        assert action_dict["5"] == ("Legacy Rule", "Archive")

        # Verify no matches for:
        # UID 2: Recent newsletter (not old enough)
        # UID 6: Ancient email (no matching rule)
        assert "2" not in action_dict
        assert "6" not in action_dict

        db.close()
        print("\n✅ Phase 3 Integration Test Passed!")
        print(f"   - Processed {match_count} matches")
        print("   - Verified flag-based conditions")
        print("   - Verified age-based conditions")
        print("   - Verified backward compatibility")


def test_phase3_complex_logical_operators():
    """
    Test complex combinations of flags, age, and header conditions with logical operators.
    """
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test.db"
        log_path = tmp_path / "test.log"

        # Create database
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE headers ("
            "folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
            "PRIMARY KEY (folder, uid))"
        )
        db.execute(
            "CREATE TABLE actions ("
            "uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
            "priority INTEGER, status TEXT, created_at TEXT, "
            "action_type TEXT, action_data TEXT)"
        )

        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)

        messages = [
            # Message 1: Should match complex rule (newsletter OR important) AND old
            (
                "INBOX",
                "1",
                json.dumps({
                    "header": "Subject: Important Newsletter\n\n",
                    "flags": ["newsletter"],
                    "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 2: Should also match (has important flag)
            (
                "INBOX",
                "2",
                json.dumps({
                    "header": "Subject: Critical Update\n\n",
                    "flags": ["important"],
                    "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
            # Message 3: Should NOT match (has flag but not old enough)
            (
                "INBOX",
                "3",
                json.dumps({
                    "header": "Subject: Recent Newsletter\n\n",
                    "flags": ["newsletter"],
                    "internaldate": now.strftime("%d-%b-%Y %H:%M:%S +0000")
                }),
                "2024-01-01T00:00:00Z"
            ),
        ]

        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            messages
        )
        db.commit()

        # Complex rule: (newsletter OR important) AND older than 1 year
        rules = [
            {
                "name": "Archive Old Important Content",
                "priority": 100,
                "conditions": {
                    "all": [
                        {
                            "any": [
                                {"has_keyword": "newsletter"},
                                {"has_keyword": "important"}
                            ]
                        },
                        {"age_days_gt": 365}
                    ]
                },
                "action": {"type": "move", "target": "Archive/Important"}
            }
        ]

        logger = JsonLogger(log_path)
        timer, rule_count, match_count = evaluate_rules(
            db,
            rules,
            scope="all",
            dry_run=True,
            show_progress=False,
            logger=logger,
            verbose=False
        )

        cur = db.cursor()
        cur.execute("SELECT uid FROM actions ORDER BY uid")
        matched_uids = [row[0] for row in cur.fetchall()]

        assert len(matched_uids) == 2, f"Expected 2 matches, got {len(matched_uids)}"
        assert "1" in matched_uids
        assert "2" in matched_uids
        assert "3" not in matched_uids

        db.close()
        print("\n✅ Complex Logical Operators Test Passed!")
        print("   - Verified nested ANY/ALL with flags and age")


if __name__ == "__main__":
    test_phase3_integration_flags_and_age()
    test_phase3_complex_logical_operators()
    print("\n" + "="*60)
    print("All Phase 3 Integration Tests Passed! 🎉")
    print("="*60)
