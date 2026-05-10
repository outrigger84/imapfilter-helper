#!/usr/bin/env python3
"""Rule conflict detection and resolution for IMAPFilter.

This module analyzes rules for:
- Priority conflicts (same priority + overlapping conditions + different targets)
- Unreachable rules (shadowed by higher priority rules)
- Redundant rules (duplicate or highly similar conditions)
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from core.logging_utils import JsonLogger
from core.rule_engine import conditions_match


class ConflictType(Enum):
    """Types of rule conflicts."""

    PRIORITY_CONFLICT = "priority_conflict"
    UNREACHABLE = "unreachable"
    REDUNDANT = "redundant"
    OVERLAPPING_TARGETS = "overlapping_targets"


class ConflictSeverity(Enum):
    """Severity levels for conflicts."""

    HIGH = "high"  # Definitely causes issues
    MEDIUM = "medium"  # Likely causes issues
    LOW = "low"  # Potential optimization


class OverlapRelationship(Enum):
    """Relationship between two condition sets."""

    EQUAL = "equal"  # Conditions are identical
    SUBSET = "subset"  # Rule1 ⊂ Rule2
    SUPERSET = "superset"  # Rule1 ⊃ Rule2
    INTERSECT = "intersect"  # Partial overlap
    DISJOINT = "disjoint"  # No overlap


@dataclass
class ConflictResult:
    """Result of conflict detection between two rules."""

    type: ConflictType
    severity: ConflictSeverity
    rule1_name: str
    rule2_name: str
    rule1_priority: int
    rule2_priority: int
    overlap_relationship: OverlapRelationship
    overlap_percent: float  # 0.0-1.0
    affected_count: Optional[int]  # From cache if available
    explanation: str
    suggestion: str
    reason: str = ""  # Why this conflict was detected

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "rule1_name": self.rule1_name,
            "rule2_name": self.rule2_name,
            "rule1_priority": self.rule1_priority,
            "rule2_priority": self.rule2_priority,
            "overlap_relationship": self.overlap_relationship.value,
            "overlap_percent": round(self.overlap_percent, 3),
            "affected_count": self.affected_count,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "reason": self.reason,
        }


class ConditionAnalyzer:
    """Analyzes individual conditions and condition trees."""

    def __init__(self) -> None:
        """Initialize the analyzer."""
        self.regex_cache: dict[str, re.Pattern] = {}

    def extract_simple_conditions(
        self, node: Any, conditions: Optional[list[dict]] = None
    ) -> list[dict]:
        """Extract all leaf conditions from a tree.

        Args:
            node: Condition node (can be dict or list)
            conditions: Accumulator list

        Returns:
            List of simple condition dictionaries
        """
        if conditions is None:
            conditions = []

        if not isinstance(node, dict):
            return conditions

        # If this is a simple condition (has operator like 'contains', 'equals')
        if any(op in node for op in ("contains", "equals", "regex", "not_contains", "not_equals", "not_regex", "has_keyword", "lacks_keyword", "age_days_gt", "age_days_lt", "age_days_eq")):
            conditions.append(node)
            return conditions

        # If this is a logical group (has 'all', 'any', or 'not')
        if "all" in node:
            for child in node["all"]:
                self.extract_simple_conditions(child, conditions)
        elif "any" in node:
            for child in node["any"]:
                self.extract_simple_conditions(child, conditions)
        elif "not" in node:
            self.extract_simple_conditions(node["not"], conditions)

        return conditions

    def conditions_overlap(self, cond1: dict, cond2: dict) -> bool:
        """Check if two simple conditions can match the same message.

        Args:
            cond1: First condition
            cond2: Second condition

        Returns:
            True if overlap possible, False if definitely disjoint
        """
        # Extract header fields
        header1 = cond1.get("header", "").lower()
        header2 = cond2.get("header", "").lower()

        # Different headers → disjoint
        if header1 != header2:
            return False

        # Extract operators and values
        ops1 = self._detect_operators(cond1)
        ops2 = self._detect_operators(cond2)

        # Check if any operator pair overlaps
        for op1, val1 in ops1:
            for op2, val2 in ops2:
                if self._operators_overlap(op1, val1, op2, val2):
                    return True

        return False

    def _detect_operators(self, cond: dict) -> list[tuple[str, str]]:
        """Extract operators and values from a condition.

        Args:
            cond: Condition dictionary

        Returns:
            List of (operator, value) tuples
        """
        operators = []

        operator_keys = (
            "contains",
            "equals",
            "regex",
            "not_contains",
            "not_equals",
            "not_regex",
        )
        for op in operator_keys:
            if op in cond:
                operators.append((op, cond[op]))

        return operators

    def _operators_overlap(self, op1: str, val1: str, op2: str, val2: str) -> bool:
        """Check if two operators with values can overlap.

        This uses a more careful approach to negations: conditions with different
        values are assumed to be disjoint unless proven otherwise.

        Args:
            op1: First operator
            val1: First value
            op2: Second operator
            val2: Second value

        Returns:
            True if overlap possible
        """
        val1_lower = str(val1).lower()
        val2_lower = str(val2).lower()

        # Both contains
        if op1 == "contains" and op2 == "contains":
            # Check substring relationships
            return val1_lower in val2_lower or val2_lower in val1_lower

        # Both equals
        if op1 == "equals" and op2 == "equals":
            return val1_lower == val2_lower

        # Equals vs contains
        if op1 == "equals" and op2 == "contains":
            return val2_lower in val1_lower

        if op1 == "contains" and op2 == "equals":
            return val1_lower in val2_lower

        # Not_contains vs not_contains
        if op1 == "not_contains" and op2 == "not_contains":
            # Two negations can overlap unless they're for the same value
            return val1_lower != val2_lower

        # Contains vs not_contains (or vice versa)
        if (op1 == "contains" and op2 == "not_contains") or (op1 == "not_contains" and op2 == "contains"):
            # A message can contain X and simultaneously not contain Y (if X != Y)
            # Only contradictory if they're the exact same value
            # e.g., "contains @aarmy.com" and "not_contains @openrent.co.uk" -> CAN both be true
            # e.g., "contains @example.com" and "not_contains @example.com" -> CANNOT both be true
            return val1_lower != val2_lower  # Only contradiction if values are identical

        # Regex patterns - conservative (assume overlap unless proven disjoint)
        if op1 == "regex" or op2 == "regex":
            return True  # Conservative approach - regex is hard to analyze

        # If one is negation and the other isn't, be conservative
        if op1.startswith("not_") or op2.startswith("not_"):
            # Only assume overlap if both are negations or if the non-negation is very general
            return val1_lower == val2_lower or val1_lower in val2_lower or val2_lower in val1_lower

        # Flag/age conditions - can overlap if not contradictory
        if op1 in ("has_keyword", "lacks_keyword"):
            # Same keyword operation might not overlap
            return val1_lower != val2_lower or op1 == op2

        if op1 in ("age_days_gt", "age_days_lt", "age_days_eq"):
            # Age conditions can overlap unless they're strict contradictions
            return True  # Conservative approach

        return False  # Default to disjoint for unknown combinations


class ConditionTreeComparator:
    """Compares entire condition trees between rules."""

    def __init__(self, analyzer: ConditionAnalyzer) -> None:
        """Initialize comparator.

        Args:
            analyzer: ConditionAnalyzer instance to use
        """
        self.analyzer = analyzer

    def compare_trees(
        self, tree1: Any, tree2: Any
    ) -> tuple[OverlapRelationship, float]:
        """Compare two condition trees.

        NOTE: This uses a conservative approach. When trees have different AND/OR
        structures, they are treated as potentially disjoint unless proven otherwise.

        Args:
            tree1: First condition tree
            tree2: Second condition tree

        Returns:
            Tuple of (relationship, overlap_percent)
        """
        # Extract simple conditions from both trees
        conds1 = self.analyzer.extract_simple_conditions(tree1)
        conds2 = self.analyzer.extract_simple_conditions(tree2)

        if not conds1 or not conds2:
            return (OverlapRelationship.DISJOINT, 0.0)

        # Check if trees have same AND/OR structure
        # If they differ in structure (e.g., "all" vs "any"), be conservative
        tree1_logic = self._get_tree_logic(tree1)
        tree2_logic = self._get_tree_logic(tree2)

        # For now, use simple comparison: check if conditions are identical
        # This is a simplified approach; full DNF would be more complex

        if self._conditions_equal(conds1, conds2):
            return (OverlapRelationship.EQUAL, 1.0)

        if self._is_subset(conds1, conds2):
            overlap = len([c for c in conds1 if any(self.analyzer.conditions_overlap(c, c2) for c2 in conds2)]) / max(len(conds1), 1)
            return (OverlapRelationship.SUBSET, overlap)

        if self._is_subset(conds2, conds1):
            overlap = len([c for c in conds2 if any(self.analyzer.conditions_overlap(c, c1) for c1 in conds1)]) / max(len(conds2), 1)
            return (OverlapRelationship.SUPERSET, overlap)

        # If trees have very different structures, be conservative and assume disjoint
        if tree1_logic != tree2_logic:
            # Different AND/OR structure - very hard to prove overlap without actual evaluation
            # Be conservative: only flag as overlapping if ALL conditions match exactly
            if all(any(self.analyzer.conditions_overlap(c1, c2) for c2 in conds2) for c1 in conds1):
                # Only return INTERSECT if literally all conditions can overlap
                # This is very conservative
                overlap_percent = 1.0
                return (OverlapRelationship.INTERSECT, overlap_percent)
            else:
                return (OverlapRelationship.DISJOINT, 0.0)

        # Check for partial overlap (only if same structure)
        overlap_count = sum(1 for c1 in conds1 if any(self.analyzer.conditions_overlap(c1, c2) for c2 in conds2))
        if overlap_count > 0:
            overlap_percent = overlap_count / max(len(conds1), len(conds2), 1)
            return (OverlapRelationship.INTERSECT, overlap_percent)

        return (OverlapRelationship.DISJOINT, 0.0)

    def _conditions_equal(self, conds1: list[dict], conds2: list[dict]) -> bool:
        """Check if two condition lists are equal.

        Args:
            conds1: First condition list
            conds2: Second condition list

        Returns:
            True if lists are equal
        """
        if len(conds1) != len(conds2):
            return False

        for c1 in conds1:
            if not any(self._condition_equal(c1, c2) for c2 in conds2):
                return False

        return True

    def _condition_equal(self, c1: dict, c2: dict) -> bool:
        """Check if two conditions are equal.

        Args:
            c1: First condition
            c2: Second condition

        Returns:
            True if conditions are equal
        """
        # Compare header
        if c1.get("header", "").lower() != c2.get("header", "").lower():
            return False

        # Compare operators and values
        ops1 = self.analyzer._detect_operators(c1)
        ops2 = self.analyzer._detect_operators(c2)

        if len(ops1) != len(ops2):
            return False

        for (op1, val1), (op2, val2) in zip(ops1, ops2):
            if op1 != op2 or str(val1).lower() != str(val2).lower():
                return False

        return True

    def _is_subset(self, conds1: list[dict], conds2: list[dict]) -> bool:
        """Check if conds1 is a subset of conds2.

        Args:
            conds1: Potentially more specific conditions
            conds2: Potentially more general conditions

        Returns:
            True if conds1 ⊂ conds2
        """
        if len(conds1) > len(conds2):
            return False

        for c1 in conds1:
            if not any(self._condition_matches_or_is_more_specific(c1, c2) for c2 in conds2):
                return False

        return True

    def _get_tree_logic(self, node: Any) -> str:
        """Determine if a tree uses AND ("all") or OR ("any") logic.

        Args:
            node: Condition tree node

        Returns:
            String "all", "any", or "mixed"
        """
        if not isinstance(node, dict):
            return "simple"

        if "all" in node:
            return "all"
        elif "any" in node:
            return "any"
        elif "not" in node:
            return self._get_tree_logic(node["not"])
        else:
            return "simple"

    def _condition_matches_or_is_more_specific(self, c1: dict, c2: dict) -> bool:
        """Check if c1 matches or is more specific than c2.

        Args:
            c1: Potentially more specific condition
            c2: Potentially more general condition

        Returns:
            True if c1 is more specific or equal to c2
        """
        # Headers must match
        if c1.get("header", "").lower() != c2.get("header", "").lower():
            return False

        ops1 = self.analyzer._detect_operators(c1)
        ops2 = self.analyzer._detect_operators(c2)

        if not ops1 or not ops2:
            return False

        # For each operator in c1, check if it's more specific than c2
        op1, val1 = ops1[0]
        op2, val2 = ops2[0]

        # equals is more specific than contains
        if op1 == "equals" and op2 == "contains":
            val1_lower = str(val1).lower()
            val2_lower = str(val2).lower()
            return val2_lower in val1_lower

        # Identical conditions
        if op1 == op2 and str(val1).lower() == str(val2).lower():
            return True

        return False


class CacheQueryEngine:
    """Queries cache database to find actual affected messages."""

    def __init__(self, db_path: Path) -> None:
        """Initialize cache query engine.

        Args:
            db_path: Path to cache database
        """
        self.db_path = db_path

    def count_matching_messages(self, conditions: Any) -> int:
        """Count how many cached messages match given conditions.

        Args:
            conditions: Condition tree to evaluate

        Returns:
            Count of matching messages
        """
        try:
            from core.rule_engine import conditions_match

            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            count = 0
            cursor.execute("SELECT data FROM headers LIMIT 10000")

            from core.tools.cache_viewer import safe_parse_header

            for row in cursor:
                try:
                    header = safe_parse_header(row["data"])
                    if conditions_match(header, conditions):
                        count += 1
                except Exception:
                    pass

            conn.close()
            return count

        except Exception:
            return 0

    def count_overlap(self, cond1: Any, cond2: Any) -> tuple[int, int, int]:
        """Count messages matching: (rule1_only, rule2_only, both).

        Args:
            cond1: First condition tree
            cond2: Second condition tree

        Returns:
            Tuple of (count_r1, count_r2, count_both)
        """
        try:
            from core.rule_engine import conditions_match

            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            count_r1_only = 0
            count_r2_only = 0
            count_both = 0

            cursor.execute("SELECT data FROM headers LIMIT 10000")

            from core.tools.cache_viewer import safe_parse_header

            for row in cursor:
                try:
                    header = safe_parse_header(row["data"])
                    match1 = conditions_match(header, cond1)
                    match2 = conditions_match(header, cond2)

                    if match1 and match2:
                        count_both += 1
                    elif match1:
                        count_r1_only += 1
                    elif match2:
                        count_r2_only += 1
                except Exception:
                    pass

            conn.close()
            return count_r1_only, count_r2_only, count_both

        except Exception:
            return 0, 0, 0

    def get_sample_messages(self, conditions: Any, limit: int = 5) -> list[dict]:
        """Get sample messages matching conditions for display.

        Args:
            conditions: Condition tree to evaluate
            limit: Maximum number of samples

        Returns:
            List of sample message dicts
        """
        try:
            from core.rule_engine import conditions_match
            from core.tools.cache_viewer import safe_parse_header

            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            samples = []
            cursor.execute("SELECT folder, uid, data FROM headers LIMIT 1000")

            for row in cursor:
                if len(samples) >= limit:
                    break

                try:
                    header = safe_parse_header(row["data"])
                    if conditions_match(header, conditions):
                        from_addr = header.get("from", "")
                        subject = header.get("subject", "")
                        samples.append(
                            {
                                "folder": row["folder"],
                                "uid": row["uid"],
                                "from": from_addr[:50],
                                "subject": subject[:60],
                            }
                        )
                except Exception:
                    pass

            conn.close()
            return samples

        except Exception:
            return []


class ConflictDetector:
    """Main conflict detection engine."""

    def __init__(
        self,
        rules: Sequence[dict],
        cache_db: Optional[Path] = None,
        logger: Optional[JsonLogger] = None,
    ) -> None:
        """Initialize conflict detector.

        Args:
            rules: List of rule dictionaries
            cache_db: Optional path to cache database
            logger: Optional JSON logger
        """
        self.rules = list(rules)
        self.cache_db = cache_db
        self.logger = logger
        self.analyzer = ConditionAnalyzer()
        self.comparator = ConditionTreeComparator(self.analyzer)
        self.cache_engine = CacheQueryEngine(cache_db) if cache_db else None

    def detect_all_conflicts(self) -> list[ConflictResult]:
        """Detect all types of conflicts across all rules.

        Returns:
            List of ConflictResults sorted by severity
        """
        conflicts = []

        # Detect each type
        conflicts.extend(self.detect_priority_conflicts())
        conflicts.extend(self.detect_unreachable_rules())
        conflicts.extend(self.detect_redundant_rules())

        # Sort by severity (high first) then by rule names
        severity_order = {ConflictSeverity.HIGH: 0, ConflictSeverity.MEDIUM: 1, ConflictSeverity.LOW: 2}
        conflicts.sort(
            key=lambda c: (
                severity_order.get(c.severity, 3),
                c.rule1_name,
                c.rule2_name,
            )
        )

        return conflicts

    def detect_priority_conflicts(self) -> list[ConflictResult]:
        """Find rules with same priority that might conflict.

        Returns:
            List of priority conflict results
        """
        conflicts = []

        # Group rules by priority
        by_priority = defaultdict(list)
        for rule in self.rules:
            priority = rule.get("priority", 100)
            by_priority[priority].append(rule)

        # Check each priority group with 2+ rules
        for priority, rules_at_priority in by_priority.items():
            if len(rules_at_priority) < 2:
                continue

            # Compare all pairs
            for i, rule1 in enumerate(rules_at_priority):
                for rule2 in rules_at_priority[i + 1 :]:
                    rel, overlap_pct = self.comparator.compare_trees(
                        rule1.get("conditions", {}),
                        rule2.get("conditions", {}),
                    )

                    # Only flag priority conflicts for clear overlap relationships
                    # INTERSECT relationships (partial overlap) are unreliable without cache data
                    # because static analysis can't determine actual message overlap
                    allowed_rels = (OverlapRelationship.EQUAL, OverlapRelationship.SUBSET, OverlapRelationship.SUPERSET)
                    if rel not in allowed_rels:
                        continue

                    # Check if they have conflicting move targets
                    if not self._has_target_conflict(rule1, rule2):
                        continue

                    # Count affected messages
                    affected = None
                    if self.cache_engine:
                        _, _, affected = self.cache_engine.count_overlap(
                            rule1.get("conditions", {}),
                            rule2.get("conditions", {}),
                        )

                    # Determine severity
                    severity = self._determine_severity(overlap_pct, affected)

                    # Generate reason
                    target1 = self._get_primary_target(rule1)
                    target2 = self._get_primary_target(rule2)
                    reason = f"Both rules have move actions to different targets: '{target1}' vs '{target2}', with {overlap_pct:.0%} condition overlap"

                    conflicts.append(
                        ConflictResult(
                            type=ConflictType.PRIORITY_CONFLICT,
                            severity=severity,
                            rule1_name=rule1.get("name", "unnamed"),
                            rule2_name=rule2.get("name", "unnamed"),
                            rule1_priority=priority,
                            rule2_priority=priority,
                            overlap_relationship=rel,
                            overlap_percent=overlap_pct,
                            affected_count=affected,
                            explanation=self._explain_priority_conflict(rule1, rule2, overlap_pct),
                            suggestion=self._suggest_priority_fix(rule1, rule2, rel),
                            reason=reason,
                        )
                    )

        return conflicts

    def detect_unreachable_rules(self) -> list[ConflictResult]:
        """Find rules shadowed by higher priority rules.

        Returns:
            List of unreachable rule results
        """
        conflicts = []

        # Sort rules by priority (lower first)
        sorted_rules = sorted(self.rules, key=lambda r: r.get("priority", 100))

        # For each rule, check if shadowed by higher priority
        for i, rule2 in enumerate(sorted_rules):
            priority2 = rule2.get("priority", 100)

            for rule1 in sorted_rules[:i]:
                priority1 = rule1.get("priority", 100)

                # Only check rules with strictly higher priority
                if priority1 <= priority2:
                    continue

                # Check overlap
                rel, overlap_pct = self.comparator.compare_trees(
                    rule2.get("conditions", {}),
                    rule1.get("conditions", {}),
                )

                # Only care if rule2 is subset of rule1 (or equal, or superset)
                # SUBSET: rule2's conditions are subset of rule1's
                # SUPERSET: rule2's conditions are superset of rule1's (but fewer messages match)
                # EQUAL: identical conditions
                # This means rule1 (higher priority) either matches more broadly or the same
                if rel not in (OverlapRelationship.EQUAL, OverlapRelationship.SUBSET, OverlapRelationship.SUPERSET):
                    continue

                # Count affected messages
                affected = None
                if self.cache_engine:
                    affected = self.cache_engine.count_matching_messages(rule2.get("conditions", {}))

                # Generate reason
                reason = f"Rule with priority {priority2} is shadowed by higher priority rule {priority1}: conditions are {rel.value}"

                conflicts.append(
                    ConflictResult(
                        type=ConflictType.UNREACHABLE,
                        severity=ConflictSeverity.MEDIUM if affected else ConflictSeverity.LOW,
                        rule1_name=rule1.get("name", "unnamed"),
                        rule2_name=rule2.get("name", "unnamed"),
                        rule1_priority=priority1,
                        rule2_priority=priority2,
                        overlap_relationship=rel,
                        overlap_percent=overlap_pct,
                        affected_count=affected,
                        explanation=self._explain_unreachable(rule1, rule2),
                        suggestion=self._suggest_unreachable_fix(rule2),
                        reason=reason,
                    )
                )

        return conflicts

    def detect_redundant_rules(self) -> list[ConflictResult]:
        """Find rules with highly similar conditions.

        Returns:
            List of redundant rule results
        """
        conflicts = []

        # Compare all pairs
        for i, rule1 in enumerate(self.rules):
            for rule2 in self.rules[i + 1 :]:
                rel, overlap_pct = self.comparator.compare_trees(
                    rule1.get("conditions", {}),
                    rule2.get("conditions", {}),
                )

                # Only flag if very similar (>90%)
                if overlap_pct < 0.9 or rel == OverlapRelationship.DISJOINT:
                    continue

                priority1 = rule1.get("priority", 100)
                priority2 = rule2.get("priority", 100)

                target1 = self._get_primary_target(rule1)
                target2 = self._get_primary_target(rule2)

                # Only flag if same target and priority (true redundancy)
                if target1 != target2 or priority1 != priority2:
                    continue

                reason = f"Rules have {overlap_pct:.0%} identical conditions and same priority ({priority1}) with same target '{target1}'"

                conflicts.append(
                    ConflictResult(
                        type=ConflictType.REDUNDANT,
                        severity=ConflictSeverity.LOW,
                        rule1_name=rule1.get("name", "unnamed"),
                        rule2_name=rule2.get("name", "unnamed"),
                        rule1_priority=priority1,
                        rule2_priority=priority2,
                        overlap_relationship=rel,
                        overlap_percent=overlap_pct,
                        affected_count=None,
                        explanation=f"Rules '{rule1.get('name')}' and '{rule2.get('name')}' have {overlap_pct:.0%} identical conditions and move to the same target.",
                        suggestion=f"Consider merging these rules into a single rule with combined conditions.",
                        reason=reason,
                    )
                )

        return conflicts

    def _has_target_conflict(self, rule1: dict, rule2: dict) -> bool:
        """Check if two rules have conflicting move targets.

        Only rules with move actions can conflict.
        Rules with only keyword/flag actions don't conflict.

        Args:
            rule1: First rule
            rule2: Second rule

        Returns:
            True if targets conflict
        """
        # Both rules must have move actions to conflict
        if not (self._has_move_action(rule1) and self._has_move_action(rule2)):
            return False

        target1 = self._get_primary_target(rule1)
        target2 = self._get_primary_target(rule2)

        # No targets = no conflict
        if not target1 or not target2:
            return False

        # Different move targets = conflict
        return target1 != target2

    def _get_primary_target(self, rule: dict) -> Optional[str]:
        """Get the primary move target for a rule.

        Args:
            rule: Rule dictionary

        Returns:
            Target folder name or None
        """
        actions = rule.get("actions", [])
        if not actions:
            return None

        for action in actions:
            if isinstance(action, dict) and action.get("type") == "move":
                return action.get("target")

        return None

    def _has_move_action(self, rule: dict) -> bool:
        """Check if a rule has a move action.

        Args:
            rule: Rule dictionary

        Returns:
            True if rule has move action
        """
        actions = rule.get("actions", [])
        for action in actions:
            if isinstance(action, dict) and action.get("type") == "move":
                return True
        return False

    def _determine_severity(self, overlap_pct: float, affected_count: Optional[int]) -> ConflictSeverity:
        """Determine conflict severity.

        Args:
            overlap_pct: Overlap percentage (0.0-1.0)
            affected_count: Number of affected messages

        Returns:
            ConflictSeverity level
        """
        # High severity: >80% overlap or affected messages
        if overlap_pct > 0.8 or (affected_count and affected_count > 100):
            return ConflictSeverity.HIGH

        # Medium severity: >50% overlap
        if overlap_pct > 0.5:
            return ConflictSeverity.MEDIUM

        # Low severity: >20% overlap
        if overlap_pct > 0.2:
            return ConflictSeverity.LOW

        return ConflictSeverity.LOW

    def _explain_priority_conflict(self, rule1: dict, rule2: dict, overlap_pct: float) -> str:
        """Generate explanation for priority conflict.

        Args:
            rule1: First rule
            rule2: Second rule
            overlap_pct: Overlap percentage

        Returns:
            Explanation string
        """
        return (
            f"Rules '{rule1.get('name')}' and '{rule2.get('name')}' have the same priority "
            f"({rule1.get('priority', 100)}) and overlapping conditions ({overlap_pct:.0%} overlap). "
            f"They move emails to different targets, which can cause unpredictable behavior "
            f"depending on execution order."
        )

    def _suggest_priority_fix(self, rule1: dict, rule2: dict, rel: OverlapRelationship) -> str:
        """Generate fix suggestion for priority conflict.

        Args:
            rule1: First rule
            rule2: Second rule
            rel: Overlap relationship

        Returns:
            Suggestion string
        """
        if rel == OverlapRelationship.SUBSET:
            return (
                f"Adjust priorities: Set '{rule2.get('name')}' to a higher priority (lower number) "
                f"since it has more specific conditions."
            )
        else:
            return (
                f"Option A: Adjust priorities to differentiate execution order. "
                f"Option B: Add exclusion conditions to one rule to prevent overlap."
            )

    def _explain_unreachable(self, rule1: dict, rule2: dict) -> str:
        """Generate explanation for unreachable rule.

        Args:
            rule1: Higher priority rule
            rule2: Lower priority rule

        Returns:
            Explanation string
        """
        return (
            f"Rule '{rule2.get('name')}' will never execute because rule '{rule1.get('name')}' "
            f"(priority {rule1.get('priority', 100)}) has broader conditions and will always match first."
        )

    def _suggest_unreachable_fix(self, rule: dict) -> str:
        """Generate fix suggestion for unreachable rule.

        Args:
            rule: Unreachable rule

        Returns:
            Suggestion string
        """
        return (
            f"Increase the priority of '{rule.get('name')}' (use lower number) "
            f"or combine it with the broader rule using AND conditions."
        )


class ConflictResolver:
    """Interactive conflict resolution helper."""

    def __init__(
        self,
        conflicts: list[ConflictResult],
        rules_dir: Path,
        logger: Optional[JsonLogger] = None,
        rules: Optional[list[dict]] = None,
    ) -> None:
        """Initialize resolver.

        Args:
            conflicts: List of ConflictResult objects
            rules_dir: Path to rules directory
            logger: Optional JSON logger
            rules: Already-loaded rules list (used to build name→path index without re-reading files)
        """
        self.conflicts = conflicts
        self.rules_dir = rules_dir
        self.logger = logger
        self.applied_fixes: list[str] = []
        self._rule_name_to_path = self._build_name_index(rules)

    def suggest_fixes(self, conflict: ConflictResult) -> list[dict[str, Any]]:
        """Generate fix suggestions for a conflict.

        Args:
            conflict: The conflict to suggest fixes for

        Returns:
            List of fix suggestions with type, description, and changes
        """
        fixes = []

        if conflict.type == ConflictType.PRIORITY_CONFLICT:
            # Option A: Adjust priorities
            if conflict.overlap_relationship == OverlapRelationship.SUBSET:
                # rule1 is more specific, give it higher priority
                fixes.append({
                    "type": "adjust_priority",
                    "description": f"Increase priority of '{conflict.rule1_name}' (more specific rule)",
                    "changes": [{
                        "rule_file": self._find_rule_file(conflict.rule1_name),
                        "field": "priority",
                        "old_value": conflict.rule1_priority,
                        "new_value": max(0, conflict.rule1_priority - 10),
                    }],
                })

                # Lower rule2 priority
                fixes.append({
                    "type": "adjust_priority",
                    "description": f"Decrease priority of '{conflict.rule2_name}' (more general rule)",
                    "changes": [{
                        "rule_file": self._find_rule_file(conflict.rule2_name),
                        "field": "priority",
                        "old_value": conflict.rule2_priority,
                        "new_value": conflict.rule2_priority + 10,
                    }],
                })
            else:
                # Symmetric overlap - suggest separating priorities
                fixes.append({
                    "type": "adjust_priority",
                    "description": f"Set '{conflict.rule1_name}' to priority {conflict.rule1_priority - 10}",
                    "changes": [{
                        "rule_file": self._find_rule_file(conflict.rule1_name),
                        "field": "priority",
                        "old_value": conflict.rule1_priority,
                        "new_value": conflict.rule1_priority - 10,
                    }],
                })

        elif conflict.type == ConflictType.UNREACHABLE:
            # Option A: Increase priority of unreachable rule
            fixes.append({
                "type": "adjust_priority",
                "description": f"Increase priority of '{conflict.rule2_name}' to execute before '{conflict.rule1_name}'",
                "changes": [{
                    "rule_file": self._find_rule_file(conflict.rule2_name),
                    "field": "priority",
                    "old_value": conflict.rule2_priority,
                    "new_value": max(0, conflict.rule1_priority - 10),
                }],
            })

        elif conflict.type == ConflictType.REDUNDANT:
            fixes.append({
                "type": "merge_rules",
                "description": f"Consider merging '{conflict.rule1_name}' and '{conflict.rule2_name}'",
                "changes": [{
                    "action": "merge_rules",
                    "rule1": conflict.rule1_name,
                    "rule2": conflict.rule2_name,
                }],
            })

        return fixes

    def _build_name_index(self, rules: Optional[list[dict]]) -> dict[str, Path]:
        """Build a name→path index.  Uses already-loaded rules when available to avoid re-reading files."""
        if rules:
            return {
                rule["name"]: self.rules_dir / rule["_file"]
                for rule in rules
                if rule.get("name") and rule.get("_file")
            }
        # Fallback: single glob+parse pass (still O(n) but only done once)
        import json as _json
        index: dict[str, Path] = {}
        for rule_file in self.rules_dir.glob("*.json"):
            try:
                with open(rule_file) as f:
                    data = _json.load(f)
                name = data.get("name")
                if name:
                    index[name] = rule_file
            except Exception:
                pass
        return index

    def _find_rule_file(self, rule_name: str) -> Optional[Path]:
        """Find the file path for a rule by name."""
        return self._rule_name_to_path.get(rule_name)

    def apply_fix(
        self,
        conflict: ConflictResult,
        fix: dict[str, Any],
        dry_run: bool = True,
    ) -> bool:
        """Apply a fix to rule files.

        Args:
            conflict: The conflict being fixed
            fix: The fix to apply
            dry_run: If True, show changes without applying

        Returns:
            True if successful
        """
        try:
            import json

            if fix["type"] == "adjust_priority":
                for change in fix.get("changes", []):
                    rule_file = change.get("rule_file")
                    if not rule_file or not rule_file.exists():
                        print(f"  ❌ Rule file not found: {rule_file}")
                        return False

                    # Load rule
                    with open(rule_file) as f:
                        rule_data = json.load(f)

                    old_value = rule_data.get("priority", 100)
                    new_value = change.get("new_value")

                    print(f"  📝 {rule_file.name}:")
                    print(f"     priority: {old_value} → {new_value}")

                    if not dry_run:
                        rule_data["priority"] = new_value
                        with open(rule_file, "w") as f:
                            json.dump(rule_data, f, indent=2)
                        self.applied_fixes.append(f"{rule_file.name}: priority {old_value} → {new_value}")

            elif fix["type"] == "merge_rules":
                print(f"  ℹ️  Manual merge required:")
                print(f"     1. Review conditions in both rules")
                print(f"     2. Combine conditions with OR logic")
                print(f"     3. Delete one of the rule files")

            return True

        except Exception as e:
            print(f"  ❌ Error applying fix: {e}")
            return False

    def interactive_resolve(self) -> None:
        """Interactive resolution workflow for conflicts."""
        print("\n" + "=" * 80)
        print("INTERACTIVE CONFLICT RESOLUTION")
        print("=" * 80)

        skipped = 0
        applied = 0

        for i, conflict in enumerate(self.conflicts, 1):
            severity_icon = {
                "high": "🔴",
                "medium": "🟡",
                "low": "🟢",
            }[conflict.severity.value]

            type_display = conflict.type.value.replace("_", " ").title()

            print(f"\n[{i}/{len(self.conflicts)}] {severity_icon} {type_display}")
            print(f"  Rules: '{conflict.rule1_name}' ↔ '{conflict.rule2_name}'")
            print(f"  Overlap: {conflict.overlap_percent:.0%}")
            print(f"  Issue: {conflict.explanation}\n")

            # Generate suggestions
            suggestions = self.suggest_fixes(conflict)

            if not suggestions:
                print("  ℹ️  No automated fixes available for this conflict type")
                choice = input("  [S]kip or [Q]uit? [s/q]: ").lower()
                if choice == "q":
                    break
                skipped += 1
                continue

            # Present options
            print("  Fix options:")
            for j, fix in enumerate(suggestions, 1):
                print(f"    [{j}] {fix['description']}")

            print("  Other options:")
            print(f"    [S] Skip this conflict")
            print(f"    [Q] Quit without more changes")

            choice = input("\n  Select option [1-{}]/s/q: ".format(len(suggestions))).lower()

            if choice == "q":
                break
            elif choice == "s":
                skipped += 1
                continue
            elif choice.isdigit() and 1 <= int(choice) <= len(suggestions):
                fix_idx = int(choice) - 1
                fix = suggestions[fix_idx]

                print(f"\n  Preview of changes:")
                if self.apply_fix(conflict, fix, dry_run=True):
                    confirm = input("\n  Apply this fix? [y/N]: ").lower()
                    if confirm == "y":
                        if self.apply_fix(conflict, fix, dry_run=False):
                            applied += 1
                            print(f"  ✓ Fix applied")
                        else:
                            print(f"  ✗ Fix failed")
            else:
                print("  Invalid choice")

        print("\n" + "=" * 80)
        print(f"Resolution Summary:")
        print(f"  Applied: {applied}")
        print(f"  Skipped: {skipped}")
        print(f"  Total: {len(self.conflicts)}")
        print("=" * 80)
