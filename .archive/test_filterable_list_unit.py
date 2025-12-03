#!/usr/bin/env python3
"""Unit tests for FilterableListSelector."""

import unittest
from unittest.mock import Mock, patch, MagicMock
from core.tools.rule_wizard_core import FilterableListSelector, format_count


class TestFormatCount(unittest.TestCase):
    """Test the format_count helper function."""

    def test_format_small_numbers(self):
        """Test formatting numbers under 1000."""
        self.assertEqual(format_count(0), "0")
        self.assertEqual(format_count(1), "1")
        self.assertEqual(format_count(999), "999")

    def test_format_thousands(self):
        """Test formatting numbers with thousand separators."""
        self.assertEqual(format_count(1000), "1,000")
        self.assertEqual(format_count(1234), "1,234")
        self.assertEqual(format_count(12345), "12,345")
        self.assertEqual(format_count(123456), "123,456")
        self.assertEqual(format_count(1234567), "1,234,567")


class TestFilterableListSelector(unittest.TestCase):
    """Test the FilterableListSelector widget."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_items = [
            ("INBOX", 100),
            ("Sent", 50),
            ("Drafts", 25),
            ("Spam", 200),
            ("Archive", 1000),
        ]
        self.selector = FilterableListSelector(self.test_items, "Test Selector")

    def test_initialization(self):
        """Test that selector initializes correctly."""
        self.assertEqual(self.selector.title, "Test Selector")
        self.assertEqual(self.selector.all_items, self.test_items)
        self.assertEqual(self.selector.filtered_items, self.test_items)
        self.assertEqual(self.selector.filter_text, "")
        self.assertEqual(self.selector.selected_index, 0)
        self.assertEqual(self.selector.scroll_offset, 0)

    def test_filter_no_text(self):
        """Test that empty filter shows all items."""
        self.selector.filter_text = ""
        self.selector._update_filtered_items()
        self.assertEqual(len(self.selector.filtered_items), len(self.test_items))

    def test_filter_case_insensitive(self):
        """Test that filtering is case-insensitive."""
        self.selector.filter_text = "inbox"
        self.selector._update_filtered_items()
        self.assertEqual(len(self.selector.filtered_items), 1)
        self.assertEqual(self.selector.filtered_items[0][0], "INBOX")

        self.selector.filter_text = "INBOX"
        self.selector._update_filtered_items()
        self.assertEqual(len(self.selector.filtered_items), 1)
        self.assertEqual(self.selector.filtered_items[0][0], "INBOX")

    def test_filter_substring_matching(self):
        """Test substring matching in filter."""
        self.selector.filter_text = "a"
        self.selector._update_filtered_items()
        # Should match "Spam" and "Archive" and "Drafts"
        self.assertEqual(len(self.selector.filtered_items), 3)
        labels = [item[0] for item in self.selector.filtered_items]
        self.assertIn("Spam", labels)
        self.assertIn("Archive", labels)
        self.assertIn("Drafts", labels)

    def test_filter_no_matches(self):
        """Test filtering with no matches."""
        self.selector.filter_text = "xyz123"
        self.selector._update_filtered_items()
        self.assertEqual(len(self.selector.filtered_items), 0)

    def test_filter_resets_selection(self):
        """Test that filtering resets selection to 0."""
        self.selector.selected_index = 3
        self.selector.filter_text = "sent"
        self.selector._update_filtered_items()
        self.assertEqual(self.selector.selected_index, 0)

    def test_handle_key_down_navigation(self):
        """Test DOWN arrow key navigation."""
        import curses
        self.assertIsNone(self.selector._handle_key(curses.KEY_DOWN))
        self.assertEqual(self.selector.selected_index, 1)

        self.assertIsNone(self.selector._handle_key(curses.KEY_DOWN))
        self.assertEqual(self.selector.selected_index, 2)

    def test_handle_key_up_navigation(self):
        """Test UP arrow key navigation."""
        import curses
        self.selector.selected_index = 2
        self.assertIsNone(self.selector._handle_key(curses.KEY_UP))
        self.assertEqual(self.selector.selected_index, 1)

    def test_handle_key_up_at_top(self):
        """Test UP arrow at top stays at 0."""
        import curses
        self.selector.selected_index = 0
        self.assertIsNone(self.selector._handle_key(curses.KEY_UP))
        self.assertEqual(self.selector.selected_index, 0)

    def test_handle_key_down_at_bottom(self):
        """Test DOWN arrow at bottom stays at last item."""
        import curses
        self.selector.selected_index = len(self.test_items) - 1
        self.assertIsNone(self.selector._handle_key(curses.KEY_DOWN))
        self.assertEqual(self.selector.selected_index, len(self.test_items) - 1)

    def test_handle_key_home(self):
        """Test HOME key jumps to first item."""
        import curses
        self.selector.selected_index = 3
        self.assertIsNone(self.selector._handle_key(curses.KEY_HOME))
        self.assertEqual(self.selector.selected_index, 0)

    def test_handle_key_end(self):
        """Test END key jumps to last item."""
        import curses
        self.selector.selected_index = 0
        self.assertIsNone(self.selector._handle_key(curses.KEY_END))
        self.assertEqual(self.selector.selected_index, len(self.test_items) - 1)

    def test_handle_key_enter_selects(self):
        """Test ENTER key returns selected item."""
        import curses
        self.selector.selected_index = 1
        result = self.selector._handle_key(curses.KEY_ENTER)
        self.assertEqual(result, "Sent")

    def test_handle_key_escape_cancels(self):
        """Test ESC key returns empty string (cancel)."""
        result = self.selector._handle_key(27)
        self.assertEqual(result, "")

    def test_handle_key_printable_adds_to_filter(self):
        """Test that printable characters are added to filter."""
        self.selector._handle_key(ord("i"))
        self.assertEqual(self.selector.filter_text, "i")

        self.selector._handle_key(ord("n"))
        self.assertEqual(self.selector.filter_text, "in")

    def test_handle_key_backspace_removes_character(self):
        """Test backspace removes last filter character."""
        import curses
        self.selector.filter_text = "test"
        self.selector._update_filtered_items()

        self.selector._handle_key(curses.KEY_BACKSPACE)
        self.assertEqual(self.selector.filter_text, "tes")

        self.selector._handle_key(127)  # DEL key
        self.assertEqual(self.selector.filter_text, "te")

    def test_handle_key_backspace_on_empty_filter(self):
        """Test backspace on empty filter does nothing."""
        import curses
        self.selector.filter_text = ""
        self.selector._handle_key(curses.KEY_BACKSPACE)
        self.assertEqual(self.selector.filter_text, "")

    def test_handle_key_page_down(self):
        """Test PAGE DOWN key navigation."""
        import curses
        self.selector.selected_index = 0
        self.selector._handle_key(curses.KEY_NPAGE)
        self.assertTrue(self.selector.selected_index > 0)

    def test_handle_key_page_up(self):
        """Test PAGE UP key navigation."""
        import curses
        self.selector.selected_index = 4
        self.selector._handle_key(curses.KEY_PPAGE)
        self.assertTrue(self.selector.selected_index < 4)

    def test_scroll_offset_follows_selection_down(self):
        """Test scroll offset adjusts when navigating down."""
        import curses

        # Create a longer list to test scrolling
        long_items = [(f"Item{i}", i*10) for i in range(30)]
        selector = FilterableListSelector(long_items, "Test")

        mock_stdscr = Mock()
        mock_stdscr.getmaxyx.return_value = (24, 80)  # Small terminal

        # Simulate the selection moving down beyond visible area
        for _ in range(20):
            selector._handle_key(curses.KEY_DOWN)

        # Render to trigger scroll offset calculation
        selector._render(mock_stdscr)
        # Verify scroll offset has moved to follow selection
        self.assertGreater(selector.scroll_offset, 0)


class TestFilterableListSelectorIntegration(unittest.TestCase):
    """Integration tests for FilterableListSelector with curses mocking."""

    def test_render_basic_structure(self):
        """Test that render creates expected screen structure."""
        test_items = [("Item1", 10), ("Item2", 20)]
        selector = FilterableListSelector(test_items, "Test")

        mock_stdscr = Mock()
        mock_stdscr.getmaxyx.return_value = (24, 80)

        selector._render(mock_stdscr)

        # Verify basic calls were made
        mock_stdscr.erase.assert_called_once()
        mock_stdscr.refresh.assert_called_once()
        # Check that addnstr was called multiple times (title, filter, items, help)
        self.assertGreater(mock_stdscr.addnstr.call_count, 3)

    def test_render_with_filter(self):
        """Test render displays filter text correctly."""
        test_items = [("INBOX", 100), ("Sent", 50)]
        selector = FilterableListSelector(test_items, "Test")
        selector.filter_text = "inbox"
        selector._update_filtered_items()

        mock_stdscr = Mock()
        mock_stdscr.getmaxyx.return_value = (24, 80)

        selector._render(mock_stdscr)

        # Verify that addnstr was called with filter text
        # Check all calls for one that contains the filter text
        calls_str = str(mock_stdscr.addnstr.call_args_list)
        self.assertIn("Filter:", calls_str)
        self.assertIn("inbox", calls_str)


if __name__ == "__main__":
    unittest.main()
