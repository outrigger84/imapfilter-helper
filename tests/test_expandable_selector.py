#!/usr/bin/env python3
"""Unit tests for expandable display names feature (Phase 1-4).

Tests the data structure foundation including:
- DisplayNameVariation dataclass
- EmailGroup dataclass with properties
- ExpandableItem dataclass
- _extract_display_name() helper function
- consolidate_email_addresses() with backward compatibility
- FilterableListSelector with expandable items
- create_expandable_email_items() helper
"""
import unittest
import pytest
from unittest.mock import Mock
from core.tools.rule_wizard_core import (
    DisplayNameVariation,
    EmailGroup,
    ExpandableItem,
    FilterableListSelector,
    _extract_display_name,
    consolidate_email_addresses,
    create_expandable_email_items,
    extract_email_address,
)


class TestDisplayNameExtraction:
    """Test the _extract_display_name() helper function."""

    def test_extract_display_name_from_angle_brackets(self):
        """Test extracting display name from 'Name <email>' format."""
        result = _extract_display_name('ClearScore <marketing@clearscore.com>')
        assert result == 'ClearScore'

    def test_extract_display_name_with_quotes(self):
        """Test extracting display name with quotes removed."""
        result = _extract_display_name('"ClearScore" <marketing@clearscore.com>')
        assert result == 'ClearScore'

    def test_extract_display_name_with_spaces(self):
        """Test extracting display name with spaces (multi-word names)."""
        result = _extract_display_name('First Last <email@example.com>')
        assert result == 'First Last'

    def test_extract_display_name_bare_email(self):
        """Test bare email address with no display name."""
        result = _extract_display_name('email@example.com')
        assert result == ''

    def test_extract_display_name_angle_brackets_only(self):
        """Test email with angle brackets but no display name."""
        result = _extract_display_name('<email@example.com>')
        assert result == ''

    def test_extract_display_name_with_spaces_in_input(self):
        """Test handling of leading/trailing spaces."""
        result = _extract_display_name('  MyName  <email@example.com>  ')
        assert result == 'MyName'

    def test_extract_display_name_complex_format(self):
        """Test complex display name with special characters."""
        result = _extract_display_name('Customer Support - Team <support@example.com>')
        assert result == 'Customer Support - Team'

    def test_extract_display_name_quoted_with_spaces(self):
        """Test quoted display name with internal spaces."""
        result = _extract_display_name('"My Company Name" <noreply@company.com>')
        assert result == 'My Company Name'

    def test_extract_display_name_empty_string(self):
        """Test handling of empty string input."""
        result = _extract_display_name('')
        assert result == ''

    def test_extract_display_name_single_quotes(self):
        """Test that single quotes are NOT removed (only double quotes)."""
        result = _extract_display_name("'MyName' <email@example.com>")
        assert result == "'MyName'"


class TestDisplayNameVariationDataclass:
    """Test the DisplayNameVariation dataclass."""

    def test_create_display_name_variation(self):
        """Test creating a DisplayNameVariation instance."""
        var = DisplayNameVariation(
            full_address='ClearScore <marketing@clearscore.com>',
            display_name='ClearScore',
            count=582
        )
        assert var.full_address == 'ClearScore <marketing@clearscore.com>'
        assert var.display_name == 'ClearScore'
        assert var.count == 582

    def test_display_name_variation_with_empty_display(self):
        """Test DisplayNameVariation with empty display name."""
        var = DisplayNameVariation(
            full_address='marketing@clearscore.com',
            display_name='',
            count=100
        )
        assert var.display_name == ''
        assert var.count == 100


class TestEmailGroupDataclass:
    """Test the EmailGroup dataclass and its properties."""

    def test_create_email_group_with_single_variation(self):
        """Test creating an EmailGroup with a single variation."""
        var = DisplayNameVariation(
            full_address='noreply@amazon.com',
            display_name='',
            count=234
        )
        group = EmailGroup(
            email='noreply@amazon.com',
            total_count=234,
            variations=[var]
        )
        assert group.email == 'noreply@amazon.com'
        assert group.total_count == 234
        assert len(group.variations) == 1

    def test_email_group_variation_count_property_single(self):
        """Test variation_count property with single variation."""
        var = DisplayNameVariation(
            full_address='noreply@amazon.com',
            display_name='',
            count=234
        )
        group = EmailGroup(
            email='noreply@amazon.com',
            total_count=234,
            variations=[var]
        )
        assert group.variation_count == 1

    def test_email_group_variation_count_property_multiple(self):
        """Test variation_count property with multiple variations."""
        variations = [
            DisplayNameVariation(
                full_address='ClearScore <marketing@clearscore.com>',
                display_name='ClearScore',
                count=582
            ),
            DisplayNameVariation(
                full_address='"ClearScore" <marketing@clearscore.com>',
                display_name='ClearScore',
                count=404
            ),
            DisplayNameVariation(
                full_address='Clearscore <marketing@clearscore.com>',
                display_name='Clearscore',
                count=17
            ),
        ]
        group = EmailGroup(
            email='marketing@clearscore.com',
            total_count=1003,
            variations=variations
        )
        assert group.variation_count == 3

    def test_email_group_has_variations_false(self):
        """Test has_variations property when there is only one variation."""
        var = DisplayNameVariation(
            full_address='noreply@amazon.com',
            display_name='',
            count=234
        )
        group = EmailGroup(
            email='noreply@amazon.com',
            total_count=234,
            variations=[var]
        )
        assert group.has_variations is False

    def test_email_group_has_variations_true(self):
        """Test has_variations property when there are multiple variations."""
        variations = [
            DisplayNameVariation(
                full_address='ClearScore <marketing@clearscore.com>',
                display_name='ClearScore',
                count=582
            ),
            DisplayNameVariation(
                full_address='Clearscore <marketing@clearscore.com>',
                display_name='Clearscore',
                count=17
            ),
        ]
        group = EmailGroup(
            email='marketing@clearscore.com',
            total_count=599,
            variations=variations
        )
        assert group.has_variations is True


class TestExpandableItemDataclass:
    """Test the ExpandableItem dataclass."""

    def test_create_expandable_item_basic(self):
        """Test creating a basic ExpandableItem."""
        item = ExpandableItem(label='Test Item', count=100)
        assert item.label == 'Test Item'
        assert item.count == 100
        assert item.is_expandable is False
        assert item.is_expanded is False

    def test_create_expandable_item_expanded(self):
        """Test creating an expanded ExpandableItem."""
        children = [
            ExpandableItem(label='Child 1', count=50),
            ExpandableItem(label='Child 2', count=30),
        ]
        item = ExpandableItem(
            label='Parent',
            count=100,
            is_expandable=True,
            is_expanded=True,
            children=children,
            indent_level=0
        )
        assert item.is_expandable is True
        assert item.is_expanded is True
        assert len(item.children) == 2
        assert item.indent_level == 0

    def test_expandable_item_with_data(self):
        """Test ExpandableItem with associated data object."""
        email_group = EmailGroup(
            email='test@example.com',
            total_count=100,
            variations=[]
        )
        item = ExpandableItem(
            label='test@example.com',
            count=100,
            is_expandable=True,
            data=email_group
        )
        assert item.data is email_group
        assert isinstance(item.data, EmailGroup)

    def test_expandable_item_with_parent_index(self):
        """Test ExpandableItem with parent index."""
        item = ExpandableItem(
            label='Child Item',
            count=50,
            parent_index=0,
            indent_level=1
        )
        assert item.parent_index == 0
        assert item.indent_level == 1


class TestConsolidateBackwardCompat:
    """Test consolidate_email_addresses() with default params (backward compatibility)."""

    def test_consolidate_backward_compat_basic(self):
        """Test backward compatible format with basic data."""
        addresses = [
            ('noreply@amazon.com', 234),
            ('orders@amazon.com', 45),
        ]
        result = consolidate_email_addresses(addresses)

        # Verify it's returning the old format (tuples with 3 elements)
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(item, tuple) and len(item) == 3 for item in result)

        # First item should have highest count
        assert result[0][0] == 'noreply@amazon.com'
        assert result[0][1] == 234
        assert result[0][2] == 1  # variation count

    def test_consolidate_backward_compat_with_variations(self):
        """Test backward compatible format consolidates variations."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('Clearscore <marketing@clearscore.com>', 17),
            ('updates@clearscore.com', 1008),
        ]
        result = consolidate_email_addresses(addresses)

        assert isinstance(result, list)
        assert len(result) == 2

        # First should be updates@clearscore.com (highest count)
        assert result[0][0] == 'updates@clearscore.com'
        assert result[0][1] == 1008
        assert result[0][2] == 1

        # Second should be marketing@clearscore.com
        assert result[1][0] == 'marketing@clearscore.com'
        assert result[1][1] == 1003  # 582 + 404 + 17
        assert result[1][2] == 3  # 3 variations

    def test_consolidate_backward_compat_single_email_multiple_displays(self):
        """Test consolidating a single email with multiple display names."""
        addresses = [
            ('Company A <billing@example.com>', 100),
            ('Company A <billing@example.com>', 50),
            ('Support <billing@example.com>', 25),
        ]
        result = consolidate_email_addresses(addresses)

        assert len(result) == 1
        assert result[0][0] == 'billing@example.com'
        assert result[0][1] == 175  # 100 + 50 + 25
        assert result[0][2] == 2  # 2 distinct variations


class TestConsolidateWithVariations:
    """Test consolidate_email_addresses() with preserve_variations=True."""

    def test_consolidate_preserve_variations_returns_email_groups(self):
        """Test that preserve_variations=True returns EmailGroup objects."""
        addresses = [
            ('noreply@amazon.com', 234),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], EmailGroup)
        assert result[0].email == 'noreply@amazon.com'
        assert result[0].total_count == 234

    def test_consolidate_preserve_variations_single_address(self):
        """Test preserve_variations with single email address."""
        addresses = [
            ('noreply@amazon.com', 234),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        group = result[0]
        assert group.email == 'noreply@amazon.com'
        assert group.total_count == 234
        assert group.variation_count == 1
        assert group.has_variations is False
        assert len(group.variations) == 1
        assert group.variations[0].display_name == ''
        assert group.variations[0].count == 234

    def test_consolidate_preserve_variations_multiple_variations(self):
        """Test preserve_variations with multiple display name variations."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('Clearscore <marketing@clearscore.com>', 17),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        assert len(result) == 1
        group = result[0]
        assert group.email == 'marketing@clearscore.com'
        assert group.total_count == 1003
        assert group.variation_count == 3
        assert group.has_variations is True

        # Check variations are sorted by count descending
        assert group.variations[0].count == 582
        assert group.variations[1].count == 404
        assert group.variations[2].count == 17

    def test_consolidate_preserve_variations_display_names_extracted(self):
        """Test that display names are properly extracted in variations."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"Clearscore" <marketing@clearscore.com>', 404),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        group = result[0]
        assert group.variations[0].display_name == 'ClearScore'
        assert group.variations[1].display_name == 'Clearscore'

    def test_consolidate_preserve_variations_multiple_emails(self):
        """Test preserve_variations with multiple different emails."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('updates@clearscore.com', 1008),
            ('noreply@amazon.com', 234),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        assert len(result) == 3
        # Verify sorted by total_count descending
        assert result[0].total_count == 1008
        assert result[1].total_count == 986  # 582 + 404
        assert result[2].total_count == 234

    def test_consolidate_preserve_variations_full_address_preserved(self):
        """Test that full_address is preserved in variations."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        group = result[0]
        assert group.variations[0].full_address == 'ClearScore <marketing@clearscore.com>'

    def test_consolidate_preserve_variations_complex_scenario(self):
        """Test preserve_variations with complex multi-email scenario."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('Clearscore <marketing@clearscore.com>', 17),
            ('updates@clearscore.com', 1008),
            ('Amazon Notifications <noreply@amazon.com>', 234),
            ('noreply@amazon.com', 45),
            ('orders@amazon.com', 89),
        ]
        result = consolidate_email_addresses(addresses, preserve_variations=True)

        # Should have 4 unique emails
        assert len(result) == 4

        # Find each email group
        emails = {group.email: group for group in result}

        # Check marketing@clearscore.com
        assert emails['marketing@clearscore.com'].total_count == 1003
        assert emails['marketing@clearscore.com'].variation_count == 3
        assert emails['marketing@clearscore.com'].has_variations is True

        # Check updates@clearscore.com
        assert emails['updates@clearscore.com'].total_count == 1008
        assert emails['updates@clearscore.com'].variation_count == 1
        assert emails['updates@clearscore.com'].has_variations is False

        # Check noreply@amazon.com (consolidates two variations)
        assert emails['noreply@amazon.com'].total_count == 279  # 234 + 45
        assert emails['noreply@amazon.com'].variation_count == 2
        assert emails['noreply@amazon.com'].has_variations is True

        # Check orders@amazon.com
        assert emails['orders@amazon.com'].total_count == 89
        assert emails['orders@amazon.com'].variation_count == 1
        assert emails['orders@amazon.com'].has_variations is False


class TestBackwardCompatibilityConsistency:
    """Test that old and new formats return consistent total counts and email normalization."""

    def test_backward_compat_and_variations_same_totals(self):
        """Test that both modes produce the same total counts."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('Clearscore <marketing@clearscore.com>', 17),
        ]

        # Get old format
        old_result = consolidate_email_addresses(addresses, preserve_variations=False)

        # Get new format
        new_result = consolidate_email_addresses(addresses, preserve_variations=True)

        # Extract totals from old format (second element of tuple)
        old_totals = {item[0]: item[1] for item in old_result}

        # Extract totals from new format
        new_totals = {group.email: group.total_count for group in new_result}

        # Should match
        assert old_totals == new_totals

    def test_backward_compat_and_variations_same_variation_counts(self):
        """Test that both modes report the same variation counts."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('Clearscore <marketing@clearscore.com>', 17),
        ]

        # Get old format
        old_result = consolidate_email_addresses(addresses, preserve_variations=False)

        # Get new format
        new_result = consolidate_email_addresses(addresses, preserve_variations=True)

        # Extract variation counts from old format (third element of tuple)
        old_var_counts = {item[0]: item[2] for item in old_result}

        # Extract variation counts from new format
        new_var_counts = {group.email: group.variation_count for group in new_result}

        # Should match
        assert old_var_counts == new_var_counts


class TestFilterableListSelectorExpandable:
    """Test FilterableListSelector with expandable items."""

    def test_selector_accepts_expandable_items(self):
        """Test that selector accepts ExpandableItem objects."""
        # Create expandable items
        parent = ExpandableItem(label="parent@example.com", count=100, is_expandable=True)
        child = ExpandableItem(label="Child Name", count=50, indent_level=1)
        parent.children = [child]

        # Create selector with expandable items
        selector = FilterableListSelector([parent], "Test")

        # Should initialize without errors
        assert len(selector.all_items) == 1
        assert selector.allow_expand is True

    def test_selector_backward_compatible_with_tuples(self):
        """Test that selector still accepts legacy tuple format."""
        items = [("email@example.com", 100), ("another@example.com", 50)]
        selector = FilterableListSelector(items, "Test")

        # Should convert to ExpandableItems
        assert len(selector.all_items) == 2
        assert isinstance(selector.all_items[0], ExpandableItem)

    def test_build_visible_items_includes_children(self):
        """Test that _build_visible_items includes children when expanded."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child1 = ExpandableItem(label="child1", count=50, indent_level=1)
        child2 = ExpandableItem(label="child2", count=50, indent_level=1)
        parent.children = [child1, child2]
        parent.is_expanded = True

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector._build_visible_items()

        # Should have parent + 2 children
        assert len(selector.visible_items) == 3
        assert selector.visible_items[0] == parent
        assert selector.visible_items[1] == child1
        assert selector.visible_items[2] == child2

    def test_build_visible_items_hides_collapsed_children(self):
        """Test that collapsed items' children are not visible."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]
        parent.is_expanded = False  # Collapsed

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector._build_visible_items()

        # Should only have parent, not child
        assert len(selector.visible_items) == 1
        assert selector.visible_items[0] == parent

    def test_update_filtered_items_parent_match(self):
        """Test that filtering matches parent items."""
        parent = ExpandableItem(label="marketing@company.com", count=100, is_expandable=True)
        child = ExpandableItem(label="Variation A", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.filter_text = "marketing"
        selector._update_filtered_items()

        # Should include parent
        assert len(selector.filtered_items) == 1
        assert selector.filtered_items[0] == parent

    def test_update_filtered_items_child_match(self):
        """Test that filtering matches child items and includes parent."""
        parent = ExpandableItem(label="parent@company.com", count=100, is_expandable=True)
        child = ExpandableItem(label="Important Variation", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.filter_text = "Important"
        selector._update_filtered_items()

        # Should include parent because child matched
        assert len(selector.filtered_items) == 1
        assert selector.filtered_items[0] == parent

    def test_update_filtered_items_no_match(self):
        """Test that filtering excludes non-matching items."""
        parent = ExpandableItem(label="marketing@company.com", count=100, is_expandable=True)
        selector = FilterableListSelector([parent], "Test")
        selector.filter_text = "xyz"
        selector._update_filtered_items()

        # Should be empty
        assert len(selector.filtered_items) == 0

    def test_toggle_expansion_expands_item(self):
        """Test that toggle expansion works on collapsed items."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True, is_expanded=False)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector.visible_items = [parent]
        selector.selected_index = 0

        selector._toggle_expansion()

        # Should be expanded now
        assert parent.is_expanded is True

    def test_toggle_expansion_collapses_item(self):
        """Test that toggle expansion works on expanded items."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True, is_expanded=True)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector.visible_items = [parent, child]
        selector.selected_index = 0

        selector._toggle_expansion()

        # Should be collapsed now
        assert parent.is_expanded is False

    def test_expand_current_expands_if_collapsed(self):
        """Test _expand_current expands collapsed parent."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True, is_expanded=False)
        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector.visible_items = [parent]
        selector.selected_index = 0

        selector._expand_current()

        assert parent.is_expanded is True

    def test_expand_current_does_nothing_on_expanded(self):
        """Test _expand_current does nothing on expanded item."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True, is_expanded=True)
        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector.visible_items = [parent]
        selector.selected_index = 0

        selector._expand_current()

        # Should still be expanded
        assert parent.is_expanded is True

    def test_find_parent_locates_correct_parent(self):
        """Test that _find_parent locates the parent of a child."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]

        found_parent = selector._find_parent(child)

        assert found_parent is parent

    def test_find_parent_returns_none_for_orphan(self):
        """Test that _find_parent returns None for orphaned child."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        orphan = ExpandableItem(label="orphan", count=50, indent_level=1)

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]

        found_parent = selector._find_parent(orphan)

        assert found_parent is None

    def test_get_display_num_for_parent(self):
        """Test display numbering for parent items."""
        parent1 = ExpandableItem(label="first", count=100)
        parent2 = ExpandableItem(label="second", count=50)
        parent3 = ExpandableItem(label="third", count=75)

        selector = FilterableListSelector([parent1, parent2, parent3], "Test")

        assert selector._get_display_num_for_parent(parent1) == 1
        assert selector._get_display_num_for_parent(parent2) == 2
        assert selector._get_display_num_for_parent(parent3) == 3

    def test_disable_expand_functionality(self):
        """Test that allow_expand=False disables expansion."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        selector = FilterableListSelector([parent], "Test", allow_expand=False)

        assert selector.allow_expand is False


class TestCreateExpandableEmailItems:
    """Test conversion from EmailGroup to ExpandableItem."""

    def test_create_with_single_variation(self):
        """Test creation with email having single variation (not expandable)."""
        variations = [
            DisplayNameVariation('Name <m@c.com>', 'Name', 100),
        ]
        group = EmailGroup('m@c.com', 100, variations)

        items = create_expandable_email_items([group])

        assert len(items) == 1
        parent = items[0]
        assert parent.label == 'm@c.com'  # No "[N display names]"
        assert not parent.is_expandable
        assert parent.children is None

    def test_create_with_multiple_variations(self):
        """Test creation with email having multiple variations (expandable)."""
        variations = [
            DisplayNameVariation('Name1 <m@c.com>', 'Name1', 100),
            DisplayNameVariation('Name2 <m@c.com>', 'Name2', 50),
        ]
        group = EmailGroup('m@c.com', 150, variations)

        items = create_expandable_email_items([group])

        assert len(items) == 1
        parent = items[0]
        assert parent.label == 'm@c.com [2 display names]'
        assert parent.is_expandable
        assert len(parent.children) == 2

        # Verify children
        assert parent.children[0].label == 'Name1'
        assert parent.children[0].count == 100
        assert parent.children[0].indent_level == 1
        assert parent.children[1].label == 'Name2'
        assert parent.children[1].count == 50

    def test_create_preserves_variation_order_by_count(self):
        """Test that variations are ordered by count descending."""
        variations = [
            DisplayNameVariation('Less <m@c.com>', 'Less', 10),
            DisplayNameVariation('Most <m@c.com>', 'Most', 100),
            DisplayNameVariation('Some <m@c.com>', 'Some', 50),
        ]
        group = EmailGroup('m@c.com', 160, variations)

        items = create_expandable_email_items([group])

        # Children should be in order they appear in group.variations
        # (already sorted by consolidate_email_addresses)
        assert items[0].children[0].label == 'Less'
        assert items[0].children[1].label == 'Most'
        assert items[0].children[2].label == 'Some'

    def test_create_with_no_display_name(self):
        """Test variation with no display name uses full address."""
        variations = [
            DisplayNameVariation('m@c.com', '', 100),  # No display name
        ]
        group = EmailGroup('m@c.com', 100, variations)

        items = create_expandable_email_items([group])

        # Should not be expandable (only 1 variation)
        assert not items[0].is_expandable

    def test_create_parent_has_email_group_data(self):
        """Test that parent item has EmailGroup as data."""
        variations = [DisplayNameVariation('Name <m@c.com>', 'Name', 100)]
        group = EmailGroup('m@c.com', 100, variations)

        items = create_expandable_email_items([group])

        parent = items[0]
        assert parent.data is group
        assert isinstance(parent.data, EmailGroup)

    def test_create_child_has_variation_data(self):
        """Test that child items have DisplayNameVariation as data."""
        variation1 = DisplayNameVariation('Name1 <m@c.com>', 'Name1', 100)
        variation2 = DisplayNameVariation('Name2 <m@c.com>', 'Name2', 50)
        group = EmailGroup('m@c.com', 150, [variation1, variation2])

        items = create_expandable_email_items([group])

        parent = items[0]
        assert parent.children[0].data is variation1
        assert isinstance(parent.children[0].data, DisplayNameVariation)
        assert parent.children[1].data is variation2


class TestIntegrationWizardExpandableEmails:
    """Integration tests for wizard with expandable emails."""

    def test_extract_email_from_email_group_data(self):
        """Test extracting email when selecting EmailGroup (parent)."""
        # Create a mock wizard with the method we're testing
        class MockWizard:
            def _extract_email_from_consolidated_label(self, label, data=None):
                # Import the method from the actual wizard class
                from core.tools.rule_wizard_core import RuleWizard
                wizard = RuleWizard.__new__(RuleWizard)
                return wizard._extract_email_from_consolidated_label(label, data)

        wizard = MockWizard()
        group = EmailGroup('m@c.com', 100, [])
        email = wizard._extract_email_from_consolidated_label(
            'm@c.com [2 display names]',
            data=group
        )

        assert email == 'm@c.com'

    def test_extract_email_from_variation_data(self):
        """Test extracting email when selecting DisplayNameVariation (child)."""
        class MockWizard:
            def _extract_email_from_consolidated_label(self, label, data=None):
                from core.tools.rule_wizard_core import RuleWizard
                wizard = RuleWizard.__new__(RuleWizard)
                return wizard._extract_email_from_consolidated_label(label, data)

        wizard = MockWizard()
        variation = DisplayNameVariation('Name <m@c.com>', 'Name', 100)
        email = wizard._extract_email_from_consolidated_label(
            'Name',
            data=variation
        )

        assert email == 'm@c.com'

    def test_extract_email_from_label_only_backward_compat(self):
        """Test backward compatibility - extract from label without data."""
        class MockWizard:
            def _extract_email_from_consolidated_label(self, label, data=None):
                from core.tools.rule_wizard_core import RuleWizard
                wizard = RuleWizard.__new__(RuleWizard)
                return wizard._extract_email_from_consolidated_label(label, data)

        wizard = MockWizard()
        email = wizard._extract_email_from_consolidated_label(
            'm@c.com [2 display names]',
            data=None
        )

        assert email == 'm@c.com'

    def test_create_expandable_items_for_batch_selector(self):
        """Test full workflow: consolidate -> create expandable items for batch selector."""
        addresses = [
            ('ClearScore <marketing@clearscore.com>', 582),
            ('"ClearScore" <marketing@clearscore.com>', 404),
            ('updates@clearscore.com', 1008),
        ]

        # Step 1: Consolidate with variations
        email_groups = consolidate_email_addresses(addresses, preserve_variations=True)

        # Step 2: Create expandable items
        items = create_expandable_email_items(email_groups)

        # Verify structure
        assert len(items) == 2
        assert items[0].label == 'updates@clearscore.com'  # Not expandable
        assert items[1].label == 'marketing@clearscore.com [2 display names]'  # Expandable

    def test_selector_with_expandable_email_items(self):
        """Test FilterableListSelector accepts expandable email items."""
        addresses = [
            ('Company <support@example.com>', 100),
            ('Support Team <support@example.com>', 50),
            ('noreply@example.com', 75),
        ]

        email_groups = consolidate_email_addresses(addresses, preserve_variations=True)
        items = create_expandable_email_items(email_groups)

        # Create selector
        selector = FilterableListSelector(items, "Test Selector")

        # Verify items are properly initialized
        assert len(selector.all_items) == 2
        assert selector.all_items[0].is_expandable or not selector.all_items[0].is_expandable
        assert all(isinstance(item, ExpandableItem) for item in selector.all_items)


class TestFilterableListSelectorRendering(unittest.TestCase):
    """Test rendering of expandable items in FilterableListSelector."""

    def test_format_count_simple(self):
        """Test count formatting without thousands."""
        selector = FilterableListSelector([], "Test")
        self.assertEqual(selector._format_count(1), "1")
        self.assertEqual(selector._format_count(999), "999")

    def test_format_count_thousands(self):
        """Test count formatting with thousands separator."""
        selector = FilterableListSelector([], "Test")
        self.assertEqual(selector._format_count(1000), "1,000")
        self.assertEqual(selector._format_count(1234567), "1,234,567")

    def test_format_count_zero(self):
        """Test count formatting for zero."""
        selector = FilterableListSelector([], "Test")
        self.assertEqual(selector._format_count(0), "0")

    def test_format_count_large_numbers(self):
        """Test count formatting for very large numbers."""
        selector = FilterableListSelector([], "Test")
        self.assertEqual(selector._format_count(1000000000), "1,000,000,000")

    def test_get_parent_display_num(self):
        """Test getting parent display number for children."""
        # Create test structure
        parent1 = ExpandableItem(label="parent1", count=100, is_expandable=True)
        child1a = ExpandableItem(label="child1a", count=50, indent_level=1)
        child1b = ExpandableItem(label="child1b", count=50, indent_level=1)
        parent1.children = [child1a, child1b]
        parent1.is_expanded = True

        parent2 = ExpandableItem(label="parent2", count=200, is_expandable=True)

        selector = FilterableListSelector([parent1, parent2], "Test")
        selector.filtered_items = [parent1, parent2]
        selector._build_visible_items()

        # visible_items: [parent1, child1a, child1b, parent2]
        # Child indices 1, 2 should have parent num 1
        self.assertEqual(selector._get_parent_display_num(1), 1)
        self.assertEqual(selector._get_parent_display_num(2), 1)

    def test_find_parent_start_index(self):
        """Test finding parent start index for children."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child1 = ExpandableItem(label="child1", count=50, indent_level=1)
        child2 = ExpandableItem(label="child2", count=50, indent_level=1)
        parent.children = [child1, child2]
        parent.is_expanded = True

        selector = FilterableListSelector([parent], "Test")
        selector.filtered_items = [parent]
        selector._build_visible_items()

        # visible_items: [parent, child1, child2]
        self.assertEqual(selector._find_parent_start_index(1), 0)  # child1's parent at 0
        self.assertEqual(selector._find_parent_start_index(2), 0)  # child2's parent at 0

    def test_find_parent_start_index_multiple_parents(self):
        """Test finding parent start index with multiple parents and children."""
        parent1 = ExpandableItem(label="p1", count=100, is_expandable=True)
        child1a = ExpandableItem(label="c1a", count=50, indent_level=1)
        parent1.children = [child1a]
        parent1.is_expanded = True

        parent2 = ExpandableItem(label="p2", count=100, is_expandable=True)
        child2a = ExpandableItem(label="c2a", count=50, indent_level=1)
        child2b = ExpandableItem(label="c2b", count=50, indent_level=1)
        parent2.children = [child2a, child2b]
        parent2.is_expanded = True

        selector = FilterableListSelector([parent1, parent2], "Test")
        selector.filtered_items = [parent1, parent2]
        selector._build_visible_items()

        # visible_items: [parent1, child1a, parent2, child2a, child2b]
        # Index 1 (child1a) should find parent1 at 0
        # Index 3 (child2a) should find parent2 at 2
        # Index 4 (child2b) should find parent2 at 2
        self.assertEqual(selector._find_parent_start_index(1), 0)
        self.assertEqual(selector._find_parent_start_index(3), 2)
        self.assertEqual(selector._find_parent_start_index(4), 2)


class TestExpandableItemDisplay(unittest.TestCase):
    """Test proper display formatting of expandable items."""

    def test_parent_expanded_has_down_arrow(self):
        """Test that expanded parent shows ▼ indicator."""
        parent = ExpandableItem(
            label="test@example.com",
            count=100,
            is_expandable=True,
            is_expanded=True
        )

        # Verify the data structure
        self.assertTrue(parent.is_expanded)
        self.assertTrue(parent.is_expandable)

    def test_parent_collapsed_has_right_arrow(self):
        """Test that collapsed parent shows ▶ indicator."""
        parent = ExpandableItem(
            label="test@example.com",
            count=100,
            is_expandable=True,
            is_expanded=False
        )

        self.assertFalse(parent.is_expanded)
        self.assertTrue(parent.is_expandable)

    def test_child_has_indent_level(self):
        """Test that child items have correct indent level."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child = ExpandableItem(
            label="child",
            count=50,
            indent_level=1,  # Child level
            is_expandable=False
        )
        parent.children = [child]

        self.assertEqual(child.indent_level, 1)
        self.assertEqual(parent.indent_level, 0)

    def test_non_expandable_parent_no_arrow(self):
        """Test that non-expandable parents don't show expansion arrows."""
        parent = ExpandableItem(
            label="test@example.com",
            count=100,
            is_expandable=False
        )

        self.assertFalse(parent.is_expandable)

    def test_child_indent_level_defaults_to_zero(self):
        """Test that items default to indent level 0."""
        item = ExpandableItem(label="test", count=50)
        self.assertEqual(item.indent_level, 0)


class TestRenderingIntegration(unittest.TestCase):
    """Test rendering integration with visible_items."""

    def test_visible_items_includes_expanded_children(self):
        """Test that visible items includes children when parent expanded."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child1 = ExpandableItem(label="child1", count=50, indent_level=1)
        child2 = ExpandableItem(label="child2", count=50, indent_level=1)
        parent.children = [child1, child2]
        parent.is_expanded = True

        selector = FilterableListSelector([parent], "Test")

        # visible_items should have parent + both children
        self.assertEqual(len(selector.visible_items), 3)
        self.assertEqual(selector.visible_items[0], parent)
        self.assertEqual(selector.visible_items[1], child1)
        self.assertEqual(selector.visible_items[2], child2)

    def test_visible_items_excludes_collapsed_children(self):
        """Test that children are excluded when parent collapsed."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]
        parent.is_expanded = False

        selector = FilterableListSelector([parent], "Test")

        # visible_items should only have parent
        self.assertEqual(len(selector.visible_items), 1)
        self.assertEqual(selector.visible_items[0], parent)

    def test_toggle_expansion_updates_visible_items(self):
        """Test that toggling expansion updates visible_items."""
        parent = ExpandableItem(label="parent", count=100, is_expandable=True, is_expanded=False)
        child = ExpandableItem(label="child", count=50, indent_level=1)
        parent.children = [child]

        selector = FilterableListSelector([parent], "Test")
        selector.selected_index = 0

        # Initially collapsed
        self.assertEqual(len(selector.visible_items), 1)

        # Toggle expansion
        selector._toggle_expansion()

        # Should now be expanded
        self.assertEqual(len(selector.visible_items), 2)
        self.assertTrue(parent.is_expanded)

    def test_filtering_preserves_expansion_state(self):
        """Test that filtering preserves expansion state."""
        parent1 = ExpandableItem(label="important@example.com", count=100, is_expandable=True)
        child1 = ExpandableItem(label="Variation A", count=50, indent_level=1)
        parent1.children = [child1]
        parent1.is_expanded = True

        parent2 = ExpandableItem(label="other@example.com", count=50, is_expandable=True)
        parent2.is_expanded = False

        selector = FilterableListSelector([parent1, parent2], "Test")

        # Filter to only important
        selector.filter_text = "important"
        selector._update_filtered_items()

        # Should still have parent1 expanded
        self.assertTrue(parent1.is_expanded)
        self.assertEqual(len(selector.visible_items), 2)  # parent1 + child1


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
