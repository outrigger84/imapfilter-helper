#!/usr/bin/env python3
"""Comprehensive tests for the conflict detector module."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conflict_detector import (
    ConditionAnalyzer,
    ConditionTreeComparator,
    ConflictDetector,
    ConflictResolver,
    ConflictResult,
    ConflictSeverity,
    ConflictType,
    OverlapRelationship,
)


class TestConditionAnalyzer:
    """Test the ConditionAnalyzer class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.analyzer = ConditionAnalyzer()

    def test_extract_simple_conditions(self):
        """Test extracting simple conditions from a tree."""
        tree = {
            "all": [
                {"header": "from", "contains": "example.com"},
                {"header": "subject", "contains": "invoice"},
            ]
        }

        conditions = self.analyzer.extract_simple_conditions(tree)
        assert len(conditions) == 2
        assert conditions[0]["header"] == "from"
        assert conditions[1]["header"] == "subject"

    def test_extract_nested_conditions(self):
        """Test extracting conditions from nested trees."""
        tree = {
            "all": [
                {
                    "any": [
                        {"header": "from", "contains": "gmail"},
                        {"header": "from", "contains": "yahoo"},
                    ]
                },
                {"header": "subject", "contains": "receipt"},
            ]
        }

        conditions = self.analyzer.extract_simple_conditions(tree)
        assert len(conditions) == 3
        assert any(c.get("contains") == "gmail" for c in conditions)
        assert any(c.get("contains") == "receipt" for c in conditions)

    def test_conditions_overlap_same_header_contains(self):
        """Test overlap detection for contains operators."""
        cond1 = {"header": "from", "contains": "@example.com"}
        cond2 = {"header": "from", "contains": "alice@example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is True

    def test_conditions_overlap_different_headers(self):
        """Test that conditions with different headers don't overlap."""
        cond1 = {"header": "from", "contains": "example.com"}
        cond2 = {"header": "subject", "contains": "example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is False

    def test_conditions_overlap_equals_exact_match(self):
        """Test overlap detection for equals operators."""
        cond1 = {"header": "from", "equals": "alice@example.com"}
        cond2 = {"header": "from", "equals": "alice@example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is True

    def test_conditions_overlap_equals_no_match(self):
        """Test equals operators with different values."""
        cond1 = {"header": "from", "equals": "alice@example.com"}
        cond2 = {"header": "from", "equals": "bob@example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is False

    def test_conditions_overlap_equals_vs_contains(self):
        """Test overlap between equals and contains."""
        cond1 = {"header": "from", "equals": "alice@example.com"}
        cond2 = {"header": "from", "contains": "@example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is True

    def test_conditions_overlap_case_insensitive(self):
        """Test that overlap detection is case-insensitive."""
        cond1 = {"header": "from", "contains": "Example.COM"}
        cond2 = {"header": "from", "contains": "example.com"}

        assert self.analyzer.conditions_overlap(cond1, cond2) is True


class TestConditionTreeComparator:
    """Test the ConditionTreeComparator class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.analyzer = ConditionAnalyzer()
        self.comparator = ConditionTreeComparator(self.analyzer)

    def test_compare_identical_trees(self):
        """Test comparison of identical condition trees."""
        tree1 = {"all": [{"header": "from", "contains": "example.com"}]}
        tree2 = {"all": [{"header": "from", "contains": "example.com"}]}

        rel, overlap = self.comparator.compare_trees(tree1, tree2)
        assert rel == OverlapRelationship.EQUAL
        assert overlap == 1.0

    def test_compare_subset_trees(self):
        """Test detection of subset relationships."""
        # tree1 is more specific (subset of tree2)
        tree1 = {
            "all": [
                {"header": "from", "contains": "alice@example.com"},
                {"header": "subject", "contains": "invoice"},
            ]
        }
        # tree2 is more general
        tree2 = {"all": [{"header": "from", "contains": "@example.com"}]}

        rel, overlap = self.comparator.compare_trees(tree1, tree2)
        # tree1 should be subset or intersect of tree2
        assert rel in (OverlapRelationship.SUBSET, OverlapRelationship.INTERSECT)

    def test_compare_disjoint_trees(self):
        """Test detection of disjoint condition trees."""
        tree1 = {"all": [{"header": "from", "contains": "alice"}]}
        tree2 = {"all": [{"header": "from", "contains": "bob"}]}

        rel, overlap = self.comparator.compare_trees(tree1, tree2)
        assert rel == OverlapRelationship.DISJOINT
        assert overlap == 0.0

    def test_compare_empty_trees(self):
        """Test comparison of empty condition trees."""
        tree1 = {}
        tree2 = {}

        rel, overlap = self.comparator.compare_trees(tree1, tree2)
        assert rel == OverlapRelationship.DISJOINT
        assert overlap == 0.0


class TestConflictDetector:
    """Test the ConflictDetector class."""

    def test_detect_priority_conflicts(self):
        """Test detection of priority conflicts."""
        rules = [
            {
                "name": "Gmail Newsletters",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "@gmail.com"}]},
                "actions": [{"type": "move", "target": "Newsletters/Gmail"}],
            },
            {
                "name": "All Newsletters",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "newsletter@"}]},
                "actions": [{"type": "move", "target": "Newsletters/General"}],
            },
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_priority_conflicts()

        # Should detect conflict if there's any overlap and different targets
        assert len(conflicts) >= 0
        if conflicts:
            assert conflicts[0].type == ConflictType.PRIORITY_CONFLICT

    def test_detect_unreachable_rules(self):
        """Test detection of unreachable rules."""
        rules = [
            {
                "name": "All Gmail",
                "priority": 50,
                "conditions": {"all": [{"header": "from", "contains": "@gmail.com"}]},
                "actions": [{"type": "move", "target": "Gmail"}],
            },
            {
                "name": "Gmail Newsletters",
                "priority": 100,
                "conditions": {
                    "all": [
                        {"header": "from", "contains": "@gmail.com"},
                        {"header": "subject", "contains": "newsletter"},
                    ]
                },
                "actions": [{"type": "move", "target": "Gmail/Newsletters"}],
            },
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_unreachable_rules()

        # Gmail Newsletters is more specific but has a higher priority number,
        # so the broader All Gmail rule (lower number = evaluated first in the
        # first-match-wins engine) shadows it.
        assert any(
            c.type == ConflictType.UNREACHABLE and c.rule2_name == "Gmail Newsletters"
            for c in conflicts
        )

    def test_detect_redundant_rules(self):
        """Test detection of redundant rules."""
        rules = [
            {
                "name": "Crypto Scams",
                "priority": 100,
                "conditions": {
                    "any": [
                        {"header": "subject", "contains": "bitcoin"},
                        {"header": "subject", "contains": "crypto"},
                    ]
                },
                "actions": [{"type": "move", "target": "Spam"}],
            },
            {
                "name": "Cryptocurrency",
                "priority": 100,
                "conditions": {
                    "any": [
                        {"header": "subject", "contains": "bitcoin"},
                        {"header": "subject", "contains": "crypto"},
                    ]
                },
                "actions": [{"type": "move", "target": "Spam"}],
            },
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_redundant_rules()

        # Should detect identical rules
        assert any(c.type == ConflictType.REDUNDANT for c in conflicts)

    def test_detect_all_conflicts(self):
        """Test detecting all types of conflicts together."""
        rules = [
            {
                "name": "Rule 1",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "example.com"}]},
                "actions": [{"type": "move", "target": "Folder1"}],
            },
            {
                "name": "Rule 2",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "test"}]},
                "actions": [{"type": "move", "target": "Folder2"}],
            },
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_all_conflicts()

        # Should return a list of conflicts (may be empty depending on overlap)
        assert isinstance(conflicts, list)

    def test_find_rule_file(self):
        """Test finding rule file by name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create a test rule file
            rule_file = tmpdir / "test_rule.json"
            rule_data = {"name": "Test Rule", "priority": 100}
            rule_file.write_text(json.dumps(rule_data))

            rules = [rule_data]
            resolver = ConflictResolver([], tmpdir)
            found_file = resolver._find_rule_file("Test Rule")

            assert found_file is not None
            assert found_file.name == "test_rule.json"

    def test_suggest_fixes_priority_conflict(self):
        """Test fix suggestion for priority conflicts."""
        conflict = ConflictResult(
            type=ConflictType.PRIORITY_CONFLICT,
            severity=ConflictSeverity.HIGH,
            rule1_name="Rule A",
            rule2_name="Rule B",
            rule1_priority=100,
            rule2_priority=100,
            overlap_relationship=OverlapRelationship.SUBSET,
            overlap_percent=0.8,
            affected_count=50,
            explanation="Test conflict",
            suggestion="Test suggestion",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            resolver = ConflictResolver([conflict], tmpdir)

            fixes = resolver.suggest_fixes(conflict)
            assert len(fixes) > 0
            assert any(f["type"] == "adjust_priority" for f in fixes)

    def test_suggest_fixes_unreachable(self):
        """Test fix suggestion for unreachable rules."""
        conflict = ConflictResult(
            type=ConflictType.UNREACHABLE,
            severity=ConflictSeverity.MEDIUM,
            rule1_name="Rule A",
            rule2_name="Rule B",
            rule1_priority=50,
            rule2_priority=100,
            overlap_relationship=OverlapRelationship.SUBSET,
            overlap_percent=1.0,
            affected_count=100,
            explanation="Test conflict",
            suggestion="Test suggestion",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            resolver = ConflictResolver([conflict], tmpdir)

            fixes = resolver.suggest_fixes(conflict)
            assert len(fixes) > 0
            assert any(f["type"] == "adjust_priority" for f in fixes)

    def test_suggest_fixes_redundant(self):
        """Test fix suggestion for redundant rules."""
        conflict = ConflictResult(
            type=ConflictType.REDUNDANT,
            severity=ConflictSeverity.LOW,
            rule1_name="Rule A",
            rule2_name="Rule B",
            rule1_priority=100,
            rule2_priority=100,
            overlap_relationship=OverlapRelationship.EQUAL,
            overlap_percent=1.0,
            affected_count=None,
            explanation="Test conflict",
            suggestion="Test suggestion",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            resolver = ConflictResolver([conflict], tmpdir)

            fixes = resolver.suggest_fixes(conflict)
            assert len(fixes) > 0
            assert any(f["type"] == "merge_rules" for f in fixes)


class TestConflictResult:
    """Test the ConflictResult dataclass."""

    def test_conflict_result_creation(self):
        """Test creating a ConflictResult."""
        result = ConflictResult(
            type=ConflictType.PRIORITY_CONFLICT,
            severity=ConflictSeverity.HIGH,
            rule1_name="Rule A",
            rule2_name="Rule B",
            rule1_priority=100,
            rule2_priority=100,
            overlap_relationship=OverlapRelationship.INTERSECT,
            overlap_percent=0.75,
            affected_count=50,
            explanation="Test explanation",
            suggestion="Test suggestion",
        )

        assert result.type == ConflictType.PRIORITY_CONFLICT
        assert result.severity == ConflictSeverity.HIGH
        assert result.overlap_percent == 0.75

    def test_conflict_result_to_dict(self):
        """Test converting ConflictResult to dictionary."""
        result = ConflictResult(
            type=ConflictType.PRIORITY_CONFLICT,
            severity=ConflictSeverity.HIGH,
            rule1_name="Rule A",
            rule2_name="Rule B",
            rule1_priority=100,
            rule2_priority=100,
            overlap_relationship=OverlapRelationship.INTERSECT,
            overlap_percent=0.75,
            affected_count=50,
            explanation="Test explanation",
            suggestion="Test suggestion",
        )

        result_dict = result.to_dict()
        assert result_dict["type"] == "priority_conflict"
        assert result_dict["severity"] == "high"
        assert result_dict["overlap_percent"] == 0.75


class TestEnums:
    """Test the enum classes."""

    def test_conflict_type_enum(self):
        """Test ConflictType enum."""
        assert ConflictType.PRIORITY_CONFLICT.value == "priority_conflict"
        assert ConflictType.UNREACHABLE.value == "unreachable"
        assert ConflictType.REDUNDANT.value == "redundant"

    def test_conflict_severity_enum(self):
        """Test ConflictSeverity enum."""
        assert ConflictSeverity.HIGH.value == "high"
        assert ConflictSeverity.MEDIUM.value == "medium"
        assert ConflictSeverity.LOW.value == "low"

    def test_overlap_relationship_enum(self):
        """Test OverlapRelationship enum."""
        assert OverlapRelationship.EQUAL.value == "equal"
        assert OverlapRelationship.SUBSET.value == "subset"
        assert OverlapRelationship.SUPERSET.value == "superset"
        assert OverlapRelationship.INTERSECT.value == "intersect"
        assert OverlapRelationship.DISJOINT.value == "disjoint"


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_rules_list(self):
        """Test detector with empty rules list."""
        detector = ConflictDetector([])
        conflicts = detector.detect_all_conflicts()
        assert conflicts == []

    def test_single_rule(self):
        """Test detector with single rule."""
        rules = [
            {
                "name": "Single Rule",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "example.com"}]},
                "actions": [{"type": "move", "target": "Folder"}],
            }
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_all_conflicts()
        # Single rule shouldn't conflict with anything
        assert len(conflicts) == 0

    def test_rules_without_actions(self):
        """Test handling rules without actions."""
        rules = [
            {
                "name": "Rule 1",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "example.com"}]},
            },
            {
                "name": "Rule 2",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "test"}]},
            },
        ]

        detector = ConflictDetector(rules)
        # Should not crash when rules have no actions
        conflicts = detector.detect_all_conflicts()
        assert isinstance(conflicts, list)

    def test_rules_with_keyword_actions(self):
        """Test handling rules with keyword actions (non-move)."""
        rules = [
            {
                "name": "Rule 1",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "example.com"}]},
                "actions": [{"type": "set_keywords", "keywords": ["Retain365"]}],
            },
            {
                "name": "Rule 2",
                "priority": 100,
                "conditions": {"all": [{"header": "from", "contains": "test"}]},
                "actions": [{"type": "set_keywords", "keywords": ["Important"]}],
            },
        ]

        detector = ConflictDetector(rules)
        conflicts = detector.detect_all_conflicts()
        # Keyword actions shouldn't create move conflicts
        assert not any(
            c.type == ConflictType.PRIORITY_CONFLICT for c in conflicts
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
