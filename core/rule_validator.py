#!/usr/bin/env python3
"""Validation framework for IMAPFilter rules.

This module provides tools to detect logical impossibilities and issues
in rule structures before they are saved.
"""
from __future__ import annotations

from typing import Any


class RuleValidator:
    """Validates rule structures for logical consistency."""

    def validate_rule(self, rule: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate a rule for common issues.

        Args:
            rule: Rule dictionary with 'conditions' and 'actions'

        Returns:
            Tuple of (is_valid, list of warnings)
        """
        warnings = []

        conditions = rule.get("conditions", {})
        if not conditions:
            warnings.append("Rule has no conditions defined")
            return len(warnings) == 0, warnings

        # Check for empty groups
        empty_groups = self._find_empty_groups(conditions)
        if empty_groups:
            warnings.append(
                f"Rule contains {len(empty_groups)} empty condition group(s) - "
                "these will never match"
            )

        # Check for over-nesting
        max_depth = self._get_max_depth(conditions)
        if max_depth > 5:
            warnings.append(
                f"Rule has excessive nesting depth ({max_depth} levels) - "
                "simplify for clarity"
            )

        # Check for obvious structure problems
        structure_issues = self._find_structure_issues(conditions)
        warnings.extend(structure_issues)

        # Check actions for suspicious patterns
        action_issues = self._validate_actions(rule)
        warnings.extend(action_issues)

        return len(warnings) == 0, warnings

    def _validate_actions(self, rule: dict[str, Any]) -> list[str]:
        """Validate rule actions for suspicious patterns.

        Args:
            rule: Rule dictionary containing 'action'/'actions' keys

        Returns:
            List of warning messages
        """
        warnings = []

        # Get actions (support both singular 'action' and plural 'actions')
        actions = rule.get("actions", [])
        if not actions and "action" in rule:
            actions = [rule.get("action")]

        if not actions:
            warnings.append("Rule has no actions defined")
            return warnings

        # Validate each action
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                continue

            action_type = action.get("type", "move")

            # Validate move actions
            if action_type == "move":
                target = action.get("target", "")
                if not target:
                    warnings.append(
                        f"Action {i + 1}: Move action has no target folder specified"
                    )

        return warnings

    def _find_empty_groups(self, node: Any, path: str = "root") -> list[str]:
        """Recursively find empty groups in condition structure.

        Args:
            node: Condition node to check
            path: Path for error reporting

        Returns:
            List of paths to empty groups
        """
        empty = []

        if isinstance(node, dict) and ("all" in node or "any" in node):
            key = "all" if "all" in node else "any"
            children = node.get(key) or []

            # Check if group is empty
            if not children:
                empty.append(f"{path} [{key.upper()}]")
                return empty

            # Recursively check children
            for i, child in enumerate(children):
                child_path = f"{path}/{key}[{i}]"
                empty.extend(self._find_empty_groups(child, child_path))

        return empty

    def _get_max_depth(self, node: Any, depth: int = 0) -> int:
        """Get maximum nesting depth of condition tree.

        Args:
            node: Condition node
            depth: Current depth

        Returns:
            Maximum depth in tree
        """
        if isinstance(node, dict) and ("all" in node or "any" in node):
            key = "all" if "all" in node else "any"
            children = node.get(key) or []

            if not children:
                return depth

            max_child_depth = max(
                (self._get_max_depth(child, depth + 1) for child in children),
                default=depth,
            )
            return max_child_depth

        return depth

    def _find_structure_issues(self, node: Any) -> list[str]:
        """Find structural problems in conditions.

        Args:
            node: Root condition node

        Returns:
            List of warning messages
        """
        issues = []

        # Check for problematic patterns
        if isinstance(node, dict) and ("all" in node or "any" in node):
            key = "all" if "all" in node else "any"
            children = node.get(key) or []

            # Pattern: nested ALL with same ALL contains multiple domain+condition+nested domain+empty
            # This is the Hollister pattern
            if key == "all" and len(children) >= 2:
                if self._matches_hollister_pattern(children):
                    issues.append(
                        "⚠️ Rule structure might be impossible: nested ALL blocks "
                        "with mixed conditions and empty groups. "
                        "Did you mean: (domain1 OR domain2) AND NOT excluded?"
                    )

            # Recursively check children
            for child in children:
                issues.extend(self._find_structure_issues(child))

        return issues

    def _matches_hollister_pattern(self, children: list[Any]) -> bool:
        """Detect the Hollister rule anti-pattern.

        Pattern: nested ALL with condition + condition + nested group + empty group

        Args:
            children: List of children in ALL group

        Returns:
            True if pattern matches
        """
        # Look for: nested all group + empty all group
        has_nested_all = False
        has_empty_all = False

        for child in children:
            if isinstance(child, dict):
                if "all" in child:
                    children_of_all = child.get("all", [])
                    if not children_of_all:
                        has_empty_all = True
                    else:
                        has_nested_all = True

        return has_nested_all and has_empty_all

    def suggest_fix_for_rule(
        self, rule_data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Suggest a fix for problematic rule patterns.

        Args:
            rule_data: Rule dictionary to analyze

        Returns:
            Dict with suggestion info, or None if no known pattern
        """
        conditions = rule_data.get("conditions", {})
        rule_name = rule_data.get("name", "unknown")

        # Check for Hollister pattern
        if self._is_hollister_pattern(conditions):
            return {
                "name": rule_name,
                "pattern": "hollister",
                "issue": "Over-nested ALL blocks with impossible logic",
                "description": "This rule structure requires matching BOTH domains "
                "simultaneously, which is impossible. It should use OR for the domains.",
                "suggestion": "Use (domain1 OR domain2) AND NOT excluded pattern",
            }

        return None

    def _is_hollister_pattern(self, node: Any) -> bool:
        """Check if a condition matches the Hollister anti-pattern.

        Args:
            node: Root condition node

        Returns:
            True if pattern matches
        """
        if not isinstance(node, dict) or "all" not in node:
            return False

        children = node.get("all", [])
        if len(children) < 2:
            return False

        # Look for nested all + empty all pattern
        has_nested_all = False
        has_empty_all = False
        has_contains = 0
        has_not_contains = 0

        for child in children:
            if isinstance(child, dict):
                if "all" in child:
                    all_children = child.get("all", [])
                    if not all_children:
                        has_empty_all = True
                    else:
                        has_nested_all = True
                        # Count conditions in nested group
                        for nested_child in all_children:
                            if isinstance(nested_child, dict):
                                if "contains" in nested_child:
                                    has_contains += 1
                                if "not_contains" in nested_child:
                                    has_not_contains += 1
                elif "contains" in child or "not_contains" in child:
                    # Top-level conditions
                    if "contains" in child:
                        has_contains += 1
                    if "not_contains" in child:
                        has_not_contains += 1

        # Pattern: nested ALL with multiple contains, plus empty ALL
        return (
            has_nested_all
            and has_empty_all
            and (has_contains >= 2 or has_not_contains >= 1)
        )
