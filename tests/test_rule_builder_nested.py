"""Unit tests for nested condition structures in RuleBuilder.

Tests the ConditionNode, ConditionGroup, GroupingSpec classes and
RuleBuilder's ability to create nested boolean logic in rules.
"""

import json
import pytest
from pathlib import Path

from core.tools.rule_wizard_core import (
    ConditionNode,
    ConditionGroup,
    GroupingSpec,
    RuleBuilder,
)


class TestConditionNode:
    """Test the ConditionNode tree structure class."""

    def test_leaf_node_creation(self):
        """Test creating a simple leaf node."""
        node = ConditionNode()
        assert node.type == "leaf"
        assert node.condition is None
        assert node.children == []
        assert node.logic == "any"

    def test_leaf_node_to_dict(self):
        """Test converting a leaf node to dictionary."""
        node = ConditionNode()
        node.type = "leaf"
        node.condition = {"header": "from", "contains": "test@example.com"}

        result = node.to_dict()
        assert result == {"header": "from", "contains": "test@example.com"}

    def test_group_node_with_children(self):
        """Test creating a group node with children."""
        group = ConditionNode()
        group.type = "group"
        group.logic = "any"

        # Add two leaf children
        child1 = ConditionNode()
        child1.type = "leaf"
        child1.condition = {"header": "subject", "contains": "pattern1"}
        group.children.append(child1)

        child2 = ConditionNode()
        child2.type = "leaf"
        child2.condition = {"header": "subject", "contains": "pattern2"}
        group.children.append(child2)

        result = group.to_dict()
        assert result == {
            "any": [
                {"header": "subject", "contains": "pattern1"},
                {"header": "subject", "contains": "pattern2"},
            ]
        }

    def test_single_child_optimization(self):
        """Test that single-child nodes are optimized."""
        group = ConditionNode()
        group.type = "group"
        group.logic = "all"

        # Add single child
        child = ConditionNode()
        child.type = "leaf"
        child.condition = {"header": "from", "contains": "sender@example.com"}
        group.children.append(child)

        # Should return child's dict directly, not wrapped in "all"
        result = group.to_dict()
        assert result == {"header": "from", "contains": "sender@example.com"}
        assert "all" not in result

    def test_nested_groups(self):
        """Test nested groups (group containing groups)."""
        root = ConditionNode()
        root.type = "root"
        root.logic = "all"

        # Create first group (from conditions)
        from_group = ConditionNode()
        from_group.type = "group"
        from_group.logic = "any"

        from_leaf1 = ConditionNode()
        from_leaf1.type = "leaf"
        from_leaf1.condition = {"header": "from", "contains": "test1@example.com"}
        from_group.children.append(from_leaf1)

        from_leaf2 = ConditionNode()
        from_leaf2.type = "leaf"
        from_leaf2.condition = {"header": "from", "contains": "test2@example.com"}
        from_group.children.append(from_leaf2)

        # Create second group (subject conditions)
        subject_group = ConditionNode()
        subject_group.type = "group"
        subject_group.logic = "any"

        subject_leaf = ConditionNode()
        subject_leaf.type = "leaf"
        subject_leaf.condition = {"header": "subject", "contains": "important"}
        subject_group.children.append(subject_leaf)

        # Add groups to root
        root.children.append(from_group)
        root.children.append(subject_group)

        result = root.to_dict()
        # Note: single-child groups are optimized away, so subject_group with 1 child
        # becomes just the condition dict
        assert result == {
            "all": [
                {
                    "any": [
                        {"header": "from", "contains": "test1@example.com"},
                        {"header": "from", "contains": "test2@example.com"},
                    ]
                },
                {"header": "subject", "contains": "important"},
            ]
        }

    def test_root_node(self):
        """Test root node type - always wraps children even if single."""
        root = ConditionNode()
        root.type = "root"
        root.logic = "all"

        leaf = ConditionNode()
        leaf.type = "leaf"
        leaf.condition = {"header": "from", "contains": "test@example.com"}
        root.children.append(leaf)

        result = root.to_dict()
        # Root nodes always wrap their children to ensure validation compatibility
        assert result == {"all": [{"header": "from", "contains": "test@example.com"}]}

    def test_empty_node_to_dict(self):
        """Test empty leaf node returns None."""
        node = ConditionNode()
        result = node.to_dict()
        # Empty leaf node has None condition, which is returned as-is
        assert result is None


class TestGroupingSpec:
    """Test the GroupingSpec dataclass."""

    def test_grouping_spec_creation(self):
        """Test creating a GroupingSpec."""
        spec = GroupingSpec(
            groups=[ConditionGroup(indices=[0], logic="any")],
            overall_logic="all",
        )
        assert len(spec.groups) == 1
        assert spec.groups[0].indices == [0]
        assert spec.groups[0].logic == "any"
        assert spec.overall_logic == "all"

    def test_multiple_groups(self):
        """Test GroupingSpec with multiple groups."""
        spec = GroupingSpec(
            groups=[
                ConditionGroup(indices=[0], logic="any"),
                ConditionGroup(indices=[1, 2, 3], logic="any"),
            ],
            overall_logic="all",
        )
        assert len(spec.groups) == 2
        assert spec.groups[0].indices == [0]
        assert spec.groups[1].indices == [1, 2, 3]


class TestRuleBuilderNested:
    """Test RuleBuilder with nested conditions."""

    def test_simple_flat_rule_backward_compat(self):
        """Test that simple rules still work (backward compatibility)."""
        builder = RuleBuilder()
        builder.set_name("Test Rule")
        builder.add_condition("from", "contains", "test@example.com")
        builder.set_logic("any")
        builder.add_action("move", "Test/Folder")

        rule = builder.generate_rule()
        assert "conditions" in rule
        assert rule["name"] == "Test Rule"
        assert rule["priority"] == 100

        # Root nodes now always wrap conditions (for validation compatibility)
        conditions = rule["conditions"]
        assert "any" in conditions  # Root wraps in logic operator
        assert len(conditions["any"]) == 1
        assert conditions["any"][0]["header"] == "from"
        assert conditions["any"][0]["contains"] == "test@example.com"

    def test_multiple_flat_conditions(self):
        """Test multiple conditions with flat logic."""
        builder = RuleBuilder()
        builder.set_name("Multi Condition")
        builder.add_condition("from", "contains", "test1@example.com")
        builder.add_condition("from", "contains", "test2@example.com")
        builder.set_logic("any")
        builder.add_action("move", "Test/Folder")

        rule = builder.generate_rule()
        conditions = rule["conditions"]
        assert "any" in conditions
        assert len(conditions["any"]) == 2

    def test_finalize_conditions_flat(self):
        """Test finalize_conditions with no grouping."""
        builder = RuleBuilder()
        builder.set_name("Flat Test")
        builder.add_condition("from", "contains", "test@example.com")
        builder.add_condition("subject", "contains", "important")
        builder._flat_logic = "all"
        builder.add_action("move", "Test/Folder")

        builder.finalize_conditions(grouping=None)

        assert len(builder.root.children) == 2
        assert builder.root.logic == "all"

        rule = builder.generate_rule()
        assert "all" in rule["conditions"]
        assert len(rule["conditions"]["all"]) == 2

    def test_finalize_conditions_grouped(self):
        """Test finalize_conditions with grouping spec."""
        builder = RuleBuilder()
        builder.set_name("Calendar Responses")
        builder.add_condition("from", "contains", "steve.gibson@metoffice.gov.uk")
        builder.add_condition("subject", "contains", "Accepted:")
        builder.add_condition("subject", "contains", "Tentative:")
        builder.add_condition("subject", "contains", "Declined:")
        builder.add_action("move", "Cal Responses")

        grouping = GroupingSpec(
            groups=[
                ConditionGroup(indices=[0], logic="any"),
                ConditionGroup(indices=[1, 2, 3], logic="any"),
            ],
            overall_logic="all",
        )
        builder.finalize_conditions(grouping)

        rule = builder.generate_rule()
        conditions = rule["conditions"]

        # Should have "all" at root
        assert "all" in conditions
        assert len(conditions["all"]) == 2

        # First should be from condition
        from_cond = conditions["all"][0]
        assert from_cond["header"] == "from"
        assert from_cond["contains"] == "steve.gibson@metoffice.gov.uk"

        # Second should be "any" group of subjects
        subject_group = conditions["all"][1]
        assert "any" in subject_group
        assert len(subject_group["any"]) == 3

    def test_user_requested_pattern(self):
        """Test the exact pattern the user requested."""
        builder = RuleBuilder()
        builder.set_name("Cal Responses")
        builder.set_priority(100)

        # Add conditions in order
        builder.add_condition("from", "contains", "steve.gibson@metoffice.gov.uk")
        builder.add_condition("subject", "contains", "Accepted:")
        builder.add_condition("subject", "contains", "Tentative:")
        builder.add_condition("subject", "contains", "Declined:")
        builder.add_action("move", "Cal Responses")

        # Create grouping: FROM alone, SUBJECT patterns as group
        grouping = GroupingSpec(
            groups=[
                ConditionGroup(indices=[0], logic="any"),
                ConditionGroup(indices=[1, 2, 3], logic="any"),
            ],
            overall_logic="all",
        )
        builder.finalize_conditions(grouping)

        rule = builder.generate_rule()

        # Verify structure
        assert rule["name"] == "Cal Responses"
        assert rule["priority"] == 100

        conditions = rule["conditions"]
        expected_conditions = {
            "all": [
                {
                    "header": "from",
                    "contains": "steve.gibson@metoffice.gov.uk",
                },
                {
                    "any": [
                        {"header": "subject", "contains": "Accepted:"},
                        {"header": "subject", "contains": "Tentative:"},
                        {"header": "subject", "contains": "Declined:"},
                    ]
                },
            ]
        }
        assert conditions == expected_conditions

    def test_multiple_groups_complex(self):
        """Test rule with multiple independent groups."""
        builder = RuleBuilder()
        builder.set_name("Complex Rule")

        # Add 6 conditions: 2 from, 2 subject, 2 to
        builder.add_condition("from", "contains", "sender1@example.com")
        builder.add_condition("from", "contains", "sender2@example.com")
        builder.add_condition("subject", "contains", "pattern1")
        builder.add_condition("subject", "contains", "pattern2")
        builder.add_condition("to", "contains", "recipient1@example.com")
        builder.add_condition("to", "contains", "recipient2@example.com")
        builder.add_action("move", "Test/Folder")

        # Group: all senders (any), all subjects (any), all recipients (any)
        # Overall: all groups must match
        grouping = GroupingSpec(
            groups=[
                ConditionGroup(indices=[0, 1], logic="any"),  # from group
                ConditionGroup(indices=[2, 3], logic="any"),  # subject group
                ConditionGroup(indices=[4, 5], logic="any"),  # to group
            ],
            overall_logic="all",
        )
        builder.finalize_conditions(grouping)

        rule = builder.generate_rule()
        conditions = rule["conditions"]

        assert "all" in conditions
        assert len(conditions["all"]) == 3

        # Each group should be an "any"
        for group in conditions["all"]:
            assert "any" in group
            assert len(group["any"]) == 2

    def test_backward_compat_no_finalize(self):
        """Test backward compatibility when finalize is not called."""
        builder = RuleBuilder()
        builder.set_name("Old Style")
        builder.add_condition("from", "contains", "test@example.com")
        builder.set_logic("any")
        builder.add_action("move", "Test/Folder")

        # Don't call finalize_conditions, just generate
        rule = builder.generate_rule()

        # Should still work due to auto-finalize in generate_rule()
        assert "conditions" in rule
        assert rule["name"] == "Old Style"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
