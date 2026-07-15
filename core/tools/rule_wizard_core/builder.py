"""Rule construction: condition trees, the rule builder, and rule saving."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from core.rule_utils import generate_filename


class ConditionNode:
    """Tree node for nested condition structures in rules.

    Represents a node in the condition tree, which can be:
    - A leaf node containing a single condition dict
    - A group node containing child nodes with a logic operator
    - A root node that wraps the entire condition tree

    This enables nested boolean logic like: ALL [ condition1, ANY [ condition2, condition3 ] ]

    Attributes:
        type: One of "leaf", "group", or "root"
        condition: The condition dict (for leaf nodes only)
        children: List of child ConditionNode objects (for group/root nodes)
        logic: The logical operator - "any" (OR) or "all" (AND) for group/root nodes
    """

    def __init__(self):
        """Initialize an empty condition node."""
        self.type: str = "leaf"  # "leaf", "group", or "root"
        self.condition: Optional[dict] = None  # For leaf nodes
        self.children: List[ConditionNode] = []  # For group/root nodes
        self.logic: str = "any"  # For group/root: "any" or "all"

    def to_dict(self) -> dict:
        """Convert this node to rule engine format (dict).

        For leaf nodes: returns the condition dict
        For group nodes: returns {logic: [children_dicts]}
        For root nodes: always returns {logic: [children_dicts]} (never unwrapped)
        Optimizes single-child group nodes to avoid unnecessary nesting

        Returns:
            Dictionary in rule engine format
        """
        if self.type == "leaf":
            return self.condition
        elif self.type == "group":
            # Optimize: if only one child in a group, return it directly
            if len(self.children) == 1:
                return self.children[0].to_dict()
            # Multiple children: wrap in logic operator
            return {self.logic: [child.to_dict() for child in self.children]}
        elif self.type == "root":
            # Root must always be wrapped to satisfy validation
            # Never optimize root - always include the logic operator
            return {self.logic: [child.to_dict() for child in self.children]}
        return {}


@dataclass
class ConditionGroup:
    """A group of related conditions that share the same logic operator.

    Attributes:
        indices: List of 0-based indices into the flat conditions list
        logic: The logic operator for this group - "any" (OR) or "all" (AND)
    """

    indices: List[int]
    logic: str


@dataclass
class GroupingSpec:
    """Specification for how to group conditions in a rule.

    This captures the user's grouping choices and is used to convert
    a flat list of conditions into a nested tree structure.

    Attributes:
        groups: List of ConditionGroup objects describing each group
        overall_logic: The logic operator for the root - "any" or "all"
    """

    groups: List[ConditionGroup]
    overall_logic: str


class RuleBuilder:
    """Build and validate IMAPFilter rules with a fluent interface.

    This class provides a builder pattern for constructing rule dictionaries
    that match the IMAPFilter rule format. It validates all components and
    can generate the final rule JSON structure.

    Example:
        >>> builder = RuleBuilder()
        >>> builder.set_name("Newsletters » Reddit")
        >>> builder.set_priority(100)
        >>> builder.add_condition("from", "contains", "noreply@redditmail.com")
        >>> builder.add_condition("from", "contains", "community@reddit.com")
        >>> builder.set_logic("any")
        >>> builder.set_action("move", "Newsletters/Reddit")
        >>> builder.add_comment("Created with rule wizard")
        >>> valid, msg = builder.validate()
        >>> if valid:
        ...     rule = builder.generate_rule()
    """

    def __init__(self):
        """Initialize an empty rule builder."""
        self.name: str = ""
        self.priority: int = 100

        # Tree structure for nested conditions (replaces flat list)
        self.root: ConditionNode = ConditionNode()
        self.root.type = "root"

        # Backward compatibility: Keep flat interface during collection
        # These are used while building conditions, then converted to tree
        self._flat_conditions: List[dict] = []
        self._flat_logic: str = "any"

        # Deprecated: kept for backward compatibility with existing code
        self.conditions: List[dict] = []  # Alias to _flat_conditions
        self.logic: str = "any"

        self.actions: List[dict] = []  # Support multiple actions
        self.comments: List[str] = []

    def set_name(self, name: str) -> "RuleBuilder":
        """Set the rule name.

        Args:
            name: Human-readable rule name (e.g., "Banking » NatWest")

        Returns:
            Self for method chaining
        """
        self.name = name.strip()
        return self

    def set_priority(self, priority: int) -> "RuleBuilder":
        """Set the rule priority.

        Args:
            priority: Integer priority value (default: 100, higher = more important)

        Returns:
            Self for method chaining
        """
        self.priority = priority
        return self

    def add_condition(
        self, header: str, match_type: str, value: str
    ) -> "RuleBuilder":
        """Add a condition to the rule.

        Args:
            header: Header field to match (e.g., "from", "to", "subject")
            match_type: Type of match - "contains" or "regex"
            value: The pattern to match against

        Returns:
            Self for method chaining
        """
        condition = {"header": header.lower(), match_type: value}
        self._flat_conditions.append(condition)
        # Maintain backward compatibility alias
        self.conditions = self._flat_conditions
        return self

    def set_logic(self, logic: str) -> "RuleBuilder":
        """Set the logic operator for multiple conditions.

        Args:
            logic: Either "all" (AND) or "any" (OR) for combining conditions

        Returns:
            Self for method chaining
        """
        if logic.lower() in ("all", "any"):
            self._flat_logic = logic.lower()
            self.logic = self._flat_logic  # Backward compatibility
        return self

    def add_action(self, action_type: str, target: str = "", keywords: List[str] = None) -> "RuleBuilder":
        """Add an action to the rule.

        Args:
            action_type: Type of action ("move", "set_keywords", or "remove_keywords")
            target: Target folder path for move actions (e.g., "Banking/NatWest")
            keywords: List of keywords for keyword actions

        Returns:
            Self for method chaining
        """
        action = {"type": action_type.lower()}
        if target:
            action["target"] = target
        if keywords:
            action["keywords"] = keywords
        self.actions.append(action)
        return self

    def add_comment(self, comment: str) -> "RuleBuilder":
        """Add a documentation comment to the rule.

        Args:
            comment: Comment text to add

        Returns:
            Self for method chaining
        """
        if comment.strip():
            self.comments.append(comment.strip())
        return self

    def finalize_conditions(self, grouping: Optional[GroupingSpec] = None) -> None:
        """Convert flat conditions to tree structure for nested logic support.

        This method must be called before generate_rule() when using grouped conditions.
        For simple rules without grouping, generate_rule() will auto-finalize.

        Args:
            grouping: Optional GroupingSpec describing how to group conditions.
                     If None, creates simple flat structure (backward compatible).
        """
        if grouping is None:
            # Simple case: wrap all conditions in single logic operator
            for cond in self._flat_conditions:
                node = ConditionNode()
                node.type = "leaf"
                node.condition = cond
                self.root.children.append(node)
            self.root.logic = self._flat_logic
        else:
            # Complex case: apply grouping specification
            self._apply_grouping(grouping)

    def _apply_grouping(self, spec: GroupingSpec) -> None:
        """Apply grouping specification to create nested structure.

        Args:
            spec: GroupingSpec defining how to organize conditions
        """
        self.root.logic = spec.overall_logic

        for group in spec.groups:
            if len(group.indices) == 1:
                # Single condition - add as leaf to root
                idx = group.indices[0]
                node = ConditionNode()
                node.type = "leaf"
                node.condition = self._flat_conditions[idx]
                self.root.children.append(node)
            else:
                # Multiple conditions - create group node
                group_node = ConditionNode()
                group_node.type = "group"
                group_node.logic = group.logic

                for idx in group.indices:
                    leaf = ConditionNode()
                    leaf.type = "leaf"
                    leaf.condition = self._flat_conditions[idx]
                    group_node.children.append(leaf)

                self.root.children.append(group_node)

    def validate(self) -> Tuple[bool, str]:
        """Validate the rule configuration.

        Returns:
            Tuple of (is_valid, error_message)
            If valid, error_message is empty string
        """
        if not self.name:
            return False, "Rule name is required"

        if not self.conditions:
            return False, "At least one condition is required"

        if not self.actions:
            return False, "At least one action is required"

        # Validate each action
        for i, action in enumerate(self.actions):
            action_type = action.get("type", "")
            if not action_type:
                return False, f"Action {i+1} is missing a type"

            if action_type not in ("move", "set_keywords", "remove_keywords"):
                return False, f"Action {i+1} has unsupported type: {action_type}"

            # Validate action type specific requirements
            if action_type == "move":
                if not action.get("target"):
                    return False, f"Action {i+1} (move) requires a target folder"
            elif action_type in ("set_keywords", "remove_keywords"):
                if not action.get("keywords"):
                    return False, f"Action {i+1} ({action_type}) requires at least one keyword"

        # Validate each condition
        for i, condition in enumerate(self.conditions):
            if "header" not in condition:
                return False, f"Condition {i+1} missing header field"

            # Check for match type keys (supports multiple match types)
            match_type_keys = ["contains", "regex", "equals", "not_equals", "not_contains", "not_regex"]
            has_match_type = any(key in condition for key in match_type_keys)
            if not has_match_type:
                return False, f"Condition {i+1} must have a match type (contains, regex, equals, etc.)"

            # Check for multiple match types (should only have one)
            present_keys = [key for key in match_type_keys if key in condition]
            if len(present_keys) > 1:
                return False, f"Condition {i+1} has multiple match types: {', '.join(present_keys)}"

            # Get the match value from whichever match type is present
            value = None
            for key in present_keys:
                value = condition.get(key)
                if value:
                    break

            if not value or not str(value).strip():
                return False, f"Condition {i+1} has empty match value"

        # Validate logic for multiple conditions
        if len(self.conditions) > 1 and self.logic not in ("all", "any"):
            return False, "Logic must be 'all' or 'any' for multiple conditions"

        return True, ""

    def generate_rule(self) -> dict:
        """Generate the complete rule dictionary in IMAPFilter format.

        Automatically finalizes conditions if not already done.
        For single action: uses "action" field (backward compatible)
        For multiple actions: uses "actions" array

        Returns:
            Dictionary in IMAPFilter rule format (with nested conditions if grouped)

        Raises:
            ValueError: If rule validation fails
        """
        valid, error = self.validate()
        if not valid:
            raise ValueError(f"Invalid rule configuration: {error}")

        # Auto-finalize if conditions haven't been finalized yet
        if not self.root.children:
            self.finalize_conditions(grouping=None)

        # Build rule with correct key order: name, priority, conditions
        rule = {
            "name": self.name,
            "priority": self.priority,
        }

        # Build conditions block using tree structure (supports nesting)
        rule["conditions"] = self.root.to_dict()

        # Add actions (single or multiple)
        if len(self.actions) == 1:
            # Backward compatibility: use "action" field for single action
            rule["action"] = self.actions[0]
        else:
            # Multiple actions: use "actions" array
            rule["actions"] = self.actions

        # Add comments if any
        if self.comments:
            rule["comments"] = self.comments

        return rule


def save_rule(rule: dict | list, rules_dir: Path) -> Tuple[bool, str]:
    """Save a rule or list of rules to JSON file(s) in the rules directory.

    This function validates the rule(s), generates appropriate filename(s),
    creates the rules directory if needed, and writes the rule(s) as
    pretty-printed JSON.

    Args:
        rule: A rule dictionary or list of rule dictionaries to save
        rules_dir: Path to the rules directory

    Returns:
        Tuple of (success: bool, message: str)
        - If successful: (True, "Saved to /path/to/file.json")
        - If failed: (False, "Error description")

    Examples:
        >>> rule = {
        ...     "name": "Test Rule",
        ...     "priority": 100,
        ...     "conditions": {"any": [{"header": "from", "contains": "test@example.com"}]},
        ...     "action": {"type": "move", "target": "Test"}
        ... }
        >>> success, msg = save_rule(rule, Path("/rules"))
        >>> if success:
        ...     print(f"Rule saved: {msg}")
    """
    # Handle list of rules
    if isinstance(rule, list):
        saved_files = []
        for single_rule in rule:
            success, message = save_rule(single_rule, rules_dir)
            if not success:
                return False, f"Failed to save rule: {message}"
            saved_files.append(message)
        return True, f"Saved {len(rule)} rule(s): {', '.join(saved_files)}"

    # Validate single rule
    required_fields = ["name", "priority", "conditions"]
    for field in required_fields:
        if field not in rule:
            return False, f"Rule missing required field: {field}"

    # Validate conditions structure
    conditions = rule.get("conditions", {})
    if not isinstance(conditions, dict):
        return False, "Conditions must be a dictionary"

    if "all" not in conditions and "any" not in conditions:
        return False, "Conditions must have either 'all' or 'any' key"

    # Validate action(s) - support both "action" (single) and "actions" (array)
    if "action" in rule and "actions" in rule:
        return False, "Rule cannot have both 'action' and 'actions' fields"

    if "action" not in rule and "actions" not in rule:
        return False, "Rule must have either 'action' or 'actions' field"

    # Validate single action (backward compatible)
    if "action" in rule:
        action = rule.get("action", {})
        if not isinstance(action, dict):
            return False, "Action must be a dictionary"

        if "type" not in action:
            return False, "Action must have 'type' field"

        # Validate action type specific requirements
        action_type = action.get("type", "")
        if action_type == "move" and "target" not in action:
            return False, "Move action must have 'target' field"
        elif action_type in ("set_keywords", "remove_keywords") and "keywords" not in action:
            return False, f"{action_type} action must have 'keywords' field"

    # Validate actions array
    if "actions" in rule:
        actions = rule.get("actions", [])
        if not isinstance(actions, list):
            return False, "Actions must be a list"

        if not actions:
            return False, "Actions list cannot be empty"

        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                return False, f"Action {i+1} must be a dictionary"

            if "type" not in action:
                return False, f"Action {i+1} must have 'type' field"

            # Validate action type specific requirements
            action_type = action.get("type", "")
            if action_type == "move" and "target" not in action:
                return False, f"Action {i+1} (move) must have 'target' field"
            elif action_type in ("set_keywords", "remove_keywords") and "keywords" not in action:
                return False, f"Action {i+1} ({action_type}) must have 'keywords' field"

    # Create rules directory if it doesn't exist
    try:
        rules_dir = Path(rules_dir)
        rules_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"Failed to create rules directory: {e}"

    # Generate filename
    try:
        filepath = generate_filename(rule["name"], rules_dir)
    except Exception as e:
        return False, f"Failed to generate filename: {e}"

    # Write rule to file
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(rule, f, indent=2, ensure_ascii=False)
            f.write("\n")  # Add trailing newline
        return True, f"Saved to {filepath}"
    except Exception as e:
        return False, f"Failed to write rule file: {e}"
