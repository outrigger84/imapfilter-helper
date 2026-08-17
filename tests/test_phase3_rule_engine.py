"""
Test Rule Engine Extensions: Age-Based Conditions and Age-Gated Actions
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.rule_engine import (
    _evaluate_age_condition,
    _evaluate_condition_node,
    _extract_message_metadata,
    _parse_internaldate,
    all_possible_move_targets,
    conditions_match,
    expand_actions_for_age,
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

    def test_age_condition_explicit_now(self):
        """Test that an explicit `now` is honored over the wall clock."""
        anchor = datetime(2030, 1, 1, tzinfo=timezone.utc)
        msg_date = anchor - timedelta(days=400)
        condition = {"age_days_gt": 365}
        assert _evaluate_age_condition(msg_date, condition, now=anchor) is True
        assert _evaluate_age_condition(msg_date, condition, now=anchor - timedelta(days=400)) is False


class TestConditionNodeIntegration:
    """Test integration of age conditions with condition node evaluation."""

    def test_evaluate_node_with_age_condition(self):
        """Test evaluating a node with age condition."""
        header = {"subject": "Test"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)
        node = {"age_days_gt": 365}

        assert _evaluate_condition_node(header, node, date=old_date) is True
        assert _evaluate_condition_node(header, node, date=None) is False

    def test_evaluate_node_combined_conditions(self):
        """Test node with header and age conditions."""
        header = {"subject": "Newsletter", "from": "news@example.com"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        node = {
            "all": [
                {"header": "subject", "contains": "Newsletter"},
                {"age_days_gt": 365}
            ]
        }

        # All conditions match
        assert _evaluate_condition_node(header, node, date=old_date) is True

        # Age condition fails
        recent_date = datetime.now(timezone.utc) - timedelta(days=30)
        assert _evaluate_condition_node(header, node, date=recent_date) is False

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

    def test_age_condition_with_none_date(self):
        """Test that age conditions fail gracefully when date is None."""
        header = {"subject": "Test"}
        node = {"age_days_gt": 30}

        result = _evaluate_condition_node(header, node, date=None)
        assert result is False

    def test_complex_nested_conditions(self):
        """Test deeply nested conditions with age."""
        header = {"subject": "Important Newsletter"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        conditions = {
            "all": [
                {
                    "any": [
                        {"header": "subject", "contains": "Newsletter"},
                        {"header": "subject", "contains": "Spam"}
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

        assert conditions_match(header, conditions, date=old_date) is True

    def test_empty_conditions(self):
        """Test that empty conditions return False."""
        header = {"subject": "Test"}
        assert conditions_match(header, None) is False
        assert conditions_match(header, {}) is False
        assert conditions_match(header, []) is False


class TestRealWorldScenarios:
    """Test real-world rule scenarios."""

    def test_archive_old_newsletters(self):
        """Test rule to archive old newsletter emails, gated purely on age."""
        header = {"subject": "Monthly Newsletter"}
        old_date = datetime.now(timezone.utc) - timedelta(days=400)

        rule_conditions = {
            "all": [
                {"header": "subject", "contains": "Newsletter"},
                {"age_days_gt": 365}
            ]
        }

        assert conditions_match(header, rule_conditions, date=old_date) is True

        # Recent newsletter should not match
        recent_date = datetime.now(timezone.utc) - timedelta(days=30)
        assert conditions_match(header, rule_conditions, date=recent_date) is False


class TestExpandActionsForAge:
    """Test expand_actions_for_age: the bracket-expansion helper backing
    age-gated actions."""

    NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_plain_actions_unaffected(self):
        """A rule with no brackets is returned unchanged, regardless of age."""
        actions = [{"type": "move", "target": "X"}]
        expanded, no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=5), self.NOW)
        assert expanded == actions
        assert no_match is False

    def test_plain_actions_with_no_date(self):
        """A pure-plain rule still matches even with an unknown date."""
        actions = [{"type": "move", "target": "X"}]
        expanded, no_match = expand_actions_for_age(actions, None, self.NOW)
        assert expanded == actions
        assert no_match is False

    def test_empty_actions_list_is_a_no_op_match(self):
        """A rule with a genuinely empty actions list stays a silent no-op
        match (today's behavior), not a fall-through."""
        expanded, no_match = expand_actions_for_age([], self.NOW - timedelta(days=5), self.NOW)
        assert expanded == []
        assert no_match is False

    def test_single_bracket_fires(self):
        actions = [{"age_days_gt": 30, "do": [{"type": "move", "target": "Old"}]}]
        expanded, no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=40), self.NOW)
        assert expanded == [{"type": "move", "target": "Old"}]
        assert no_match is False

    def test_single_bracket_does_not_fire_is_coverage_gap(self):
        actions = [{"age_days_gt": 30, "do": [{"type": "move", "target": "Old"}]}]
        expanded, no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=5), self.NOW)
        assert expanded == []
        assert no_match is True

    def test_two_bracket_partition_first_wins(self):
        """The 2FA pattern: young -> no-op, old -> move. Exactly one bracket
        fires per message."""
        actions = [
            {"age_days_lt": 1, "do": []},
            {"age_days_gte": 1, "do": [{"type": "move", "target": "Deleted Messages"}]},
        ]
        young, young_no_match = expand_actions_for_age(actions, self.NOW - timedelta(hours=2), self.NOW)
        assert young == []
        assert young_no_match is False  # matched, deliberate no-op

        old, old_no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=3), self.NOW)
        assert old == [{"type": "move", "target": "Deleted Messages"}]
        assert old_no_match is False

    def test_bounded_band_gte_and_lt_anded(self):
        """A bracket combining a lower and upper bound requires both."""
        actions = [{"age_days_gte": 30, "age_days_lt": 90, "do": [{"type": "move", "target": "Mid"}]}]

        below = expand_actions_for_age(actions, self.NOW - timedelta(days=10), self.NOW)
        assert below == ([], True)

        within = expand_actions_for_age(actions, self.NOW - timedelta(days=60), self.NOW)
        assert within == ([{"type": "move", "target": "Mid"}], False)

        above = expand_actions_for_age(actions, self.NOW - timedelta(days=120), self.NOW)
        assert above == ([], True)

    def test_overlapping_brackets_first_in_list_order_wins(self):
        """Two brackets that could both match: only the first (list order)
        contributes its actions."""
        actions = [
            {"age_days_gt": 0, "do": [{"type": "move", "target": "A"}]},
            {"age_days_gt": 0, "do": [{"type": "move", "target": "B"}]},
        ]
        expanded, no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=5), self.NOW)
        assert expanded == [{"type": "move", "target": "A"}]
        assert no_match is False

    def test_plain_and_bracket_mixed_plain_always_included(self):
        actions = [
            {"type": "move", "target": "Always"},
            {"age_days_gt": 1000, "do": [{"type": "move", "target": "Never"}]},
        ]
        expanded, no_match = expand_actions_for_age(actions, self.NOW - timedelta(days=5), self.NOW)
        assert expanded == [{"type": "move", "target": "Always"}]
        assert no_match is False  # plain item present -> always matched

    def test_unknown_date_never_fires_a_bracket(self):
        actions = [{"age_days_gt": 1, "do": [{"type": "move", "target": "X"}]}]
        expanded, no_match = expand_actions_for_age(actions, None, self.NOW)
        assert expanded == []
        assert no_match is True


class TestAllPossibleMoveTargets:
    """Test all_possible_move_targets: the age-agnostic reporting helper."""

    def test_plain_actions(self):
        actions = [{"type": "move", "target": "A"}, {"type": "move", "target": "B"}]
        assert all_possible_move_targets(actions) == {"A", "B"}

    def test_sees_through_brackets(self):
        actions = [
            {"age_days_lte": 365, "do": [{"type": "move", "target": "A"}]},
            {"age_days_gt": 365, "do": [{"type": "move", "target": "B"}]},
        ]
        assert all_possible_move_targets(actions) == {"A", "B"}

    def test_ignores_non_move_actions(self):
        actions = [{"type": "noop"}, {"age_days_gt": 1, "do": [{"type": "noop"}]}]
        assert all_possible_move_targets(actions) == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
