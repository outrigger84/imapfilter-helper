from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import init_db
from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules, rule_match, conditions_match, _parse_header_map


@pytest.fixture()
def rule_test_env(tmp_path: Path):
    db_path = tmp_path / "rules.db"
    log_path = tmp_path / "log.jsonl"

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    header = "From: newsletter@example.com\nSubject: Urgent update\n\n"
    db.execute(
        "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
        ("1", "INBOX", json.dumps({"header": header}), None),
    )
    db.commit()

    try:
        yield db, logger
    finally:
        db.close()


def _run_rule(db, logger, rule):
    return evaluate_rules(
        db,
        [rule],
        scope="all",
        dry_run=True,
        show_progress=False,
        logger=logger,
        folders=None,
    )


def test_evaluate_rules_with_all_conditions(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "AND rule",
        "conditions": [
            {"header": "from", "contains": "newsletter@example.com"},
            {"header": "subject", "contains": "Urgent"},
        ],
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_with_any_conditions(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "OR rule",
        "conditions": {
            "any": [
                {"header": "subject", "contains": "Digest"},
                {"header": "from", "contains": "newsletter@example.com"},
            ]
        },
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_with_mixed_logic(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "Mixed logic rule",
        "conditions": {
            "all": [
                {
                    "any": [
                        {"header": "from", "contains": "alerts@example.com"},
                        {"header": "from", "contains": "newsletter@example.com"},
                    ]
                },
                {"header": "subject", "contains": "update"},
            ]
        },
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_streaming_large_dataset(tmp_path: Path):
    db_path = tmp_path / "large_rules.db"
    log_path = tmp_path / "large_log.jsonl"

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    try:
        total_folders = 5
        messages_per_folder = 300
        expected_matches = 0

        for folder_idx in range(total_folders):
            folder = f"Folder-{folder_idx}"
            for msg_idx in range(messages_per_folder):
                subject = "Match me" if msg_idx % 10 == 0 else "Other"
                header = f"From: sender{folder_idx}@example.com\nSubject: {subject}\n\n"
                db.execute(
                    "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
                    (
                        f"{folder_idx}-{msg_idx}",
                        folder,
                        json.dumps({"header": header}),
                        None,
                    ),
                )
                if subject == "Match me":
                    expected_matches += 1

        db.commit()

        rule = {
            "name": "Subject contains match",
            "conditions": {"header": "subject", "contains": "Match me"},
            "action": {"type": "move", "target": "Matches"},
        }

        _timer, count, matches = evaluate_rules(
            db,
            [rule],
            scope="all",
            dry_run=True,
            show_progress=False,
            logger=logger,
        )

        assert count == 1
        assert matches == expected_matches

        cursor = db.cursor()
        cursor.execute("SELECT COUNT(*) FROM actions")
        (action_count,) = cursor.fetchone()
        assert action_count == expected_matches
    finally:
        db.close()


def test_evaluate_rules_respects_folder_filter(rule_test_env):
    db, logger = rule_test_env

    rule = {
        "name": "Match anything",
        "conditions": {"header": "subject", "contains": "Urgent"},
        "action": {"type": "move", "target": "Archive"},
    }

    _timer, count, matches = evaluate_rules(
        db,
        [rule],
        scope="all",
        dry_run=True,
        show_progress=False,
        logger=logger,
        folders=["Archive"],
    )

    assert count == 1
    assert matches == 0


# =============================================================================
# Test Suite 1: Specific Negation Operators (12 tests)
# =============================================================================

class TestNegationOperators:
    """Test negation operators for header conditions."""

    @pytest.fixture
    def sample_header(self):
        """Sample header for testing."""
        return _parse_header_map(
            "From: receipts@amazon.com\n"
            "To: user@example.com\n"
            "Subject: Your Order #12345\n\n"
        )

    def test_not_contains_match(self, sample_header):
        """Test not_contains when substring is absent."""
        condition = {"header": "from", "not_contains": "noreply"}
        assert rule_match(sample_header, condition) is True

    def test_not_contains_no_match(self, sample_header):
        """Test not_contains when substring is present."""
        condition = {"header": "from", "not_contains": "receipts"}
        assert rule_match(sample_header, condition) is False

    def test_not_contains_case_insensitive(self, sample_header):
        """Test not_contains is case-insensitive."""
        condition = {"header": "from", "not_contains": "RECEIPTS"}
        assert rule_match(sample_header, condition) is False

    def test_not_equals_match(self, sample_header):
        """Test not_equals when value differs."""
        condition = {"header": "from", "not_equals": "noreply@amazon.com"}
        assert rule_match(sample_header, condition) is True

    def test_not_equals_no_match(self, sample_header):
        """Test not_equals when value matches exactly."""
        condition = {"header": "from", "not_equals": "receipts@amazon.com"}
        assert rule_match(sample_header, condition) is False

    def test_not_equals_case_insensitive(self, sample_header):
        """Test not_equals is case-insensitive."""
        condition = {"header": "from", "not_equals": "RECEIPTS@AMAZON.COM"}
        assert rule_match(sample_header, condition) is False

    def test_not_regex_match(self, sample_header):
        """Test not_regex when pattern doesn't match."""
        condition = {"header": "from", "not_regex": r"^noreply@"}
        assert rule_match(sample_header, condition) is True

    def test_not_regex_no_match(self, sample_header):
        """Test not_regex when pattern matches."""
        condition = {"header": "from", "not_regex": r"receipts@.*\.com"}
        assert rule_match(sample_header, condition) is False

    def test_not_regex_case_insensitive(self, sample_header):
        """Test not_regex is case-insensitive."""
        condition = {"header": "from", "not_regex": r"RECEIPTS@"}
        assert rule_match(sample_header, condition) is False

    def test_equals_operator(self, sample_header):
        """Test new equals operator (positive exact match)."""
        condition = {"header": "from", "equals": "receipts@amazon.com"}
        assert rule_match(sample_header, condition) is True

        condition = {"header": "from", "equals": "other@amazon.com"}
        assert rule_match(sample_header, condition) is False

    def test_empty_header_value(self):
        """Test negation with missing header."""
        header = {"subject": "Test"}  # No "from" field
        condition = {"header": "from", "not_contains": "spam"}
        assert rule_match(header, condition) is True

    def test_mixed_positive_negative_conditions(self, sample_header):
        """Test combining positive and negative conditions."""
        conditions = {
            "all": [
                {"header": "from", "contains": "@amazon.com"},
                {"header": "from", "not_contains": "noreply"},
                {"header": "subject", "not_equals": "Newsletter"}
            ]
        }
        assert conditions_match(sample_header, conditions) is True


# =============================================================================
# Test Suite 2: NOT Wrapper (9 tests)
# =============================================================================

class TestNotWrapper:
    """Test NOT wrapper for complex negation."""

    @pytest.fixture
    def test_header(self):
        """Sample header for testing."""
        return _parse_header_map(
            "From: user@example.com\n"
            "Subject: Important Message\n\n"
        )

    def test_not_wrapper_simple_condition(self, test_header):
        """Test NOT wrapper with simple condition."""
        conditions = {"not": {"header": "from", "contains": "other.com"}}
        assert conditions_match(test_header, conditions) is True

        conditions = {"not": {"header": "from", "contains": "example.com"}}
        assert conditions_match(test_header, conditions) is False

    def test_not_wrapper_with_any_group(self, test_header):
        """Test NOT wrapper negating an OR group."""
        conditions = {
            "not": {
                "any": [
                    {"header": "from", "contains": "spam.com"},
                    {"header": "from", "contains": "junk.com"}
                ]
            }
        }
        assert conditions_match(test_header, conditions) is True

    def test_not_wrapper_with_all_group(self, test_header):
        """Test NOT wrapper negating an AND group."""
        conditions = {
            "not": {
                "all": [
                    {"header": "from", "contains": "example.com"},
                    {"header": "subject", "contains": "Newsletter"}
                ]
            }
        }
        assert conditions_match(test_header, conditions) is True

    def test_not_wrapper_with_flags(self):
        """Test NOT wrapper with flag conditions."""
        header = _parse_header_map("From: test@example.com\n\n")
        flags = ["\\Seen", "important"]

        conditions = {"not": {"has_keyword": "\\Flagged"}}
        assert conditions_match(header, conditions, flags=flags) is True

        conditions = {"not": {"has_keyword": "\\Seen"}}
        assert conditions_match(header, conditions, flags=flags) is False

    def test_not_wrapper_nested_in_all(self, test_header):
        """Test NOT wrapper inside ALL group (typical use case)."""
        conditions = {
            "all": [
                {"header": "from", "contains": "example.com"},
                {
                    "not": {
                        "any": [
                            {"header": "from", "contains": "spam"},
                            {"header": "subject", "contains": "promo"}
                        ]
                    }
                }
            ]
        }
        assert conditions_match(test_header, conditions) is True

    def test_not_wrapper_nested_in_any(self, test_header):
        """Test NOT wrapper inside ANY group."""
        conditions = {
            "any": [
                {"header": "subject", "contains": "Important"},
                {"not": {"header": "from", "contains": "spam.com"}}
            ]
        }
        assert conditions_match(test_header, conditions) is True

    def test_double_negation(self, test_header):
        """Test NOT wrapper with negated operators (edge case)."""
        conditions = {"not": {"header": "from", "not_contains": "example.com"}}
        assert conditions_match(test_header, conditions) is True

    def test_not_wrapper_empty_content(self):
        """Test NOT wrapper with empty dict (edge case)."""
        header = _parse_header_map("From: test@example.com\n\n")
        conditions = {"not": {}}
        assert conditions_match(header, conditions) is True


# =============================================================================
# Test Suite 3: Integration Test (Real-world scenario)
# =============================================================================

def test_amazon_receipts_use_case(tmp_path):
    """Test the motivating use case: Amazon but not receipts."""
    db_path = tmp_path / "test.db"
    log_path = tmp_path / "test.jsonl"

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    # Insert test messages
    messages = [
        ("1", "INBOX", json.dumps({"header": "From: receipts@amazon.com\nSubject: Order\n\n"})),
        ("2", "INBOX", json.dumps({"header": "From: shipment@amazon.com\nSubject: Shipped\n\n"})),
        ("3", "INBOX", json.dumps({"header": "From: deals@amazon.com\nSubject: Deal\n\n"})),
        ("4", "INBOX", json.dumps({"header": "From: user@other.com\nSubject: Hello\n\n"})),
    ]

    for uid, folder, data in messages:
        db.execute(
            "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
            (uid, folder, data, None)
        )
    db.commit()

    # Rule: Amazon but not receipts
    rule = {
        "name": "Amazon non-receipts",
        "conditions": {
            "all": [
                {"header": "from", "contains": "@amazon.com"},
                {"header": "from", "not_contains": "receipts@"}
            ]
        },
        "action": {"type": "move", "target": "Shopping"}
    }

    _timer, count, matches = evaluate_rules(
        db, [rule],
        scope="all",
        dry_run=True,
        show_progress=False,
        logger=logger
    )

    assert matches == 2  # Should match uid 2 and 3, not 1 or 4

    # Verify the correct messages matched
    cursor = db.cursor()
    cursor.execute("SELECT uid FROM actions ORDER BY uid")
    matched_uids = [row[0] for row in cursor.fetchall()]
    assert matched_uids == ["2", "3"]

    db.close()
