#!/usr/bin/env python3
"""Test script for FilterableListSelector widget."""

import curses
import sys
from core.tools.rule_wizard_core import FilterableListSelector


def main():
    """Run a test of the FilterableListSelector."""
    # Create test data
    test_items = [
        ("INBOX", 1234),
        ("INBOX/Work", 567),
        ("INBOX/Personal", 890),
        ("Sent", 2341),
        ("Drafts", 89),
        ("Trash", 456),
        ("Archive", 12345),
        ("Archive/2023", 5678),
        ("Archive/2024", 3456),
        ("Spam", 999),
        ("Important", 123),
        ("Updates", 4567),
        ("Promotions", 8901),
        ("Social", 2345),
        ("Forums", 678),
    ]

    # Create and run selector
    selector = FilterableListSelector(test_items, "Select Email Folder")

    try:
        result = curses.wrapper(selector.run)
        if result:
            print(f"\nYou selected: {result}")
        else:
            print("\nSelection cancelled")
        return 0
    except Exception as e:
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
