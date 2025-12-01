"""
Test Phase 3 Rule Engine Extensions: Keyword and Age-Based Conditions
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.rule_engine import (
    _evaluate_age_condition,
    _evaluate_condition_node,
    _evaluate_flag_condition,
    _extract_message_metadata,
    _parse_header_map,
    _parse_internaldate,
    conditions_match,
)


class TestParseDateFunctions:
    """Test date parsing functions."""

    def test_parse_internaldate_with_timezone(self):
        """Test parsing IMAP INTERNALDATE with timezone."""
        date_str = "28-Oct-2025 07:30:19 +0000"
        result = _parse_internaldate(date_str)
        assert result is not None
        assert result.year == 2025
        assert result.month == 10
        assert result.day == 28
        assert result.hour == 7
        assert result.minute == 30
        assert result.second == 19
        assert result.tzinfo == timezone.utc

    def test_parse_internaldate_without_timezone(self):
        """Test parsing IMAP INTERNALDATE without timezone (assumes UTC)."""
        date_str = "15-Jan-2024 14:22:33"
        result = _parse_internaldate(date_str)
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 14
        assert result.tzinfo == timezone.utc

    def test_parse_internaldate_invalid(self):
        """Test parsing invalid date strings."""
        assert _parse_internaldate("") is None
        assert _parse_internaldate(None) is None
        assert _parse_internaldate("invalid-date") is None
        assert _parse_internaldate("2024-01-01") is None  # Wrong format

    def test_parse_internaldate_with_different_timezone(self):
        """Test parsing IMAP INTERNALDATE with non-UTC timezone."""
        date_str = "01-Dec-2025 12:00:00 -0500"
        result = _parse_internaldate(date_str)
        assert result is not None
        assert result.year == 2025
        assert result.month == 12
        assert result.day == 1
        assert result.tzinfo is not None


class TestExtractMessageMetadata:
    """Test message metadata extraction."""

    def test_extract_metadata_full_format(self):
        """Test extraction with full metadata (header, flags, date)."""
        raw_header = "From: test@example.com\nSubject: Test\n\n"
        data = json.dumps({
            "header": raw_header,
            "flags": ["\\Seen", "newsletter"],
            "internaldate": "28-Oct-2025 07:30:19 +0000"
        })

        header, flags, date = _extract_message_metadata(data)

        assert header["from"] == "test@example.com"
        assert header["subject"] == "Test"
        assert flags == ["\\Seen", "newsletter"]
        assert date is not None
        assert date.year == 2025

    def test_extract_metadata_header_only(self):
        """Test extraction with header-only format (backward compatibility)."""
        raw_header = "From: test@example.com\nSubject: Test\n\n"
        data = json.dumps({"header": raw_header})

        header, flags, date = _extract_message_metadata(data)

        assert header["from"] == "test@example.com"
        assert header["subject"] == "Test"
        assert flags == []
        assert date is None

    def test_extract_metadata_old_format_plain_string(self):
        """Test extraction with old format (plain header string)."""
        raw_header = "From: test@example.com\nSubject: Test\n\n"

        header, flags, date = _extract_message_metadata(raw_header)

        assert header["from"] == "test@example.com"
        assert flags == []
        assert date is None

    def test_extract_metadata_empty_data(self):
        """Test extraction with empty data."""
        header, flags, date = _extract_message_metadata("")

        assert header == {}
        assert flags == []
        assert date is None

    def test_extract_metadata_invalid_json(self):
        """Test extraction with invalid JSON (treats as raw header)."""
        invalid_json = "From: test@example.com\nSubject: Test"

        header, flags, date = _extract_message_metadata(invalid_json)

        assert header["from"] == "test@example.com"
        assert flags == []
        assert date is None

    def test_extract_metadata_partial_fields(self):
        """Test extraction when some fields are missing."""
        data = json.dumps({
            "header": "Subject: Test\n\n",
            "flags": ["\\Seen"]
            # No internaldate
        })

        header, flags, date = _extract_message_metadata(data)

        assert header["subject"] == "Test"
        assert flags == ["\\Seen"]
        assert date is None


class TestFlagConditions:
    """Test flag-based condition evaluation."""

    def test_has_keyword_present(self):
        """Test has_keyword when keyword is present."""
        flags = ["\\Seen", "newsletter", "important"]
        condition = {"has_keyword": "newsletter"}
        assert _evaluate_flag_condition(flags, condition) is True

    def test_has_keyword_absent(self):
        """Test has_keyword when keyword is absent."""
        flags = ["\\Seen", "important"]
        condition = {"has_keyword": "newsletter"}
        assert _evaluate_flag_condition(flags, condition) is False

    def test_has_flag_present(self):
        """Test has_flag (alias for has_keyword)."""
        flags = ["\\Seen", "\\Flagged"]
        condition = {"has_flag": "\\Seen"}
        assert _evaluate_flag_condition(flags, condition) is True

    def test_lacks_keyword_absent(self):
        """Test lacks_keyword when keyword is absent (should be True)."""
        flags = ["\\Seen", "important"]
        condition = {"lacks_keyword": "newsletter"}
        assert _evaluate_flag_condition(flags, condition) is True

    def test_lacks_keyword_present(self):
        """Test lacks_keyword when keyword is present (should be False)."""
        flags = ["\\Seen", "newsletter"]
        condition = {"lacks_keyword": "newsletter"}
        assert _evaluate_flag_condition(flags, condition) is False

    def test_lacks_flag_absent(self):
        """Test lacks_flag (alias for lacks_keyword)."""
        flags = ["\\Seen"]
        condition = {"lacks_flag": "\\Flagged"}
        assert _evaluate_flag_condition(flags, condition) is True

    def test_empty_flags_list(self):
        """Test conditions with empty flags list."""
        flags = []
        assert _evaluate_flag_condition(flags, {"has_keyword": "test"}) is False
        assert _evaluate_flag_condition(flags, {"lacks_keyword": "test"}) is True

    def test_case_sensitivity(self):
        """Test that flag matching is case-sensitive."""
        flags = ["Newsletter"]
        # Exact match required
        assert _evaluate_flag_condition(flags, {"has_keyword": "Newsletter"}) is True
        assert _evaluate_flag_condition(flags, {"has_keyword": "newsletter"}) is False

    def test_no_flag_condition_returns_false(self):
        """Test that condition without flag keys returns False."""
        flags = ["\\Seen"]
        condition = {"header": "subject", "contains": "test"}
        assert _evaluate_flag_condition(flags, condition) is False


class TestAgeConditions:
    """Test age-based condition evaluation."""

    def test_age_days_gt_old_message(self):
        """Test age_days_gt with old message (should match)."""
        # Message from 400 days ago
        old_date = datetime.now(timezone.utc) - timedelta(days=400)
        condition = {"age_days_gt": 365}
        assert _evaluate_age_condition(old_date, condition) is True

    def test_age_days_gt_recent_message(self):
        """Test age_days_gt with recent message (should not match)."""
        # Message from 30 days ago
        recent_date = datetime.now(timezone.utc) - timedelta(days=30)
        condition = {"age_days_gt": 365}
        assert _evaluate_age_condition(recent_date, condition) is False

    def test_age_days_lt_recent_message(self):
        """Test age_days_lt with recent message (should match)."""
        # Message from 5 days ago
        recent_date = datetime.now(timezone.utc) - timedelta(days=5)
        condition = {"age_days_lt": 30}
        assert _evaluate_age_condition(recent_date, condition) is True

    def test_age_days_lt_old_message(self):
        """Test age_days_lt with old message (should not match)."""
        # Message from 100 days ago
        old_date = datetime.now(timezone.utc) - timedelta(days=100)
        condition = {"age_days_lt": 30}
        assert _evaluate_age_condition(old_date, condition) is False

    def test_age_days_eq_exact_match(self):
        """Test age_days_eq with exact age match."""
        # Message from exactly 30 days ago
        exact_date = datetime.now(timezone.utc) - timedelta(days=30)
        condition = {"age_days_eq": 30}
        assert _evaluate_age_condition(exact_date, condition) is True

    def test_age_days_eq_no_match(self):
        """Test age_days_eq with different age."""
        date = datetime.now(timezone.utc) - timedelta(days=31)
        condition = {"age_days_eq": 30}
        assert _evaluate_age_condition(date, condition) is False

    def test_age_condition_none_date(self):
        """Test age condition with None date (should return False)."""
        condition = {"age_days_gt": 365}
        assert _evaluate_age_condition(None, condition) is False

    def test_age_condition_timezone_naive(self):
        """Test age condition with timezone-naive datetime (assumes UTC)."""
        # Create timezone-naive datetime
        naive_date = datetime.now() - timedelta(days=100)
        condition = {"age_days_gt": 50}
        # Should still work by assuming UTC
        assert _evaluate_age_condition(naive_date, condition) is True

    def test_age_condition_invalid_threshold(self):
        """Test age condition with invalid threshold type."""
        date = datetime.now(timezone.utc) - timedelta(days=100)
        condition = {"age_days_gt": "invalid"}
        assert _evaluate_age_condition(date, condition) is False

    def test_age_condition_no_age_keys(self):
        """Test that condition without age keys returns False."""
        date = datetime.now(timezone.utc) - timedelta(days=100)
        condition = {"header": "subject", "contains": "test"}
        assert _evaluate_age_condition(date, condition) is False


class TestConditionNodeIntegration:
    """Test integration of flag and age conditions with condition node evaluation."""

    def test_evaluate_node_with_flag_condition(self):
        """Test evaluating a node with flag condition."""
        header = {"subject": "Test"}
        flags = ["newsletter"]
        node = {"has_keyword": "newsletter"}

        assert _evaluate_condition_node(header, node, flags=flags) is True
        assert _evaluate_condition_node(header, node, flags=[]) is False

    def test_evaluate_node_with_age_condition(self):
        """Test evaluating a node with age condition."""
        header = {"subject": "Test"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)
        node = {"age_days_gt": 365}

        assert _evaluate_condition_node(header, node, date=old_date) is True
        assert _evaluate_condition_node(header, node, date=None) is False

    def test_evaluate_node_combined_conditions(self):
        """Test node with header, flag, and age conditions."""
        header = {"subject": "Newsletter", "from": "news@example.com"}
        flags = ["newsletter"]
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        node = {
            "all": [
                {"header": "subject", "contains": "Newsletter"},
                {"has_keyword": "newsletter"},
                {"age_days_gt": 365}
            ]
        }

        # All conditions match
        assert _evaluate_condition_node(header, node, flags=flags, date=old_date) is True

        # One condition fails (no flag)
        assert _evaluate_condition_node(header, node, flags=[], date=old_date) is False

    def test_evaluate_node_any_with_flags(self):
        """Test 'any' operator with flag conditions."""
        header = {"subject": "Test"}
        flags = ["important"]

        node = {
            "any": [
                {"has_keyword": "newsletter"},
                {"has_keyword": "important"}
            ]
        }

        # One matches
        assert _evaluate_condition_node(header, node, flags=flags) is True

        # Neither matches
        assert _evaluate_condition_node(header, node, flags=["other"]) is False

    def test_evaluate_node_backward_compatibility(self):
        """Test that nodes without flags/date still work (backward compatibility)."""
        header = {"subject": "Test"}
        node = {"header": "subject", "contains": "Test"}

        # Works without flags and date
        assert _evaluate_condition_node(header, node) is True

        # Also works with None values explicitly passed
        assert _evaluate_condition_node(header, node, flags=None, date=None) is True


class TestConditionsMatchFunction:
    """Test the conditions_match wrapper function."""

    def test_conditions_match_with_flags(self):
        """Test conditions_match with flag conditions."""
        header = {"subject": "Newsletter"}
        flags = ["newsletter"]
        conditions = {
            "all": [
                {"header": "subject", "contains": "Newsletter"},
                {"has_keyword": "newsletter"}
            ]
        }

        assert conditions_match(header, conditions, flags=flags) is True
        assert conditions_match(header, conditions, flags=[]) is False

    def test_conditions_match_with_age(self):
        """Test conditions_match with age conditions."""
        header = {"subject": "Old email"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)
        recent_date = datetime.now(timezone.utc) - timedelta(days=30)
        conditions = {"age_days_gt": 365}

        assert conditions_match(header, conditions, date=old_date) is True
        assert conditions_match(header, conditions, date=recent_date) is False

    def test_conditions_match_backward_compatibility(self):
        """Test conditions_match without flags/date (backward compatibility)."""
        header = {"subject": "Test", "from": "test@example.com"}
        conditions = {"header": "subject", "contains": "Test"}

        # Old signature still works
        assert conditions_match(header, conditions) is True


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_flag_condition_with_none_flags(self):
        """Test that flag conditions fail gracefully when flags is None."""
        header = {"subject": "Test"}
        node = {"has_keyword": "test"}

        # Should not match when flags is None (not just empty list)
        result = _evaluate_condition_node(header, node, flags=None)
        assert result is False

    def test_age_condition_with_none_date(self):
        """Test that age conditions fail gracefully when date is None."""
        header = {"subject": "Test"}
        node = {"age_days_gt": 30}

        result = _evaluate_condition_node(header, node, date=None)
        assert result is False

    def test_complex_nested_conditions(self):
        """Test deeply nested conditions with flags and age."""
        header = {"subject": "Important Newsletter"}
        flags = ["newsletter", "important"]
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        conditions = {
            "all": [
                {
                    "any": [
                        {"has_keyword": "newsletter"},
                        {"has_keyword": "spam"}
                    ]
                },
                {
                    "all": [
                        {"header": "subject", "contains": "Important"},
                        {"age_days_gt": 365}
                    ]
                }
            ]
        }

        assert conditions_match(header, conditions, flags=flags, date=old_date) is True

    def test_empty_conditions(self):
        """Test that empty conditions return False."""
        header = {"subject": "Test"}
        assert conditions_match(header, None) is False
        assert conditions_match(header, {}) is False
        assert conditions_match(header, []) is False


class TestRealWorldScenarios:
    """Test real-world rule scenarios."""

    def test_archive_old_newsletters(self):
        """Test rule to archive old newsletter emails."""
        header = {"subject": "Monthly Newsletter"}
        flags = ["newsletter"]
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        rule_conditions = {
            "all": [
                {"has_keyword": "newsletter"},
                {"age_days_gt": 365}
            ]
        }

        assert conditions_match(header, rule_conditions, flags=flags, date=old_date) is True

        # Recent newsletter should not match
        recent_date = datetime.now(timezone.utc) - timedelta(days=30)
        assert conditions_match(header, rule_conditions, flags=flags, date=recent_date) is False

    def test_move_unread_important(self):
        """Test rule to move unread important emails."""
        header = {"subject": "Important Update"}
        flags = ["\\Flagged"]  # Has flagged, lacks \Seen

        rule_conditions = {
            "all": [
                {"has_keyword": "\\Flagged"},
                {"lacks_keyword": "\\Seen"}
            ]
        }

        assert conditions_match(header, rule_conditions, flags=flags) is True

        # Read important email should not match
        read_flags = ["\\Flagged", "\\Seen"]
        assert conditions_match(header, rule_conditions, flags=read_flags) is False

    def test_delete_old_spam(self):
        """Test rule to delete old spam emails."""
        header = {"subject": "Special Offer!!!"}
        flags = ["\\Seen", "Junk"]
        old_date = datetime.now(timezone.utc) - timedelta(days=100)

        rule_conditions = {
            "all": [
                {"has_keyword": "Junk"},
                {"age_days_gt": 90}
            ]
        }

        assert conditions_match(header, rule_conditions, flags=flags, date=old_date) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
