#!/usr/bin/env python3
"""Core components for the IMAPFilter rule creation wizard.

This module provides interactive widgets for building rules, including
a filterable list selector with real-time search capabilities, and
a cache query engine for extracting header statistics.
"""
from __future__ import annotations

import curses
import email
import imaplib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from tqdm import tqdm

from core.config import DEFAULT_DATA_DIR
from core.imap_client import imap_login, list_all_folders
from core.logging_utils import JsonLogger
from core.keywords import KeywordManager
from core.rule_utils import slugify, generate_filename
from core.ui_components import prompt_yes_no, format_count
from core.tools.coverage_analyzer import (
    RuleCoverageAnalyzer,
    DomainCluster,
    BatchTarget,
    _decode_mime_header,
)


@dataclass
class DisplayNameVariation:
    """Represents a single display name variation for an email address.

    This tracks a specific variation of how an email address appears in messages,
    including the full address with display name and the count of messages using it.

    Attributes:
        full_address: The complete email address string with display name
                     (e.g., "ClearScore <marketing@clearscore.com>")
        display_name: The extracted display name portion (e.g., "ClearScore")
        count: Number of messages with this exact variation
    """
    full_address: str
    display_name: str
    count: int


@dataclass
class EmailGroup:
    """Consolidates a single email address with all its display name variations.

    This groups together an email address and all the different ways it appears
    in messages (with different display names), tracking the total count and
    individual variation counts.

    Attributes:
        email: The normalized email address (e.g., "marketing@clearscore.com")
        total_count: Total number of messages from this email (sum of all variations)
        variations: List of DisplayNameVariation objects for this email
    """
    email: str
    total_count: int
    variations: List[DisplayNameVariation]

    @property
    def variation_count(self) -> int:
        """Get the number of different display name variations for this email.

        Returns:
            Number of distinct variations (len of variations list)
        """
        return len(self.variations)

    @property
    def has_variations(self) -> bool:
        """Check if this email has multiple display name variations.

        Returns:
            True if more than one variation exists, False otherwise
        """
        return len(self.variations) > 1


@dataclass
class ExpandableItem:
    """Wrapper for items that can be expanded/collapsed in UI displays.

    This represents a single item in an expandable list, which can have child items
    that are shown/hidden based on the expanded state. Used by the filterable selector
    to display hierarchical data like expandable email groups with their variations.

    Attributes:
        label: Display text for the item
        count: Number associated with the item (e.g., message count)
        is_expandable: Whether this item can be expanded to show children
        is_expanded: Current expanded/collapsed state
        children: List of child ExpandableItem objects (None if no children)
        parent_index: Index of parent item in the parent list (None if root level)
        data: Optional data object associated with this item (e.g., EmailGroup)
        indent_level: Nesting level for indentation in display (0 for root)
    """
    label: str
    count: int
    is_expandable: bool = False
    is_expanded: bool = False
    children: Optional[List['ExpandableItem']] = None
    parent_index: Optional[int] = None
    data: Optional[Any] = None
    indent_level: int = 0


class FilterableListSelector:
    """Interactive curses widget for selecting items from a list with real-time filtering.

    This widget displays a searchable, scrollable list where users can:
    - Type to filter items in real-time (case-insensitive substring matching)
    - Navigate with arrow keys
    - Select an item with Enter
    - Cancel with ESC
    - Expand/collapse hierarchical items with Space/arrow keys (when allow_expand=True)

    Example:
        items = [("INBOX", 1234), ("Sent", 567), ("Drafts", 89)]
        selector = FilterableListSelector(items, "Select Folder")
        result = curses.wrapper(selector.run)
        if result:
            print(f"Selected: {result}")
    """

    def __init__(
        self,
        items: Union[List[Tuple[str, int]], List[ExpandableItem]],
        title: str,
        allow_expand: bool = True
    ):
        """Initialize the filterable list selector.

        Args:
            items: List of (label, count) tuples OR ExpandableItem objects
            title: Display title for the selector
            allow_expand: Enable expand/collapse functionality (default: True)
        """
        self.allow_expand = allow_expand
        self.title = title
        self.filter_text = ""

        # Normalize items to ExpandableItem format
        if items and not isinstance(items[0], ExpandableItem):
            # Legacy tuple format - convert
            self.all_items = [
                ExpandableItem(label=label, count=count)
                for label, count in items
            ]
        else:
            self.all_items = items if items else []

        self.filtered_items: List[ExpandableItem] = []
        self.visible_items: List[ExpandableItem] = []  # Flattened view with expansions
        self.selected_index = 0
        self.scroll_offset = 0

        # Initialize filtered items
        self._update_filtered_items()

    def _build_visible_items(self) -> None:
        """Build the flat list of visible items based on expansion states.

        This method flattens the hierarchical structure by inserting children
        after their expanded parents. It's called whenever:
        - Filter changes
        - Item expanded/collapsed
        - Initial render
        """
        self.visible_items = []

        for item in self.filtered_items:
            # Add parent item
            self.visible_items.append(item)

            # Add children if expanded
            if item.is_expanded and item.children:
                for child in item.children:
                    self.visible_items.append(child)

    def _update_filtered_items(self) -> None:
        """Update filtered_items based on current filter_text.

        Enhanced to handle expandable items:
        - Filter matches against parent and child labels
        - If child matches, include parent too
        - Preserve expansion state after filtering
        """
        if not self.filter_text:
            self.filtered_items = list(self.all_items)
        else:
            filter_lower = self.filter_text.lower()
            matched_items = []

            for item in self.all_items:
                # Check if parent matches
                parent_matches = filter_lower in item.label.lower()

                # Check if any child matches
                child_matches = False
                if item.children:
                    child_matches = any(
                        filter_lower in child.label.lower()
                        for child in item.children
                    )

                # Include if parent OR any child matches
                if parent_matches or child_matches:
                    matched_items.append(item)

            self.filtered_items = matched_items

        # Rebuild visible items and reset selection
        self._build_visible_items()
        self.selected_index = 0
        self.scroll_offset = 0

    def _toggle_expansion(self) -> None:
        """Toggle expand/collapse for current item."""
        if not (0 <= self.selected_index < len(self.visible_items)):
            return

        item = self.visible_items[self.selected_index]

        # Only parent items can be expanded
        if item.indent_level > 0:
            return  # Child item, can't expand

        if not item.is_expandable:
            return  # No children to show

        # Toggle expansion
        item.is_expanded = not item.is_expanded

        # Rebuild visible list
        self._build_visible_items()

    def _expand_current(self) -> None:
        """Expand current item if possible."""
        if not (0 <= self.selected_index < len(self.visible_items)):
            return

        item = self.visible_items[self.selected_index]

        if item.indent_level == 0 and item.is_expandable and not item.is_expanded:
            item.is_expanded = True
            self._build_visible_items()

    def _collapse_current(self) -> None:
        """Collapse current item if possible."""
        if not (0 <= self.selected_index < len(self.visible_items)):
            return

        item = self.visible_items[self.selected_index]

        # Can collapse if:
        # 1. Currently on expanded parent
        # 2. Currently on a child (collapse its parent)

        if item.indent_level > 0:
            # Find and collapse parent
            parent = self._find_parent(item)
            if parent and parent.is_expanded:
                parent.is_expanded = False
                # Move selection to parent
                self._build_visible_items()
                # Find parent's new index
                self.selected_index = self.visible_items.index(parent)

        elif item.is_expanded:
            # Collapse current parent
            item.is_expanded = False
            self._build_visible_items()

    def _find_parent(self, child_item: ExpandableItem) -> Optional[ExpandableItem]:
        """Find the parent item for a given child item."""
        for item in self.filtered_items:
            if item.children and child_item in item.children:
                return item
        return None

    def _get_display_num_for_parent(self, parent_item: ExpandableItem) -> int:
        """Get the display number for a parent item (1-indexed position among parents)."""
        parent_count = 0
        for item in self.filtered_items:
            parent_count += 1
            if item is parent_item:
                return parent_count
        return parent_count

    def _get_parent_display_num(self, child_index: int) -> int:
        """Get the parent's display number for a child at given visible index."""
        # Walk backward to find parent
        for i in range(child_index - 1, -1, -1):
            if self.visible_items[i].indent_level == 0:
                return self._get_display_num_for_parent(self.visible_items[i])
        return 1

    def _find_parent_start_index(self, child_index: int) -> int:
        """Find the visible index of the parent item for a given child.

        Args:
            child_index: Index in visible_items of a child item

        Returns:
            The index of the parent item in visible_items, or 0 if not found
        """
        # Walk backward to find parent
        for i in range(child_index - 1, -1, -1):
            if self.visible_items[i].indent_level == 0:
                return i
        return 0

    def _format_count(self, count: int) -> str:
        """Format count with thousands separator.

        Examples:
            1 -> "1"
            1234 -> "1,234"
            1234567 -> "1,234,567"

        Args:
            count: The number to format

        Returns:
            Formatted string with thousands separators
        """
        return "{:,}".format(count)

    def _render(self, stdscr: Any) -> None:
        """Render the current state of the widget.

        Args:
            stdscr: The curses screen object
        """
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Calculate display area (80% of terminal height for list)
        header_height = 4  # Title, filter input, filter status, blank line
        footer_height = 1   # Help text
        max_list_height = max(1, int((height - header_height - footer_height) * 0.8))

        current_row = 0

        # Line 0: Title with item count
        title_text = f"{self.title} ({self._format_count(len(self.all_items))} items)"
        stdscr.addnstr(current_row, 0, title_text, width - 1, curses.A_BOLD)
        current_row += 1

        # Line 1: Filter input field
        filter_display = f"Filter: {self.filter_text}"
        stdscr.addnstr(current_row, 0, filter_display, width - 1)
        current_row += 1

        # Line 2: Filtered count indicator
        if self.filter_text:
            filter_status = f"[showing {len(self.filtered_items)} of {len(self.all_items)}]"
        else:
            filter_status = f"[all {len(self.all_items)} items]"
        stdscr.addnstr(current_row, 0, filter_status, width - 1, curses.A_DIM)
        current_row += 1

        # Line 3: Blank separator
        current_row += 1

        # Calculate actual list height (may be less than max if fewer items)
        list_start_row = current_row
        list_height = min(max_list_height, len(self.visible_items))

        # Adjust scroll offset to keep selection visible
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + list_height:
            self.scroll_offset = self.selected_index - list_height + 1

        # Render visible items
        for offset in range(list_height):
            index = self.scroll_offset + offset
            if index >= len(self.visible_items):
                break

            item = self.visible_items[index]

            # Build expansion indicator
            expansion_marker = ""
            if item.indent_level == 0:  # Parent items only
                if item.is_expandable:
                    if item.is_expanded:
                        expansion_marker = " ▼"  # Expanded
                    else:
                        expansion_marker = " ▶"  # Collapsed

            # Build indentation
            indent = "   " * item.indent_level  # 3 spaces per indent level

            # Format item text with item number and count
            if item.indent_level > 0:
                # Child item - use parent number + letter (1a, 1b, 1c, etc.)
                parent_num = self._get_parent_display_num(index)
                child_offset = index - self._find_parent_start_index(index) - 1
                letter = chr(ord('a') + child_offset)
                item_num = f"{parent_num}{letter}"
            else:
                # Parent item - use parent number (1, 2, 3, etc.)
                item_num = str(self._get_display_num_for_parent(item))

            # Format count with thousands separator
            count_str = f"({self._format_count(item.count)})"

            # Build display text
            display_text = f"{indent}{item_num:>3}. {item.label}{expansion_marker} {count_str}"

            # Add selection marker
            if index == self.selected_index:
                marker = ">"
                display_text = f"{marker} {display_text}"
                attr = curses.A_REVERSE
            else:
                display_text = f"  {display_text}"
                attr = curses.A_NORMAL

            # Render the line (truncate to terminal width)
            row = list_start_row + offset
            try:
                stdscr.addnstr(row, 0, display_text, width - 1, attr)
            except curses.error:
                # Handle edge case where terminal is too narrow or Unicode error
                try:
                    simplified = display_text.replace('▼', 'v').replace('▶', '>')
                    stdscr.addnstr(row, 0, simplified, width - 1, attr)
                except curses.error:
                    pass  # Give up on rendering this line

        # Footer: Help text
        if self.allow_expand:
            help_text = "↑/↓ navigate  Space/→/← expand  Enter select  Type filter  Backspace delete  ESC cancel"
        else:
            help_text = "↑/↓ navigate  Enter select  Type to filter  Backspace delete  ESC cancel"
        stdscr.addnstr(height - 1, 0, help_text, width - 1, curses.A_DIM)

        stdscr.refresh()

    def _handle_key(self, key: int) -> Optional[Tuple[str, Any]]:
        """Handle a keypress and return selection if complete.

        Args:
            key: The curses key code

        Returns:
            Tuple of (label, data) if Enter was pressed, None if still navigating,
            empty string "" if cancelled
        """
        # Navigation keys (arrow keys only - allow all letters for filter typing)
        if key == curses.KEY_UP:
            if self.selected_index > 0:
                self.selected_index -= 1

        elif key == curses.KEY_DOWN:
            if self.selected_index < len(self.visible_items) - 1:
                self.selected_index += 1

        elif key == curses.KEY_HOME:
            self.selected_index = 0

        elif key == curses.KEY_END:
            self.selected_index = max(0, len(self.visible_items) - 1)

        elif key == curses.KEY_PPAGE:  # Page Up
            # Move up by ~10 items
            self.selected_index = max(0, self.selected_index - 10)

        elif key == curses.KEY_NPAGE:  # Page Down
            # Move down by ~10 items
            self.selected_index = min(
                len(self.visible_items) - 1,
                self.selected_index + 10
            )

        # Expand/collapse keys
        elif self.allow_expand and key == ord(' '):  # Space bar
            self._toggle_expansion()

        elif self.allow_expand and key == curses.KEY_RIGHT:  # Right arrow
            self._expand_current()

        elif self.allow_expand and key == curses.KEY_LEFT:  # Left arrow
            self._collapse_current()

        # Selection/Cancel keys
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            # Return selected item
            if self.visible_items and 0 <= self.selected_index < len(self.visible_items):
                selected_item = self.visible_items[self.selected_index]
                # Return tuple: (label, data) to allow accessing original objects
                return (selected_item.label, selected_item.data)
            return ""  # No selection available

        elif key == 27:  # ESC
            return ""  # Cancelled

        # Backspace - remove last filter character
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self._update_filtered_items()

        # Printable characters - add to filter
        elif 32 <= key <= 126:
            char = chr(key)
            self.filter_text += char
            self._update_filtered_items()

        return None  # Continue event loop

    def run(self, stdscr: Any) -> Optional[Union[str, Tuple[str, Any]]]:
        """Show the UI and return the selected item or None if cancelled.

        This is the main entry point that should be called via curses.wrapper():
            result = curses.wrapper(selector.run)

        Args:
            stdscr: The curses screen object (provided by curses.wrapper)

        Returns:
            For legacy tuple items: returns the selected item label (str)
            For ExpandableItem objects: returns (label, data) tuple
            None if cancelled (ESC pressed)
        """
        # Initialize curses settings
        curses.curs_set(0)  # Hide cursor
        stdscr.keypad(True)  # Enable keypad mode for special keys

        # Try to use default colors for better terminal compatibility
        try:
            curses.use_default_colors()
        except curses.error:
            # Not all terminals support this, ignore if it fails
            pass

        # Initialize filtered items
        self._update_filtered_items()

        # Main event loop
        while True:
            self._render(stdscr)

            # Get key input
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                return None

            # Handle the key and check if we have a result
            result = self._handle_key(key)

            if result is not None:
                # Empty string means cancelled, return None
                if result == "":
                    return None
                # Otherwise return the result (label or tuple)
                return result


def safe_parse_header(data: str) -> dict[str, str]:
    """
    Parse header data from cache into a lowercase-keyed dictionary.

    Handles both raw headers and JSON-wrapped headers gracefully.
    Returns empty dict on any parsing errors.

    Args:
        data: Raw data from cache (JSON string or raw header)

    Returns:
        Dictionary with lowercase header keys and their values
    """
    if not data:
        return {}

    try:
        # Try to extract raw header from JSON wrapper
        payload = json.loads(data)
        if isinstance(payload, dict):
            raw_header = payload.get("header", "")
            if isinstance(raw_header, str):
                data = raw_header
    except json.JSONDecodeError:
        # Data is already raw header, use as-is
        pass
    except Exception:
        # Any other error, return empty
        return {}

    try:
        # Parse email headers
        message = email.message_from_string(data)
        return {key.lower(): value for key, value in message.items()}
    except Exception:
        # Malformed header, return empty
        return {}


class CacheQueryEngine:
    """
    SQLite query engine for extracting header data with message counts from cache.

    Provides methods to extract unique header values (From, To, Subject, etc.)
    with their occurrence counts, and to count messages matching patterns.

    The cache database contains a 'headers' table with columns:
    - folder: IMAP folder name
    - uid: Message UID
    - data: JSON string containing {"header": "raw_email_headers"}
    - updated_at: Last update timestamp
    """

    def __init__(self, db_path: Path, show_progress: bool = True):
        """
        Initialize the query engine and connect to the cache database.

        Args:
            db_path: Path to the SQLite cache database file
            show_progress: Whether to display progress bars (default: True)

        Raises:
            sqlite3.Error: If database connection fails
        """
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.show_progress = show_progress

        # Configure SQLite pragmas for better read performance on large databases
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA cache_size = -64000")  # 64MB cache
        cursor.execute("PRAGMA mmap_size = 536870912")  # 512MB memory-mapped I/O
        cursor.execute("PRAGMA temp_store = MEMORY")  # Use memory for temp storage
        cursor.execute("PRAGMA journal_mode = WAL")  # Write-ahead logging for better concurrency

        # Create indexes for common queries if they don't exist
        self._create_indexes()

    def _create_indexes(self):
        """Create performance indexes on cache tables.

        Indexes improve query performance for coverage analysis and pattern matching.
        This is called once per wizard session and is idempotent (safe to call multiple times).
        """
        cursor = self.conn.cursor()

        try:
            # Index for folder/uid lookups (used in coverage analysis)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_headers_folder_uid
                ON headers(folder, uid)
            """)

            # Index for status/folder lookups (used in actions table queries)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_actions_status_folder
                ON actions(status, folder)
            """)

            self.conn.commit()
        except sqlite3.Error as e:
            # Indexes might already exist, which is fine
            pass

    def _get_total_count(self) -> int:
        """Get total number of headers in cache for progress tracking.

        Returns:
            Total count of headers, or 0 on error
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM headers")
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception:
            return 0

    def extract_unique_from_addresses(self, limit: int = 1000) -> List[Tuple[str, int]]:
        """
        Extract unique From addresses with message counts.

        Args:
            limit: Maximum number of results to return (default: 1000)

        Returns:
            List of (address, count) tuples, sorted by count (descending)
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc="📧 Extracting from addresses",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        counter = Counter()
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            from_addr = header.get("from", "").strip()
            if from_addr:
                counter[from_addr] += 1

        return counter.most_common(limit)

    def extract_unique_to_addresses(self, limit: int = 1000) -> List[Tuple[str, int]]:
        """
        Extract unique To addresses with message counts.

        Args:
            limit: Maximum number of results to return (default: 1000)

        Returns:
            List of (address, count) tuples, sorted by count (descending)
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc="📬 Extracting to addresses",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        counter = Counter()
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            to_addr = header.get("to", "").strip()
            if to_addr:
                counter[to_addr] += 1

        return counter.most_common(limit)

    def extract_unique_subjects(self, limit: int = 100) -> List[Tuple[str, int]]:
        """
        Extract unique subject lines with message counts.

        Args:
            limit: Maximum number of results to return (default: 100)

        Returns:
            List of (subject, count) tuples, sorted by count (descending)
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc="📝 Extracting subjects",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        counter = Counter()
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            # Decode MIME-encoded subjects
            subject = _decode_mime_header(header.get("subject", "")).strip()
            if subject:
                counter[subject] += 1

        return counter.most_common(limit)

    def extract_other_header(self, name: str, limit: int = 1000) -> List[Tuple[str, int]]:
        """
        Extract unique values for any header field with message counts.

        Args:
            name: Header field name (case-insensitive, e.g., "List-Id", "X-Mailer")
            limit: Maximum number of results to return (default: 1000)

        Returns:
            List of (value, count) tuples, sorted by count (descending)
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        header_name = name.lower()

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc=f"📋 Extracting {name} headers",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        counter = Counter()
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            value = header.get(header_name, "").strip()
            if value:
                counter[value] += 1

        return counter.most_common(limit)

    def extract_unique_keywords(self, limit: int = 500, min_count: int = 1) -> List[Tuple[str, int]]:
        """Extract unique keywords/flags from cached messages with counts.

        Args:
            limit: Maximum number of unique keywords to return (default: 500)
            min_count: Minimum message count to include keyword (default: 1)

        Returns:
            List of (keyword, count) tuples, sorted by count DESC, then by name

        Example:
            >>> engine.extract_unique_keywords(limit=50)
            [("\\Seen", 1234), ("Important", 456), ("Work", 123), ...]
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers WHERE data IS NOT NULL")

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc="🏷️  Extracting keywords",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        counter = Counter()
        for row in progress_bar:
            data = row[0] if row else ""
            try:
                header_data = json.loads(data) if isinstance(data, str) else data
                flags = header_data.get("flags", [])
                if isinstance(flags, list):
                    for flag in flags:
                        if flag:  # Skip empty strings
                            counter[flag] += 1
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        # Filter by min_count and sort by count DESC, then name ASC
        results = [
            (keyword, count)
            for keyword, count in counter.most_common()
            if count >= min_count
        ]

        return results[:limit]

    def count_from_contains(self, pattern: str) -> int:
        """
        Count messages where From address contains the given pattern (case-insensitive).

        Args:
            pattern: String pattern to search for in From addresses

        Returns:
            Number of messages with matching From addresses
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        pattern_lower = pattern.lower()

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc=f"🔍 Searching from: '{pattern[:30]}'",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        count = 0
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            from_addr = header.get("from", "").lower()
            if pattern_lower in from_addr:
                count += 1

        return count

    def count_subject_contains(self, pattern: str) -> int:
        """
        Count messages where Subject contains the given pattern (case-insensitive).

        Args:
            pattern: String pattern to search for in subjects

        Returns:
            Number of messages with matching subjects
        """
        cursor = self.conn.cursor()
        total_count = self._get_total_count()
        cursor.execute("SELECT data FROM headers")

        pattern_lower = pattern.lower()

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc=f"🔍 Searching subject: '{pattern[:30]}'",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        count = 0
        for row in progress_bar:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            subject = header.get("subject", "").lower()
            if pattern_lower in subject:
                count += 1

        return count

    def count_from_pattern(self, pattern: str) -> int:
        """
        Count messages where From address matches a wildcard pattern.

        Wildcard patterns use * for any characters (e.g., "xyz@amazon.*").
        This is converted to a "contains" match for simplicity.

        Args:
            pattern: Wildcard pattern (e.g., "xyz@amazon.*", "*@example.com")

        Returns:
            Number of messages with matching From addresses
        """
        # Convert wildcard pattern to contains pattern
        # For simplicity, treat wildcards as "contains" by removing the asterisk
        # e.g., "xyz@amazon.*" becomes "xyz@amazon."
        # e.g., "*@example.com" becomes "@example.com"
        search_pattern = pattern.replace("*", "")
        return self.count_from_contains(search_pattern)

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures connection is closed."""
        self.close()


class EmailPatternExtractor:
    """Extract and suggest email address patterns for rule creation.

    Given an email address, this class suggests progressively broader patterns
    (exact match, wildcard TLD, domain only, domain base) along with estimated
    match counts from the cache.

    The pattern suggestions help users create rules that match the right scope
    of messages - from very specific (one sender) to very broad (entire domain).
    """

    def suggest_patterns(
        self, email_addr: str, cache_engine: CacheQueryEngine, fast_mode: bool = False
    ) -> List[Tuple[str, str, int]]:
        """Suggest email patterns based on the given email address.

        Args:
            email_addr: Email address to extract patterns from (e.g., "noreply@amazon.com")
            cache_engine: Cache query engine for getting match counts
            fast_mode: If True, skip pattern effectiveness checking and return first pattern immediately

        Returns:
            List of tuples: (pattern, description, estimated_count)
            Only includes patterns that differ from the original email and provide
            broader matching capabilities.

        Example:
            >>> extractor = EmailPatternExtractor()
            >>> patterns = extractor.suggest_patterns("noreply@amazon.com", cache)
            >>> for pattern, desc, count in patterns:
            ...     print(f"{pattern:30} {desc:20} {count:5} messages")
            noreply@amazon.com             Exact match          45 messages
            noreply@amazon.*               All TLDs            127 messages
            @amazon.com                    All from domain     203 messages
            amazon                         All amazon domains  298 messages
        """
        if not email_addr:
            return []

        email_lower = email_addr.lower().strip()

        # In fast mode, skip effectiveness checking and return just the email as-is
        if fast_mode:
            return [(email_lower, "Address as-is", 0)]

        patterns: List[Tuple[str, str, int]] = []

        # Handle case where there's no @ sign
        if '@' not in email_lower:
            # If no @ sign, treat whole thing as domain pattern
            domain_base = email_lower.split('.')[0] if '.' in email_lower else email_lower
            if domain_base:
                count = cache_engine.count_from_pattern(f"*{domain_base}*")
                patterns.append((domain_base, f"All {domain_base} domains", count))
            return patterns

        # Extract local and domain parts
        local_part, domain_part = email_lower.rsplit('@', 1)

        # 1. Exact match
        exact_count = cache_engine.count_from_contains(email_lower)
        patterns.append((email_lower, "Exact match", exact_count))

        # 2. Wildcard TLD (if domain has a TLD)
        if '.' in domain_part:
            domain_without_tld = domain_part.rsplit('.', 1)[0]
            wildcard_tld = f"{local_part}@{domain_without_tld}.*"
            wildcard_count = cache_engine.count_from_pattern(wildcard_tld)
            # Only include if different from exact match
            if wildcard_count != exact_count or wildcard_count > exact_count:
                patterns.append((wildcard_tld, "All TLDs", wildcard_count))

        # 3. Domain only (any sender from this domain)
        domain_only = f"@{domain_part}"
        domain_count = cache_engine.count_from_contains(domain_only)
        # Only include if broader than previous patterns
        if domain_count > exact_count:
            patterns.append((domain_only, "All from domain", domain_count))

        # 4. Domain base (all related domains)
        domain_base = domain_part.split('.')[0]
        if domain_base and domain_base != domain_part:
            base_count = cache_engine.count_from_contains(domain_base)
            # Only include if broader than domain-only pattern
            if base_count > domain_count:
                patterns.append((domain_base, f"All {domain_base} domains", base_count))

        return patterns


class SubjectPatternExtractor:
    """Extract and suggest subject line patterns for rule creation.

    Given a subject line, this class suggests progressively broader patterns
    (exact match, without numbers, first N words, keywords) along with estimated
    match counts from the cache.

    The pattern suggestions help users create rules that match similar messages
    while filtering out variable content like order numbers or tracking IDs.
    """

    def suggest_patterns(
        self, subject: str, cache_engine: CacheQueryEngine, fast_mode: bool = False
    ) -> List[Tuple[str, str, int]]:
        """Suggest subject patterns based on the given subject line.

        Args:
            subject: Subject line to extract patterns from
            cache_engine: Cache query engine for getting match counts
            fast_mode: If True, skip pattern effectiveness checking and return first pattern immediately

        Returns:
            List of tuples: (pattern, description, estimated_count)
            Only includes patterns that differ meaningfully from the original
            and would match a broader set of messages.

        Example:
            >>> extractor = SubjectPatternExtractor()
            >>> patterns = extractor.suggest_patterns(
            ...     "Your Booking Confirmation For BRS-SRS-36558426",
            ...     cache
            ... )
            >>> for pattern, desc, count in patterns:
            ...     print(f"{pattern:50} {desc:25} {count:4} messages")
            Your Booking Confirmation For BRS-SRS-36558426     Exact match               1 messages
            Your Booking Confirmation For BRS-SRS-*            Without numbers          15 messages
            Your Booking Confirmation                          First 3 words            23 messages
            Booking                                            Keyword: Booking         45 messages
        """
        if not subject:
            return []

        import re

        subject_clean = subject.strip()

        if not subject_clean:
            return []

        # In fast mode, skip effectiveness checking and return just the subject as-is
        if fast_mode:
            return [(subject_clean, "Subject as-is", 0)]

        patterns: List[Tuple[str, str, int]] = []

        # 1. Exact match
        exact_count = cache_engine.count_subject_contains(subject_clean)
        patterns.append((subject_clean, "Exact match", exact_count))

        # 2. Without numbers (replace number sequences with wildcards)
        # Look for sequences of digits, possibly with separators
        without_numbers = re.sub(r'\b\d+\b', '*', subject_clean)
        # Handle codes like BRS-SRS-36558426 or ABC-123-DEF
        without_numbers = re.sub(r'[A-Z]+-[A-Z]+-\d+', '*', without_numbers)
        without_numbers = re.sub(r'[A-Z]+\d+', '*', without_numbers)  # Handle ABC123
        without_numbers = re.sub(r'\*+', '*', without_numbers)  # Collapse multiple wildcards
        without_numbers = without_numbers.strip()

        # Only include if different and not just a wildcard
        if without_numbers != subject_clean and without_numbers and without_numbers != '*':
            no_num_count = cache_engine.count_subject_contains(without_numbers.replace('*', ''))
            if no_num_count > exact_count:
                patterns.append((without_numbers, "Without numbers", no_num_count))

        # 3. First N words (try 3, then 2)
        words = subject_clean.split()

        if len(words) >= 3:
            # Try first 3 words
            first_3 = ' '.join(words[:3])
            if first_3 != subject_clean:
                first_3_count = cache_engine.count_subject_contains(first_3)
                if first_3_count > exact_count:
                    patterns.append((first_3, "First 3 words", first_3_count))

        if len(words) >= 2:
            # Try first 2 words (only if we haven't added first 3, or if meaningfully different)
            first_2 = ' '.join(words[:2])
            already_added = any(p[0] == first_2 for p in patterns)
            if not already_added and first_2 != subject_clean:
                first_2_count = cache_engine.count_subject_contains(first_2)
                # Only add if it provides more matches than exact
                if first_2_count > exact_count:
                    patterns.append((first_2, "First 2 words", first_2_count))

        # 4. Extract keywords (capitalized words > 3 chars, or longest word)
        keywords = [
            word for word in words
            if len(word) > 3 and (word[0].isupper() or word.isupper())
        ]

        # Filter out common words
        common_words = {
            'your', 'the', 'this', 'that', 'from', 'with', 'for', 'and',
            'are', 'was', 'were', 'been', 'have', 'has', 'had', 'will',
            'would', 'should', 'could', 'may', 'might', 'must', 'can',
            'when', 'where', 'what', 'which', 'who', 'whom', 'whose'
        }
        keywords = [kw for kw in keywords if kw.lower() not in common_words]

        # If no keywords found, try finding the longest meaningful word
        if not keywords and words:
            longest = max(words, key=len)
            if len(longest) > 3 and longest.lower() not in common_words:
                keywords = [longest]

        # Only suggest up to 2 most relevant keywords
        for keyword in keywords[:2]:
            # Skip if keyword is same as exact match
            if keyword == subject_clean:
                continue

            # Skip if we've already added this as a pattern
            already_added = any(p[0] == keyword for p in patterns)
            if already_added:
                continue

            kw_count = cache_engine.count_subject_contains(keyword)
            # Only add if it provides more matches than exact
            if kw_count > exact_count:
                patterns.append((keyword, f"Keyword: {keyword}", kw_count))

        return patterns


def extract_email_address(addr: str) -> str:
    """Extract the actual email address from various display name formats.

    Handles formats like:
    - email@domain.com → email@domain.com
    - <email@domain.com> → email@domain.com
    - Name <email@domain.com> → email@domain.com
    - "Name" <email@domain.com> → email@domain.com

    Args:
        addr: Email address string, possibly with display name

    Returns:
        The email address without display name or angle brackets
    """
    addr = addr.strip()
    if '<' in addr and '>' in addr:
        # Extract content between angle brackets
        start = addr.index('<')
        end = addr.index('>', start)
        return addr[start + 1:end].strip()
    return addr


def _extract_display_name(addr: str) -> str:
    """Extract the display name from an email address string.

    Handles various formats:
    - 'email@domain.com' → '' (no display name)
    - '<email@domain.com>' → '' (no display name)
    - 'Name <email@domain.com>' → 'Name'
    - '"Name" <email@domain.com>' → 'Name' (quotes removed)
    - 'First Last <email@domain.com>' → 'First Last'

    Args:
        addr: Email address string, possibly with display name

    Returns:
        The display name string, or empty string if no display name found
    """
    addr = addr.strip()
    if '<' not in addr or '>' not in addr:
        # No angle brackets means no display name
        return ''

    # Get the part before the angle brackets
    display_part = addr[:addr.index('<')].strip()

    # Remove surrounding quotes if present
    if display_part.startswith('"') and display_part.endswith('"'):
        display_part = display_part[1:-1].strip()

    return display_part


def create_expandable_email_items(
    email_groups: List[EmailGroup]
) -> List[ExpandableItem]:
    """Convert EmailGroup objects to ExpandableItem format for selector.

    Creates a hierarchical structure where:
    - Parent: The consolidated email address (optionally marked with variation count)
    - Children: Individual display name variations with their counts

    Args:
        email_groups: List of consolidated email groups with variations

    Returns:
        List of ExpandableItem objects ready for FilterableListSelector

    Example:
        Input: [EmailGroup("m@c.com", 150, [
                    DisplayNameVariation("Name1 <m@c.com>", "Name1", 100),
                    DisplayNameVariation("Name2 <m@c.com>", "Name2", 50)
                ])]
        Output: [ExpandableItem(
                    label="m@c.com [2 display names]",
                    count=150,
                    is_expandable=True,
                    children=[
                        ExpandableItem(label="Name1", count=100, indent_level=1),
                        ExpandableItem(label="Name2", count=50, indent_level=1)
                    ]
                )]
    """
    items = []

    for group in email_groups:
        # Create parent item
        if group.has_variations:
            label = f"{group.email} [{group.variation_count} display names]"
        else:
            label = group.email

        parent = ExpandableItem(
            label=label,
            count=group.total_count,
            is_expandable=group.has_variations,
            is_expanded=False,
            data=group,
            indent_level=0
        )

        # Create child items for variations (only if multiple)
        if group.has_variations:
            children = []
            for variation in group.variations:
                # Display format: display name if available, otherwise full address
                if variation.display_name:
                    child_label = variation.display_name
                else:
                    child_label = variation.full_address

                child = ExpandableItem(
                    label=child_label,
                    count=variation.count,
                    is_expandable=False,
                    is_expanded=False,
                    data=variation,
                    indent_level=1
                )
                children.append(child)

            parent.children = children

        items.append(parent)

    return items


def compute_domain_counts(addresses: List[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """Aggregate message counts grouped by domain.

    Args:
        addresses: List of (email_address, count) tuples from cache

    Returns:
        List of (domain, total_count) sorted by count descending

    Example:
        >>> addresses = [
        ...     ('noreply@amazon.com', 234),
        ...     ('orders@amazon.com', 45),
        ...     ('support@bank.com', 156),
        ... ]
        >>> compute_domain_counts(addresses)
        [('amazon.com', 279), ('bank.com', 156)]
    """
    from collections import defaultdict

    domain_counts = defaultdict(int)
    for addr, count in addresses:
        email = extract_email_address(addr)
        if '@' in email:
            domain = email.rsplit('@', 1)[1].strip().lower()
            domain_counts[domain] += count

    return sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)


def get_emails_for_domain(
    addresses: List[Tuple[str, int]], domain: str
) -> List[Tuple[str, int]]:
    """Get all email addresses from a specific domain.

    Args:
        addresses: List of (email_address, count) tuples
        domain: Domain to filter by (e.g., 'amazon.com')

    Returns:
        List of (email_address, count) for that domain, sorted by count descending

    Example:
        >>> addresses = [
        ...     ('noreply@amazon.com', 234),
        ...     ('support@bank.com', 156),
        ...     ('orders@amazon.com', 45),
        ... ]
        >>> get_emails_for_domain(addresses, 'amazon.com')
        [('noreply@amazon.com', 234), ('orders@amazon.com', 45)]
    """
    domain_lower = domain.lower().strip()
    filtered = [
        (addr, count)
        for addr, count in addresses
        if '@' in (email := extract_email_address(addr)) and email.rsplit('@', 1)[1].lower() == domain_lower
    ]
    # Sort by count descending
    return sorted(filtered, key=lambda x: x[1], reverse=True)


def consolidate_email_addresses(
    addresses: List[Tuple[str, int]], preserve_variations: bool = False
) -> List:
    """Consolidate email addresses with different display names.

    Groups addresses by their normalized email (ignoring display names),
    sums their message counts, and tracks the number of variations.

    Can return data in two formats for backward compatibility and feature expansion:
    - Default (preserve_variations=False): Returns old format with variation count
    - New (preserve_variations=True): Returns EmailGroup objects with full variation details

    Args:
        addresses: List of (email_address_with_display_name, count) tuples
        preserve_variations: If True, return detailed EmailGroup objects with variations.
                            If False (default), return legacy format for backward compatibility.

    Returns:
        If preserve_variations=False:
            List of (normalized_email, total_count, variation_count) tuples,
            sorted by total_count descending

        If preserve_variations=True:
            List of EmailGroup objects with full DisplayNameVariation details,
            sorted by total_count descending

    Example:
        >>> addresses = [
        ...     ('ClearScore <marketing@clearscore.com>', 582),
        ...     ('"ClearScore" <marketing@clearscore.com>', 404),
        ...     ('Clearscore <marketing@clearscore.com>', 17),
        ...     ('updates@clearscore.com', 1008),
        ... ]

        # Default backward-compatible format
        >>> consolidate_email_addresses(addresses)
        [
            ('marketing@clearscore.com', 1003, 3),
            ('updates@clearscore.com', 1008, 1)
        ]

        # New detailed format with variations
        >>> consolidate_email_addresses(addresses, preserve_variations=True)
        [
            EmailGroup(email='marketing@clearscore.com', total_count=1003, variations=[...]),
            EmailGroup(email='updates@clearscore.com', total_count=1008, variations=[...])
        ]
    """
    from collections import defaultdict

    # Group by normalized email
    email_data = defaultdict(lambda: {'count': 0, 'variations': {}})

    for addr, count in addresses:
        normalized = extract_email_address(addr)
        email_data[normalized]['count'] += count
        # Store with full address as key to preserve original variation
        if addr not in email_data[normalized]['variations']:
            email_data[normalized]['variations'][addr] = 0
        email_data[normalized]['variations'][addr] += count

    if preserve_variations:
        # Return new format with detailed variation information
        result = []
        for email, data in email_data.items():
            # Create DisplayNameVariation objects for each variation
            variations = [
                DisplayNameVariation(
                    full_address=full_addr,
                    display_name=_extract_display_name(full_addr),
                    count=count
                )
                for full_addr, count in data['variations'].items()
            ]
            # Sort variations by count descending
            variations.sort(key=lambda v: v.count, reverse=True)

            # Create EmailGroup with consolidated data
            email_group = EmailGroup(
                email=email,
                total_count=data['count'],
                variations=variations
            )
            result.append(email_group)

        # Sort by total_count descending
        return sorted(result, key=lambda x: x.total_count, reverse=True)
    else:
        # Return legacy format for backward compatibility
        result = [
            (email, data['count'], len(data['variations']))
            for email, data in email_data.items()
        ]

        # Sort by count descending
        return sorted(result, key=lambda x: x[1], reverse=True)


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


def _serialize_coverage_data(stats, uncovered_messages, domain_clusters):
    """Serialize coverage analysis results to JSON-compatible format.

    Converts CoverageStats, UncoveredMessage, and DomainCluster objects
    to dictionaries suitable for JSON storage.

    Args:
        stats: CoverageStats object
        uncovered_messages: List of UncoveredMessage objects
        domain_clusters: List of DomainCluster objects

    Returns:
        Dict with serialized coverage data
    """
    # Serialize uncovered messages
    serialized_uncovered = [
        {
            'uid': msg.uid,
            'folder': msg.folder,
            'from_address': msg.from_address,
            'subject': msg.subject,
            'domain': msg.domain,
        }
        for msg in uncovered_messages
    ]

    # Serialize domain clusters
    serialized_clusters = [
        {
            'domain': cluster.domain,
            'total_count': cluster.total_count,
            'senders': cluster.senders,
            'messages': [
                {
                    'uid': msg.uid,
                    'folder': msg.folder,
                    'from_address': msg.from_address,
                    'subject': msg.subject,
                    'domain': msg.domain,
                }
                for msg in cluster.messages
            ]
        }
        for cluster in domain_clusters
    ]

    return {
        'stats': {
            'total_messages': stats.total_messages,
            'covered_messages': stats.covered_messages,
            'uncovered_messages': stats.uncovered_messages,
            'coverage_by_rule': stats.coverage_by_rule,
        },
        'uncovered_messages': serialized_uncovered,
        'domain_clusters': serialized_clusters,
    }


def _deserialize_coverage_data(data):
    """Deserialize coverage data from JSON format back to objects.

    Converts stored dictionaries back to CoverageStats, UncoveredMessage,
    and DomainCluster objects.

    Args:
        data: Dict with serialized coverage data

    Returns:
        Tuple of (CoverageStats, List[UncoveredMessage], List[DomainCluster])
    """
    from core.tools.coverage_analyzer import CoverageStats, UncoveredMessage, DomainCluster

    # Deserialize stats
    stats_data = data['stats']
    stats = CoverageStats(
        total_messages=stats_data['total_messages'],
        covered_messages=stats_data['covered_messages'],
        uncovered_messages=stats_data['uncovered_messages'],
        coverage_by_rule=stats_data['coverage_by_rule'],
    )

    # Deserialize uncovered messages
    uncovered_messages = [
        UncoveredMessage(
            uid=msg['uid'],
            folder=msg['folder'],
            from_address=msg['from_address'],
            subject=msg['subject'],
            domain=msg['domain'],
        )
        for msg in data['uncovered_messages']
    ]

    # Deserialize domain clusters
    domain_clusters = [
        DomainCluster(
            domain=cluster['domain'],
            total_count=cluster['total_count'],
            senders=cluster['senders'],
            messages=[
                UncoveredMessage(
                    uid=msg['uid'],
                    folder=msg['folder'],
                    from_address=msg['from_address'],
                    subject=msg['subject'],
                    domain=msg['domain'],
                )
                for msg in cluster['messages']
            ]
        )
        for cluster in data['domain_clusters']
    ]

    return stats, uncovered_messages, domain_clusters


class RuleWizard:
    """Interactive wizard for creating IMAPFilter rules with guided workflow.

    This class orchestrates the entire rule creation process:
    1. Validates cache availability
    2. Guides user through adding conditions
    3. Shows filterable lists of header values
    4. Suggests patterns based on selections
    5. Configures logic and actions
    6. Previews and saves the rule

    The wizard uses a curses-based interface for filterable lists and
    simple console prompts for other inputs.

    Example:
        >>> from core.config import build_default_config
        >>> config = build_default_config()
        >>> wizard = RuleWizard(config)
        >>> exit_code = wizard.run()
        >>> if exit_code == 0:
        ...     print("Rule created successfully!")
    """

    def __init__(self, config, logger: Optional[JsonLogger] = None, show_progress: bool = True):
        """Initialize the rule wizard.

        Args:
            config: AppConfig object containing paths and settings
            logger: Optional JsonLogger for IMAP operations. If not provided, creates one.
            show_progress: Whether to display progress bars (default: True)

        Raises:
            ValueError: If cache database doesn't exist
        """
        from core.config import AppConfig

        if not isinstance(config, AppConfig):
            raise TypeError("config must be an AppConfig instance")

        self.config = config
        self.logger = logger or JsonLogger(config.paths.log_file)
        self.show_progress = show_progress
        self.cache_engine: Optional[CacheQueryEngine] = None
        self.coverage_analyzer: Optional[RuleCoverageAnalyzer] = None
        self.keyword_manager = KeywordManager(config.paths.data_dir)
        self.email_extractor = EmailPatternExtractor()
        self.subject_extractor = SubjectPatternExtractor()
        self.rule_builder = RuleBuilder()

        # NEW: Initialize wizard cache
        from core.wizard_cache import WizardCache
        # Derive wizard cache path from database location
        # If using custom cache, place wizard cache alongside it
        if self.config.paths.db_file == (self.config.paths.base_dir / DEFAULT_DATA_DIR / "cache.db"):
            # Using default cache - use standard wizard cache location
            cache_path = self.config.paths.data_dir / "wizard_cache.json"
        else:
            # Using custom cache - place wizard cache alongside it
            db_dir = self.config.paths.db_file.parent
            db_name = self.config.paths.db_file.stem
            cache_path = db_dir / f"wizard_cache_{db_name}.json"
        self.wizard_cache = WizardCache(cache_path)

        # Batch mode context for consistency across condition selection
        self.batch_mode_active = False
        self.batch_selected_cluster: Optional[DomainCluster] = None

        # Fast mode flag - set via CLI --fast-mode to skip pattern effectiveness checking
        self.fast_mode = False

        # Session-level JSON parsing cache for header data
        # Caches parsed headers to avoid repeated parsing of the same message data
        # Limited to 10,000 entries to avoid excessive memory usage
        self._json_parse_cache: Dict[Tuple[str, str], dict] = {}
        self._json_cache_max_size = 10000

        # Validate cache exists
        if not self.config.paths.db_file.exists():
            raise ValueError(
                f"Cache database not found at {self.config.paths.db_file}. "
                "Please run 'build-cache' first."
            )

    def _warm_cache(self):
        """
        Pre-fetch and cache frequently-used data on wizard startup.

        This prevents delays during rule creation by loading data in advance.
        Called from run() before showing welcome message.
        """
        print("\nPreparing wizard data...")

        # Check if keyword cache is valid
        cached_keywords = self.wizard_cache.get_keywords()
        if cached_keywords is None:
            # Cache miss - extract now
            try:
                print("Extracting keywords from cache (this may take a moment)...")
                keywords = self.cache_engine.extract_unique_keywords(limit=999999, min_count=1)
                self.wizard_cache.set_keywords(keywords)
                print(f"✓ Cached {len(keywords)} unique keywords")
            except Exception as e:
                print(f"⚠️  Could not cache keywords: {e}")
        else:
            print(f"✓ Using cached keywords ({len(cached_keywords)} available)")

        # Pre-fetch folders on startup (USER CONFIRMED)
        print("Fetching folder list from mail server...")
        cached_folders = self.wizard_cache.get_folders()
        if cached_folders is None:
            # Cache miss - fetch from IMAP server
            folders = self._get_imap_folders()
            if folders:
                print(f"✓ Cached {len(folders)} folders")
        else:
            print(f"✓ Using cached folders ({len(cached_folders)} available)")

    def invalidate_cache(self):
        """Force refresh of all cached data."""
        self.wizard_cache.clear()
        print("✓ Cache cleared - will refresh on next wizard run")

    def show_cache_status(self):
        """Display cache status and age."""
        import time

        cache = self.wizard_cache.load()

        print("\n" + "=" * 60)
        print("Wizard Cache Status")
        print("=" * 60)

        # Folders
        folders_cache = cache.get('folders', {})
        folders_timestamp = folders_cache.get('timestamp', 0)
        folders_data = folders_cache.get('data')

        if folders_data:
            age_hours = (time.time() - folders_timestamp) / 3600
            status = "✓ Valid" if age_hours < 6 else "⚠️ Stale"
            print(f"Folders: {status}")
            print(f"  Count: {len(folders_data)}")
            print(f"  Age: {age_hours:.1f} hours")
        else:
            print("Folders: ❌ Not cached")

        # Keywords
        keywords_cache = cache.get('keywords', {})
        keywords_timestamp = keywords_cache.get('timestamp', 0)
        keywords_data = keywords_cache.get('data')

        if keywords_data:
            age_hours = (time.time() - keywords_timestamp) / 3600
            status = "✓ Valid" if age_hours < 6 else "⚠️ Stale"
            print(f"\nKeywords: {status}")
            print(f"  Count: {len(keywords_data)}")
            print(f"  Age: {age_hours:.1f} hours")
        else:
            print("\nKeywords: ❌ Not cached")

        print("=" * 60)

    def _load_or_analyze_coverage(self):
        """Load cached coverage analysis or perform fresh analysis.

        Checks if coverage cache is valid (rules and cache.db unchanged).
        If valid, returns cached results. Otherwise, performs analysis
        and caches the results.

        Returns:
            CoverageStats object with coverage information
        """
        # Try to load from cache
        cached_data = self.wizard_cache.get_coverage(
            self.config.paths.rules_dir,
            self.config.paths.db_file
        )

        if cached_data:
            # Cache hit - restore objects from cached data
            print("  (using cached analysis)")
            stats, uncovered_messages, domain_clusters = _deserialize_coverage_data(cached_data)
            self.coverage_analyzer._coverage_stats = stats
            self.coverage_analyzer._uncovered_messages = uncovered_messages
            self.coverage_analyzer._domain_clusters = domain_clusters
            return stats

        # Cache miss - perform analysis
        stats = self.coverage_analyzer.analyze_coverage()

        # Save results to cache for next run
        try:
            uncovered_messages = self.coverage_analyzer.get_uncovered_messages()
            domain_clusters = self.coverage_analyzer.get_domain_clusters()
            coverage_data = _serialize_coverage_data(stats, uncovered_messages, domain_clusters)
            self.wizard_cache.set_coverage(
                coverage_data,
                self.config.paths.rules_dir,
                self.config.paths.db_file
            )
        except Exception as e:
            # Don't fail if caching doesn't work
            if self.logger:
                self.logger.log(f"Warning: Could not cache coverage data: {e}")

        return stats

    def _get_parsed_header(self, folder: str, uid: str, data: str) -> dict:
        """Get parsed header, using session-level cache to avoid re-parsing.

        This method caches parsed headers to improve performance when the
        same message data is accessed multiple times during the wizard session.

        Args:
            folder: Folder name
            uid: Message UID
            data: Raw message data (JSON string)

        Returns:
            Parsed header dictionary
        """
        from core.rule_engine import _extract_raw_header, _parse_header_map

        cache_key = (folder, uid)

        # Check cache first
        if cache_key in self._json_parse_cache:
            return self._json_parse_cache[cache_key]

        # Parse and cache
        raw_header = _extract_raw_header(data)
        header = _parse_header_map(raw_header)

        # Only cache if we're not exceeding memory limits
        if len(self._json_parse_cache) < self._json_cache_max_size:
            self._json_parse_cache[cache_key] = header

        return header

    def _input_with_prefill(self, prompt: str, prefill: str = "") -> str:
        """
        Show input prompt with pre-filled text for editing.

        Args:
            prompt: Prompt text to display
            prefill: Default text to show in input field

        Returns:
            User's input (or prefill if unchanged)
        """
        try:
            import readline
            def hook():
                readline.insert_text(prefill)
                readline.redisplay()
            readline.set_pre_input_hook(hook)
            try:
                return input(prompt).strip()
            finally:
                readline.set_pre_input_hook()
        except (ImportError, AttributeError):
            # Fallback for non-readline environments
            print(prompt)
            if prefill:
                print(f"  Current: {prefill}")
            result = input("  New (or press Enter to keep): ").strip()
            return result if result else prefill

    # ====== JSON Import Methods ======

    def _run_json_import_mode(self) -> int:
        """Run JSON import mode for importing rule(s) from JSON.

        Returns:
            Exit code: 0 on success, 1 on error, 130 on cancelled
        """
        while True:
            # Collect multi-line JSON input
            json_str = self._collect_multiline_json_input()
            if json_str is None:
                print("\nJSON import cancelled.")
                return 130

            # Parse and normalize JSON
            rules, error_msg = self._parse_and_normalize_json(json_str)
            if rules is None:
                print(f"\n❌ {error_msg}\n")
                retry = prompt_yes_no("Try again?", default=True)
                if not retry:
                    return 130
                continue

            print(f"\n✓ JSON parsed successfully ({len(rules)} rule(s))")

            # Validate and fix errors interactively
            fixed_rules = []
            for i, rule in enumerate(rules):
                is_valid, errors, warnings = self._validate_imported_rule(rule, i)

                if errors:
                    print(f"\n❌ Validation failed for rule {i + 1}: \"{rule.get('name', 'Unknown')}\"")
                    print("\nErrors:")
                    for j, error in enumerate(errors, 1):
                        print(f"  {j}. {error}")

                    fixed_rule = self._interactive_fix_errors(rule, errors, i)
                    if fixed_rule is None:
                        print("\nCannot proceed without fixing errors.")
                        retry = prompt_yes_no("Try again?", default=True)
                        if not retry:
                            return 130
                        break
                    fixed_rules.append(fixed_rule)
                elif warnings:
                    print(f"\n⚠️  Warnings for rule {i + 1}: \"{rule.get('name', 'Unknown')}\"")
                    for warning in warnings:
                        print(f"  - {warning}")
                    proceed = prompt_yes_no("Proceed anyway?", default=True)
                    if proceed:
                        fixed_rules.append(rule)
                    else:
                        retry = prompt_yes_no("Try again?", default=True)
                        if not retry:
                            return 130
                        break
                else:
                    print(f"✓ Rule {i + 1}: \"{rule.get('name', 'Unknown')}\" - validation passed")
                    fixed_rules.append(rule)

            # If any rule failed validation and user didn't fix it, retry
            if len(fixed_rules) != len(rules):
                continue

            # Preview and confirm save
            if not self._preview_imported_rules(fixed_rules):
                retry = prompt_yes_no("Try again?", default=False)
                if not retry:
                    return 130
                continue

            # Save rules
            success_count, saved_files = self._save_imported_rules(fixed_rules)

            # Summary
            print("\n" + "=" * 60)
            print("Summary")
            print("=" * 60)
            print(f"✓ Saved {success_count}/{len(fixed_rules)} rule(s)")
            for filename in saved_files:
                print(f"  • {filename}")

            # Ask what to do next
            print("\n" + "=" * 60)
            print("What next?")
            print("=" * 60)
            print("  1. Import another rule")
            print("  2. Create rule with wizard")
            print("  3. Exit")
            print("=" * 60)

            next_choice = input("\nChoice (1-3) [default: 3]: ").strip()
            if next_choice == "1":
                continue
            elif next_choice == "2":
                return self._run_normal_wizard()
            else:
                return 0

    def _collect_multiline_json_input(self) -> Optional[str]:
        """Collect multi-line JSON input from user.

        Returns:
            JSON string if collected, None if cancelled
        """
        print("\n" + "=" * 60)
        print("JSON Import Mode")
        print("=" * 60)
        print("\nPaste your rule JSON below:")
        print("- Single rule: Paste the JSON object")
        print("- Multiple rules: Paste an array of rule objects")
        print("- End input: Press Enter twice or Ctrl+D")
        print("- Cancel: Press Ctrl+C\n")

        lines = []
        empty_count = 0
        print("Paste JSON now:")

        try:
            while True:
                line = input()
                if not line.strip():
                    empty_count += 1
                    if empty_count >= 2:  # Two empty lines = done
                        break
                else:
                    empty_count = 0
                    lines.append(line)
        except EOFError:  # Ctrl+D
            pass
        except KeyboardInterrupt:  # Ctrl+C
            return None

        json_str = "\n".join(lines)
        if json_str.strip():
            print(f"\n✓ Received {len(json_str)} characters")
            return json_str
        return None

    def _parse_and_normalize_json(self, json_str: str) -> Tuple[Optional[List[dict]], str]:
        """Parse JSON string and normalize to list of rules.

        Returns:
            Tuple of (rules_list, error_message)
        """
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON syntax at line {e.lineno}, column {e.colno}: {e.msg}"

        # Normalize to list
        if isinstance(parsed, dict):
            return [parsed], ""
        elif isinstance(parsed, list):
            # Validate each element is a dict
            for i, item in enumerate(parsed):
                if not isinstance(item, dict):
                    return None, f"Item {i + 1} in array is not a rule object (got {type(item).__name__})"
            return parsed, ""
        else:
            return None, f"Expected rule object or array, got {type(parsed).__name__}"

    def _validate_imported_rule(self, rule: dict, index: int = 0) -> Tuple[bool, List[str], List[str]]:
        """Validate imported rule structure.

        Returns:
            Tuple of (is_valid, errors, warnings)
        """
        errors = []
        warnings = []

        # Check required fields
        for field in ["name", "priority", "conditions"]:
            if field not in rule:
                errors.append(f"Missing required field: {field}")

        # Validate conditions structure
        conditions = rule.get("conditions", {})
        if conditions:
            if not isinstance(conditions, dict):
                errors.append("Conditions must be a dictionary")
            elif "all" not in conditions and "any" not in conditions:
                errors.append("Conditions must have 'all' or 'any' at root level")

        # Validate actions
        if "action" in rule and "actions" in rule:
            errors.append("Cannot have both 'action' and 'actions' fields")
        elif "action" not in rule and "actions" not in rule:
            errors.append("Must have either 'action' or 'actions' field")
        else:
            # Validate action-specific requirements
            actions = []
            if "action" in rule:
                actions = [rule.get("action")]
            elif "actions" in rule:
                actions = rule.get("actions", [])

            for i, action in enumerate(actions):
                if not isinstance(action, dict):
                    errors.append(f"Action {i + 1}: Must be a dictionary")
                    continue

                action_type = action.get("type", "")
                if not action_type:
                    errors.append(f"Action {i + 1}: Missing 'type' field")
                elif action_type == "move":
                    if not action.get("target"):
                        errors.append(f"Action {i + 1}: Move action missing target folder")
                elif action_type in ("set_keywords", "remove_keywords"):
                    if not action.get("keywords"):
                        errors.append(f"Action {i + 1}: {action_type} action missing keywords array")

        # Use RuleValidator for soft warnings (only if no hard errors)
        if not errors:
            try:
                from core.rule_validator import RuleValidator
                validator = RuleValidator()
                is_valid, validation_warnings = validator.validate_rule(rule)
                warnings.extend(validation_warnings)
            except Exception:
                # If validator fails, continue anyway
                pass

        return (len(errors) == 0, errors, warnings)

    def _interactive_fix_errors(self, rule: dict, errors: List[str], index: int = 0) -> Optional[dict]:
        """Guide user through fixing validation errors.

        Returns:
            Fixed rule dict or None if cancelled
        """
        print(f"\nThis rule cannot be saved without fixes.")
        fix_response = prompt_yes_no("Fix interactively?", default=True)

        if not fix_response:
            return None

        # Fix errors one by one
        for error in errors:
            if "Missing required field: name" in error:
                name = input("Enter rule name: ").strip()
                if not name:
                    print("❌ Name cannot be empty")
                    return None
                rule["name"] = name
                print(f"✓ Name set to \"{name}\"")

            elif "Missing required field: priority" in error:
                priority_str = input("Enter priority [default: 100]: ").strip()
                try:
                    priority = int(priority_str) if priority_str else 100
                    rule["priority"] = priority
                    print(f"✓ Priority set to {priority}")
                except ValueError:
                    print("❌ Invalid priority, must be an integer")
                    return None

            elif "Missing required field: conditions" in error:
                print("❌ Cannot automatically fix missing conditions.")
                print("    Conditions define when the rule matches.")
                return None

            elif "Conditions must have 'all' or 'any'" in error:
                print("\n⚠️  Conditions structure is invalid.")
                print("    Current structure: " + json.dumps(rule.get("conditions", {})))
                print("    Must have 'all' or 'any' at root level")
                print("\n    Example correct structure:")
                print('    {"conditions": {"any": [...]}}')
                return None

            elif "Move action missing target" in error:
                target = input("Enter target folder (e.g., INBOX/Archive): ").strip()
                if not target:
                    print("❌ Target folder cannot be empty")
                    return None
                # Find and fix the move action
                if "action" in rule and rule["action"].get("type") == "move":
                    rule["action"]["target"] = target
                elif "actions" in rule:
                    for action in rule["actions"]:
                        if action.get("type") == "move" and not action.get("target"):
                            action["target"] = target
                            break
                print(f"✓ Target set to \"{target}\"")

            elif "keywords action missing keywords" in error or "action missing keywords array" in error:
                keywords_str = input("Enter keywords (comma-separated): ").strip()
                if not keywords_str:
                    print("❌ Keywords cannot be empty")
                    return None
                keywords = [k.strip() for k in keywords_str.split(",")]
                # Find and fix the keywords action
                if "action" in rule and rule["action"].get("type") in ("set_keywords", "remove_keywords"):
                    rule["action"]["keywords"] = keywords
                elif "actions" in rule:
                    for action in rule["actions"]:
                        if action.get("type") in ("set_keywords", "remove_keywords") and not action.get("keywords"):
                            action["keywords"] = keywords
                            break
                print(f"✓ Keywords set to {keywords}")

        # Re-validate
        print("\nRe-validating...")
        is_valid, new_errors, new_warnings = self._validate_imported_rule(rule, index)

        if new_errors:
            print("❌ Still has errors:")
            for error in new_errors:
                print(f"  - {error}")
            return self._interactive_fix_errors(rule, new_errors, index)  # Recurse

        print("✓ Validation passed!")
        return rule

    def _preview_imported_rules(self, rules: List[dict]) -> bool:
        """Show preview of imported rules with dry-run match counts.

        Returns:
            True if user confirms save, False to cancel
        """
        print("\n" + "=" * 60)
        print(f"Preview: {len(rules)} Rule(s)")
        print("=" * 60)

        total_matches = 0
        for i, rule in enumerate(rules, 1):
            print(f"\n--- Rule {i}/{len(rules)} ---")
            print(json.dumps(rule, indent=2))

            # Run dry-run preview using existing method
            matches, folder_matches, stats = self._preview_rule(rule)
            total_matches += matches

            print(f"\n✓ Preview complete for \"{rule.get('name', 'Unknown')}\"")
            if folder_matches:
                print(f"\nMatches by folder:")
                for folder, count in sorted(folder_matches.items()):
                    print(f"  {folder}: {format_count(count)} messages")
            print(f"Total: {format_count(matches)} messages would be affected")

        # Summary
        print("\n" + "=" * 60)
        print("Summary")
        print("=" * 60)
        print(f"  • {len(rules)} rule(s) ready to save")
        print(f"  • {format_count(total_matches)} total messages will be affected")

        return prompt_yes_no("\nSave these rule(s)?", default=True)

    def _save_imported_rules(self, rules: List[dict]) -> Tuple[int, List[str]]:
        """Save imported rules to rules directory.

        Returns:
            Tuple of (success_count, saved_files)
        """
        success_count = 0
        saved_files = []

        for i, rule in enumerate(rules, 1):
            success, message = save_rule(rule, self.config.paths.rules_dir)

            if success:
                # Extract filename from message
                print(f"✓ Rule {i}: {message}")
                success_count += 1
                saved_files.append(message)
            else:
                print(f"❌ Rule {i} failed: {message}")

        return success_count, saved_files

    def run(self) -> int:
        """Run the complete rule wizard workflow.

        Returns:
            Exit code: 0 on success, 1 on error, 130 on user cancellation
        """
        try:
            # Initialize cache engine
            self.cache_engine = CacheQueryEngine(self.config.paths.db_file, show_progress=self.show_progress)

            # Display welcome and validate cache
            if not self._validate_cache():
                return 1

            # Handle cache TTL override from CLI
            if hasattr(self, 'cache_ttl_override') and self.cache_ttl_override:
                self.wizard_cache.CACHE_TTL_SECONDS = self.cache_ttl_override * 3600

            # NEW: Warm cache before starting wizard
            self._warm_cache()

            # Analyze coverage and offer batch mode
            print("\n🔍 Analyzing rule coverage...")
            self.coverage_analyzer = RuleCoverageAnalyzer(
                db_path=self.config.paths.db_file,
                rules_dir=self.config.paths.rules_dir,
                logger=self.logger,
            )
            stats = self._load_or_analyze_coverage()

            # Display coverage statistics
            self._display_coverage_stats(stats)

            # Decide on mode based on coverage
            if stats.uncovered_messages == 0:
                print("\n✅ All cached emails have rules!")
                choice = input("\nCreate a new rule anyway? (y/n): ").strip().lower()
                if choice != "y":
                    return 0
                # Fall through to normal wizard (will display welcome there)
            elif stats.uncovered_messages > 0:
                print(f"\n📋 Found {format_count(stats.uncovered_messages)} emails without rules")
                choice = input("\nEnter batch mode to create rules? (Y/n): ").strip().lower()
                if choice != "n":
                    return self.run_batch_mode()
                # Fall through to normal wizard (will display welcome there)

            # Normal wizard flow
            return self._run_normal_wizard()

        except KeyboardInterrupt:
            print("\n\nWizard interrupted.")
            return 130
        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()
            return 1
        finally:
            if self.cache_engine:
                self.cache_engine.close()
            if self.coverage_analyzer:
                self.coverage_analyzer.close()

    def _run_normal_wizard(self) -> int:
        """Run the normal (non-batch) wizard workflow.

        Returns:
            Exit code: 0 on success, 1 on error, 130 on user cancellation
        """
        self._display_welcome()

        # Mode selection
        print("\n" + "=" * 60)
        print("Choose Mode:")
        print("=" * 60)
        print("  1. Create rule with guided wizard (interactive)")
        print("  2. Import rule from JSON (paste)")
        print("=" * 60)

        choice = input("\nChoice (1/2) [default: 1]: ").strip()

        if choice == "2":
            return self._run_json_import_mode()

        # Main workflow loop
        while True:
            # Step 1: Add conditions
            conditions_added = self._add_conditions_loop()
            if conditions_added is None:  # User cancelled
                print("\nWizard cancelled.")
                return 130

            if not conditions_added:
                print("\nAt least one condition is required. Please try again.\n")
                continue

            # Step 2: Configure logic/grouping (if multiple conditions)
            if len(self.rule_builder._flat_conditions) > 1:
                # New: Use grouping workflow which allows mixing AND/OR logic
                grouping = self._configure_grouping()
            else:
                grouping = None

            # Finalize conditions (convert flat to tree structure)
            self.rule_builder.finalize_conditions(grouping)

            # Step 3: Configure action
            if not self._configure_action():
                continue  # Restart if cancelled

            # Step 4: Set metadata
            if not self._configure_metadata():
                continue  # Restart if cancelled

            # Step 5: Preview and save
            result = self._preview_and_save()
            if result == 0:
                return 0  # Success
            elif result == 130:
                return 130  # Cancelled
            # Otherwise loop back to edit

    def run_batch_mode(self) -> int:
        """Run wizard in batch mode for creating rules for uncovered emails.

        Returns:
            Exit code: 0 on success, 1 on error, 130 on user cancellation
        """
        if not self.coverage_analyzer:
            print("❌ Coverage analyzer not initialized")
            return 1

        iteration = 1
        while True:
            # Get current uncovered emails
            clusters = self.coverage_analyzer.get_domain_clusters()
            if not clusters:
                print("\n✅ All emails now have rules!")
                return 0

            print(f"\n{'='*60}")
            print(f"BATCH MODE - Iteration {iteration}")
            print(f"{'='*60}")

            # Two-step selection (domain → sender)
            batch_target = self._select_batch_target(clusters)
            if batch_target is None:
                # User cancelled - offer to continue or exit
                choice = input("\nContinue with normal wizard? (y/n): ").strip().lower()
                if choice == "y":
                    return self._run_normal_wizard()
                return 0

            # Set batch context for consistent address selection across conditions
            self.batch_mode_active = True
            if batch_target.target_type == "domain":
                # Domain-wide selection: use the domain directly
                domain = batch_target.value
            else:
                # Email selection: extract domain from email address
                domain = batch_target.value.split("@")[-1] if "@" in batch_target.value else batch_target.value

            self.batch_selected_cluster = self.coverage_analyzer.find_cluster(domain)

            # Pre-populate first condition based on selection
            self._prepopulate_condition(batch_target)
            self._display_batch_context(batch_target)

            # Reset rule builder for new rule
            self.rule_builder = RuleBuilder()
            self._prepopulate_condition(batch_target)

            # Offer option to edit the pre-populated condition
            edit_choice = prompt_yes_no(
                "Do you want to edit this pre-populated condition? "
                "(You can change the field or match type)",
                default=False
            )

            if edit_choice is True:
                # User wants to edit - remove pre-populated condition
                # Save the original condition details for potential restoration
                saved_conditions = self.rule_builder.conditions.copy()
                self.rule_builder.conditions.clear()

                # Let user build condition with full options
                print("\n" + "=" * 60)
                print("EDIT FIRST CONDITION")
                print("=" * 60)
                print("Build your condition with full field and match type options...")

                success = self._add_single_condition()

                if not success:
                    # User cancelled editing - restore pre-populated condition
                    print("\n⚠️  Edit cancelled - keeping original pre-populated condition")
                    self.rule_builder.conditions = saved_conditions
            elif edit_choice is None:
                # User cancelled the prompt - continue with batch mode
                print("\nContinuing with pre-populated condition...")
            # If edit_choice is False, continue with pre-populated condition as-is

            # Run normal wizard flow for remaining steps
            exit_code = self._add_conditions_loop()
            if exit_code is None:  # Cancelled
                print("\nContinuing batch mode...")
                iteration += 1
                continue

            # Step 2: Configure logic/grouping (if multiple conditions)
            # Note: Batch mode uses simple logic (no grouping) for now
            if len(self.rule_builder._flat_conditions) > 1:
                if not self._configure_logic():
                    iteration += 1
                    continue  # Restart if cancelled
                grouping = None
            else:
                grouping = None

            # Finalize conditions (convert flat to tree structure)
            self.rule_builder.finalize_conditions(grouping)

            # Step 3: Configure action
            if not self._configure_action():
                iteration += 1
                continue  # Restart if cancelled

            # Step 4: Set metadata
            if not self._configure_metadata():
                iteration += 1
                continue  # Restart if cancelled

            # Save rule (simplified - no preview)
            exit_code = self._save_batch_rule()
            if exit_code == 0:
                print("✅ Rule saved!")
                # Invalidate coverage cache and refresh analysis
                print("\n🔄 Refreshing coverage...")
                self.wizard_cache.invalidate_coverage()
                stats = self._load_or_analyze_coverage()
                self._display_coverage_stats(stats)

                if stats.uncovered_messages == 0:
                    print("\n✅ All emails now have rules!")
                    return 0

                # Clear batch context for next iteration
                self.batch_mode_active = False
                self.batch_selected_cluster = None

                # Continue loop
                iteration += 1
                continue
            elif exit_code == 130:
                # User wants to exit batch mode
                self.batch_mode_active = False
                self.batch_selected_cluster = None
                return 0
            else:
                # Error occurred
                print("⚠️  Error saving rule, continuing...")
                # Clear batch context for next iteration
                self.batch_mode_active = False
                self.batch_selected_cluster = None
                iteration += 1
                continue

    def _select_batch_target(self, clusters: List[DomainCluster]) -> Optional[BatchTarget]:
        """Two-step selection: domain → sender.

        Args:
            clusters: List of DomainCluster objects

        Returns:
            BatchTarget if selected, None if cancelled
        """
        # Step 1: Select domain cluster
        domain_items = [
            (cluster.domain, cluster.total_count)
            for cluster in sorted(clusters, key=lambda c: c.total_count, reverse=True)
        ]

        print(f"\nSelect Domain ({len(clusters)} domains with uncovered emails)")
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
        input("Press Enter to open selector...")

        selector = FilterableListSelector(
            domain_items, f"Select Domain ({len(clusters)} with uncovered emails)"
        )
        selection_result = curses.wrapper(selector.run)
        if not selection_result:
            # ISSUE #6 FIX: Enhanced domain cancellation with menu options
            print("\nDomain selection cancelled.")
            print("\nWhat would you like to do?")
            print("  1. Retry domain selection")
            print("  2. Exit batch mode")
            print("  3. Cancel")

            choice = input("\nChoice (1-3): ").strip()
            if choice == "1":
                return self._select_batch_target(clusters)
            elif choice == "2":
                print("\nExiting batch mode. You can continue with the normal wizard.")
                return None
            else:
                return None

        # Extract label from (label, data) tuple returned by selector
        if isinstance(selection_result, tuple):
            selected_domain = selection_result[0]
        else:
            selected_domain = selection_result

        # Find the cluster
        cluster = self.coverage_analyzer.find_cluster(selected_domain)
        if not cluster:
            print(f"❌ Cluster for {selected_domain} not found")
            return None

        # Step 2: Select specific sender or all from domain
        # Convert cluster.senders dict to list of tuples for consolidation
        senders_list = [(email, count) for email, count in cluster.senders.items()]

        # Consolidate email addresses, preserving variations for expand/collapse
        email_groups = consolidate_email_addresses(senders_list, preserve_variations=True)

        # Convert to expandable item format
        expandable_items = create_expandable_email_items(email_groups)

        # Prepend domain-wide option as ExpandableItem
        domain_option = ExpandableItem(
            label=f"[All from {cluster.domain}]",
            count=cluster.total_count,
            is_expandable=False,
            data=None,
            indent_level=0
        )
        sender_items = [domain_option] + expandable_items

        print(f"\nSelect Sender from {cluster.domain}")
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
        input("Press Enter to open selector...")

        selector = FilterableListSelector(sender_items, f"Select Sender from {cluster.domain}")
        result = curses.wrapper(selector.run)
        if not result:
            print("\nGoing back to domain selection...")
            return self._select_batch_target(clusters)

        # Handle both tuple (label, data) from new format and string from legacy
        if isinstance(result, tuple):
            selected_label, selected_data = result
        else:
            # Backward compatibility for legacy string returns
            selected_label = result
            selected_data = None

        # Determine if domain-wide or specific email
        if selected_label.startswith("[All from "):
            return BatchTarget(
                target_type="domain",
                value=cluster.domain,
                estimated_count=cluster.total_count,
                sample_messages=cluster.messages[:5],
            )
        else:
            # Extract email from consolidated label (remove "[N display names]" suffix if present)
            email_address = self._extract_email_from_consolidated_label(selected_label, selected_data)

            # Sum counts for all variations of this email address
            estimated_count = sum(
                count for addr, count in cluster.senders.items()
                if extract_email_address(addr) == email_address
            )

            return BatchTarget(
                target_type="email",
                value=email_address,
                estimated_count=estimated_count,
                sample_messages=[
                    m for m in cluster.messages
                    if extract_email_address(m.from_address) == email_address
                ][:5],
            )

    def _display_coverage_stats(self, stats) -> None:
        """Display coverage statistics.

        Args:
            stats: CoverageStats object
        """
        print("\n" + "=" * 60)
        print("📊 COVERAGE ANALYSIS")
        print("=" * 60)
        print(f"Total messages:    {format_count(stats.total_messages):>10}")
        print(f"Covered:           {format_count(stats.covered_messages):>10} ({stats.coverage_percentage:.1f}%)")
        print(f"Uncovered:         {format_count(stats.uncovered_messages):>10} ({100 - stats.coverage_percentage:.1f}%)")
        if stats.coverage_by_rule:
            print("\nCoverage by rule (top 5):")
            for rule_name, count in sorted(
                stats.coverage_by_rule.items(), key=lambda x: x[1], reverse=True
            )[:5]:
                print(f"  • {rule_name}: {format_count(count)} messages")
        print("=" * 60)

    def _format_domain_selection(self, domain: str, field: str = "from") -> str:
        """ISSUE #5 FIX: Create standardized '[All {field} domain]' format.

        This provides a single point to generate the domain-wide selection string,
        ensuring consistent formatting across batch mode and additional conditions.

        Args:
            domain: The domain name (e.g., 'example.com')
            field: Email field name ('from', 'to', 'cc', 'bcc', 'reply-to')

        Returns:
            Formatted string: '[All from example.com]', '[All to example.com]', etc.
        """
        return f"[All {field} {domain}]"

    def _is_domain_selection(self, selection: str) -> bool:
        """ISSUE #5 FIX: Detect if a selection is a domain-wide selection.

        Checks if a string is in the '[All {field} domain]' format.

        Args:
            selection: The selection string to check

        Returns:
            True if selection is domain-wide format, False otherwise

        Examples:
            '[All from example.com]' -> True
            '[All to example.com]' -> True
            '[All cc example.com]' -> True
            'user@example.com' -> False
        """
        return selection.startswith("[All ") and selection.endswith("]") and " " in selection[5:-1]

    def _extract_domain_from_selection(self, selection: str) -> Optional[str]:
        """ISSUE #5 FIX: Extract domain from '[All {field} domain]' selection.

        Safely extracts domain name from the domain-wide selection format.
        Supports all field types (from, to, cc, bcc, reply-to).

        Args:
            selection: A selection string, potentially in '[All {field} domain]' format

        Returns:
            Domain name if selection is domain format, None otherwise

        Examples:
            '[All from example.com]' -> 'example.com'
            '[All to example.com]' -> 'example.com'
            '[All cc example.com]' -> 'example.com'
            'user@example.com' -> None
        """
        if self._is_domain_selection(selection):
            # Extract from '[All {field} {domain}]' format
            # Remove '[All ' prefix (5 chars) and ']' suffix (1 char)
            content = selection[5:-1]
            # Split on last space to separate field from domain
            parts = content.rsplit(' ', 1)
            if len(parts) == 2:
                return parts[1]  # Return domain part
        return None

    def _convert_domain_selection_to_pattern(self, selection: str) -> str:
        """Convert '[All from domain]' selection to '@domain' pattern.

        This helper method provides consistent conversion of the domain-wide selection
        format used in both batch mode and additional conditions workflows.

        Args:
            selection: Either '[All from domain.com]' format or a regular email/pattern

        Returns:
            '@domain' pattern if input is '[All from ...]', otherwise returns input unchanged

        Examples:
            '[All from example.com]' -> '@example.com'
            'user@example.com' -> 'user@example.com'
        """
        domain = self._extract_domain_from_selection(selection)
        if domain:
            return f"@{domain}"
        return selection

    def _extract_email_from_consolidated_label(
        self,
        label: str,
        data: Optional[Any] = None
    ) -> str:
        """Extract email address from consolidated label or data.

        Can extract from:
        - Label only (legacy format): "email@domain.com [N display names]"
        - Data object (new format): EmailGroup or DisplayNameVariation
        - Or combination of both

        Args:
            label: Email label, potentially in format "email@domain.com [N display names]"
            data: Optional data object (EmailGroup or DisplayNameVariation)

        Returns:
            The email address

        Examples:
            'marketing@clearscore.com [4 display names]' -> 'marketing@clearscore.com'
            'marketing@clearscore.com' -> 'marketing@clearscore.com'
        """
        # If we have EmailGroup or DisplayNameVariation data, use it directly
        if data is not None:
            if isinstance(data, EmailGroup):
                return data.email
            elif isinstance(data, DisplayNameVariation):
                # Extract email from the full address
                return extract_email_address(data.full_address)

        # Fallback to label parsing
        if ' [' in label and label.endswith(' display names]'):
            return label.split(' [')[0]

        # Handle bare email
        if '@' in label:
            return extract_email_address(label)

        # Last resort
        return label

    def _prepopulate_condition(self, batch_target: BatchTarget) -> None:
        """Pre-populate first condition based on batch selection.

        Args:
            batch_target: BatchTarget with selection info
        """
        if batch_target.target_type == "domain":
            # Domain-wide: use @domain.com pattern (via helper for consistency)
            pattern = self._convert_domain_selection_to_pattern(f"[All from {batch_target.value}]")
            self.rule_builder.add_condition(header="from", match_type="contains", value=pattern)
        else:
            # Specific email: use exact address
            pattern = batch_target.value
            self.rule_builder.add_condition(header="from", match_type="contains", value=pattern)

        # ISSUE #2 FIX: Standardized success message format
        print(f"\n✓ Condition: sender (from) contains '{pattern}'")
        print(f"  (Estimated to match {format_count(batch_target.estimated_count)} messages)")

    def _display_batch_context(self, batch_target: BatchTarget) -> None:
        """Display batch context information.

        Args:
            batch_target: BatchTarget with selection info
        """
        print("\n" + "=" * 60)
        print("BATCH TARGET")
        print("=" * 60)
        if batch_target.target_type == "domain":
            print(f"Type:        Domain-wide")
            print(f"Domain:      {batch_target.value}")
        else:
            print(f"Type:        Specific sender")
            print(f"Email:       {batch_target.value}")
        print(f"Messages:    {format_count(batch_target.estimated_count)}")
        if batch_target.sample_messages:
            print("\nSample subjects:")
            for msg in batch_target.sample_messages[:3]:
                subject = msg.subject[:50] + "..." if len(msg.subject) > 50 else msg.subject
                print(f"  • {subject}")
        print("=" * 60)

    def _save_batch_rule(self) -> int:
        """Save rule in batch mode (simplified workflow).

        Returns:
            0 to continue batch, 1 to save and exit, 130 to exit without saving
        """
        # Try to generate and save the rule, handling validation errors gracefully
        while True:
            try:
                rule = self.rule_builder.generate_rule()
                break  # Rule is valid, proceed to save
            except ValueError as e:
                error_msg = str(e)
                if "must have either 'contains' or 'regex'" in error_msg:
                    # Extract condition number
                    import re
                    match = re.search(r"Condition (\d+)", error_msg)
                    if match:
                        condition_idx = int(match.group(1)) - 1
                        if self._fix_missing_match_type(condition_idx):
                            continue  # Retry with fixed condition
                        else:
                            return 130  # User cancelled
                elif "cannot have both 'contains' and 'regex'" in error_msg:
                    # Extract condition number
                    import re
                    match = re.search(r"Condition (\d+)", error_msg)
                    if match:
                        condition_idx = int(match.group(1)) - 1
                        if self._fix_duplicate_match_type(condition_idx):
                            continue  # Retry with fixed condition
                        else:
                            return 130  # User cancelled
                elif "empty match value" in error_msg:
                    # Extract condition number
                    import re
                    match = re.search(r"Condition (\d+)", error_msg)
                    if match:
                        condition_idx = int(match.group(1)) - 1
                        if self._fix_empty_value(condition_idx):
                            continue  # Retry with fixed condition
                        else:
                            return 130  # User cancelled
                elif "Action" in error_msg and ("requires" in error_msg or "missing" in error_msg):
                    # Action-related error
                    if self._fix_action_error(error_msg):
                        continue  # Retry with fixed action
                    else:
                        # Can't fix, show error and options
                        print(f"\n⚠️  Validation Error: {error_msg}")
                        print("\nWhat would you like to do?")
                        print("  1. Discard and try next email")
                        print("  2. Exit batch mode")
                        choice = input("\nChoice (1-2): ").strip() or "1"
                        if choice == "2":
                            return 130  # Exit
                        else:
                            return 0  # Continue to next email
                else:
                    # Generic validation error - show error and options
                    print(f"\n⚠️  Validation Error: {error_msg}")
                    print("\nWhat would you like to do?")
                    print("  1. Discard and try next email")
                    print("  2. Exit batch mode")
                    choice = input("\nChoice (1-2): ").strip() or "1"
                    if choice == "2":
                        return 130  # Exit
                    else:
                        return 0  # Continue to next email

        # Display rule JSON
        print("\n" + "=" * 60)
        print("RULE PREVIEW:")
        print("=" * 60)
        print(json.dumps(rule, indent=2))
        print("=" * 60)

        # Save options
        print("\nOptions:")
        print("  1. Save and continue to next email (default - press Enter)")
        print("  2. Save and exit batch mode")
        print("  3. Discard and continue")
        print("  4. Cancel")

        choice = input("\nChoice (1-4): ").strip()

        # Default to option 1 (save and continue) if user presses Enter
        if not choice:
            choice = "1"

        if choice == "1" or choice == "2":
            success, message = save_rule(rule, self.config.paths.rules_dir)
            if not success:
                print(f"❌ {message}")
                return 1
            # ISSUE #2 FIX: Standardized success message format
            print(f"✓ Rule: {message}")
            return 0 if choice == "1" else 130
        elif choice == "3":
            return 0  # Continue without saving
        else:
            return 130  # Exit

    def _fix_missing_match_type(self, condition_idx: int) -> bool:
        """Fix a condition missing a match type field.

        Args:
            condition_idx: Index of the condition to fix

        Returns:
            True if fixed successfully, False if user cancelled
        """
        if condition_idx >= len(self.rule_builder.conditions):
            return False

        condition = self.rule_builder.conditions[condition_idx]
        print(f"\n⚠️  Condition {condition_idx + 1} is missing a match type.")
        print(f"   Header: {condition.get('header', '<unknown>')}")
        print("\nWhich match type would you like to use?")
        print("  1. Equals (exact match)")
        print("  2. Not Equals (does not match exactly)")
        print("  3. Contains (substring match - case insensitive)")
        print("  4. Not Contains (does not contain substring)")
        print("  5. Regex (regular expression)")
        print("  6. Not Regex (does not match regex pattern)")
        print("  7. Cancel")

        choice = input("\nChoice (1-7): ").strip() or "7"

        # Map of choices to match type keys and prompts
        choice_map = {
            "1": ("equals", "Enter the exact value to match: "),
            "2": ("not_equals", "Enter the value NOT to match: "),
            "3": ("contains", "Enter the substring to match: "),
            "4": ("not_contains", "Enter the substring NOT to match: "),
            "5": ("regex", "Enter the regex pattern to match: "),
            "6": ("not_regex", "Enter the regex pattern NOT to match: "),
        }

        if choice in choice_map:
            match_type_key, prompt = choice_map[choice]
            value = input(prompt).strip()
            if not value:
                print("⚠️  Match value cannot be empty.")
                return self._fix_missing_match_type(condition_idx)  # Retry

            # Clear all existing match types
            for key in ["contains", "regex", "equals", "not_equals", "not_contains", "not_regex"]:
                condition.pop(key, None)

            # Set the new match type
            condition[match_type_key] = value
            print("✓ Condition fixed!")
            return True
        else:
            return False

    def _fix_duplicate_match_type(self, condition_idx: int) -> bool:
        """Fix a condition with multiple match type fields.

        Args:
            condition_idx: Index of the condition to fix

        Returns:
            True if fixed successfully, False if user cancelled
        """
        if condition_idx >= len(self.rule_builder.conditions):
            return False

        condition = self.rule_builder.conditions[condition_idx]

        # Find all present match types
        match_type_keys = ["contains", "regex", "equals", "not_equals", "not_contains", "not_regex"]
        present_keys = [key for key in match_type_keys if key in condition]

        if len(present_keys) < 2:
            # Not actually duplicate - shouldn't happen
            return False

        print(f"\n⚠️  Condition {condition_idx + 1} has multiple match types:")
        print(f"   Header: {condition.get('header', '<unknown>')}")

        for i, key in enumerate(present_keys, 1):
            print(f"   {i}. {key}: {condition.get(key)}")

        print("\nWhich one should be kept?")
        for i in range(1, len(present_keys) + 1):
            print(f"  {i}. Keep '{present_keys[i-1]}'")
        print(f"  {len(present_keys) + 1}. Cancel")

        choice = input(f"\nChoice (1-{len(present_keys) + 1}): ").strip() or str(len(present_keys) + 1)

        try:
            choice_idx = int(choice) - 1
            if 0 <= choice_idx < len(present_keys):
                # Keep the selected key, remove others
                keep_key = present_keys[choice_idx]
                for key in present_keys:
                    if key != keep_key:
                        condition.pop(key, None)
                print("✓ Condition fixed!")
                return True
        except (ValueError, IndexError):
            pass

        return False

    def _fix_empty_value(self, condition_idx: int) -> bool:
        """Fix a condition with an empty match value.

        Args:
            condition_idx: Index of the condition to fix

        Returns:
            True if fixed successfully, False if user cancelled
        """
        if condition_idx >= len(self.rule_builder.conditions):
            return False

        condition = self.rule_builder.conditions[condition_idx]
        match_type = "contains" if "contains" in condition else "regex"

        print(f"\n⚠️  Condition {condition_idx + 1} has an empty match value.")
        print(f"   Header: {condition.get('header', '<unknown>')}")
        print(f"   Match type: {match_type}")

        value = input(f"Enter the {match_type} pattern to match: ").strip()
        if not value:
            print("⚠️  Match value cannot be empty.")
            return self._fix_empty_value(condition_idx)  # Retry

        condition[match_type] = value
        print("✓ Condition fixed!")
        return True

    def _fix_action_error(self, error_msg: str) -> bool:
        """Fix an action-related validation error.

        Args:
            error_msg: The error message describing the issue

        Returns:
            True if fixed or error doesn't apply to actions, False if user cancelled
        """
        import re

        # Extract action number
        match = re.search(r"Action (\d+)", error_msg)
        if not match:
            return False

        action_idx = int(match.group(1)) - 1
        if action_idx >= len(self.rule_builder.actions):
            return False

        action = self.rule_builder.actions[action_idx]

        # Handle different action errors
        if "requires a target folder" in error_msg:
            print(f"\n⚠️  Action {action_idx + 1} (move) requires a target folder.")
            target = input("Enter the target folder path: ").strip()
            if not target:
                print("⚠️  Target folder cannot be empty.")
                return self._fix_action_error(error_msg)  # Retry
            action["target"] = target
            print("✓ Action fixed!")
            return True

        elif "requires at least one keyword" in error_msg:
            action_type = action.get("type", "unknown")
            print(f"\n⚠️  Action {action_idx + 1} ({action_type}) requires at least one keyword.")
            keyword = input("Enter a keyword to add: ").strip()
            if not keyword:
                print("⚠️  Keyword cannot be empty.")
                return self._fix_action_error(error_msg)  # Retry
            action["keywords"] = [keyword]
            print("✓ Action fixed!")
            return True

        return False

    def _validate_cache(self) -> bool:
        """Check if cache exists and has data.

        Returns:
            True if cache is valid, False otherwise
        """
        if not self.cache_engine:
            return False

        try:
            cursor = self.cache_engine.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM headers")
            count = cursor.fetchone()[0] or 0

            if count == 0:
                print("\nError: Cache is empty. Please run 'build-cache' first.")
                return False

            return True
        except Exception as e:
            print(f"\nError accessing cache: {e}")
            return False

    def _display_welcome(self):
        """Display welcome message with cache statistics."""
        cursor = self.cache_engine.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM headers")
        message_count = cursor.fetchone()[0] or 0

        print("\n" + "=" * 60)
        print("Welcome to the IMAPFilter Rule Wizard")
        print("=" * 60)
        print(f"Checking cache... {format_count(message_count)} messages found")
        print("\nReady to create a new rule!")
        print("=" * 60)
        input("\nPress Enter to continue...")
        print()

    def _add_conditions_loop(self) -> Optional[bool]:
        """Add conditions in a loop until user says no.

        Returns:
            True if at least one condition was added
            False if no conditions were added
            None if user cancelled
        """
        while True:
            if self.rule_builder.conditions:
                # Ask if they want to add another
                response = prompt_yes_no(
                    f"You have {len(self.rule_builder.conditions)} condition(s). "
                    "Add another condition?",
                    default=False
                )
                if not response:  # No more conditions
                    return True
            else:
                # First condition
                print("\nLet's add the first condition.")

            # Add a condition
            if not self._add_single_condition():
                if not self.rule_builder.conditions:
                    return None  # Cancelled on first condition
                # Otherwise continue loop

    def _display_condition_stack(self) -> None:
        """Display the current conditions being built as a stack."""
        if not self.rule_builder.conditions:
            return

        print("\nCurrent conditions:")
        print("=" * 60)
        for i, cond in enumerate(self.rule_builder._flat_conditions, 1):
            formatted = self._format_condition(cond)
            if i < len(self.rule_builder._flat_conditions):
                print(f"  {i}. {formatted}")
                print(f"     ↓ AND ↓")
            else:
                print(f"  {i}. {formatted}")
        print("=" * 60)

    def _ask_condition_intent(self, field: str) -> Optional[str]:
        """Ask user how they want to add a condition (Exclude/Refine vs Include Additional).

        Args:
            field: The header field being added (from, to, subject, etc)

        Returns:
            "refine" for Exclude/Refine, "include" for Include Additional, or None if cancelled
        """
        if not self.rule_builder.conditions:
            # First condition - no intent needed
            return "refine"

        print(f"\nHow would you like to add this {field.upper()} condition?")
        print()
        print("  [1] Exclude/Refine: Narrow down from current selection")
        print("      (Show values only from messages matching current criteria)")
        print()
        print("  [2] Include Additional: Add criteria beyond current selection")
        print("      (Show values from all uncovered messages)")
        print()

        choice = input("Choice (1-2): ").strip()
        if choice == "1":
            return "refine"
        elif choice == "2":
            return "include"
        else:
            return None

    def _add_single_condition(self) -> bool:
        """Add a single condition to the rule.

        Returns:
            True if condition was added, False if cancelled
        """
        # Step 0: Display existing conditions stack
        self._display_condition_stack()

        # Step 1: Select header field
        field = self._select_header_field()
        if not field:
            return False

        # Step 1b: Ask about condition intent (new Option C)
        intent = self._ask_condition_intent(field)
        if intent is None:
            return False

        # Step 2: Select field value
        value = self._select_field_value(field, intent=intent)
        if not value:
            return False

        # Step 3: Handle special case for "[All from domain]" selection
        # Uses same conversion logic as batch mode for consistency
        # ISSUE #5 FIX: Use helper method to check for domain selection
        if self._is_domain_selection(value):
            pattern = self._convert_domain_selection_to_pattern(value)
            print(f"\nUsing domain pattern: {pattern}")
        else:
            # Step 3b: Suggest patterns and let user choose
            pattern = self._suggest_patterns(field, value)
            if pattern is None:
                return False

        # Step 4: Select match type
        match_type = self._select_match_type(field)
        if not match_type:
            return False

        # Add condition to builder
        self.rule_builder.add_condition(field, match_type, pattern)
        print(f"\n✓ Condition: {field} {match_type} '{pattern}'")

        # NEW: Offer to view matching emails
        if prompt_yes_no("View matching emails in cache?", default=False):
            self._view_matching_cache()

        return True

    def _select_header_field(self) -> Optional[str]:
        """Let user choose a header field.

        Returns:
            Selected header field name, or None if cancelled
        """
        print("\nSelect header field:")
        print("  1. From (sender address)")
        print("  2. To (recipient address)")
        print("  3. Subject")
        print("  4. List-ID")
        print("  5. Reply-To")
        print("  6. Other (enter custom header)")

        choice = input("  > ").strip()

        field_map = {
            "1": "from",
            "2": "to",
            "3": "subject",
            "4": "list-id",
            "5": "reply-to",
        }

        if choice in field_map:
            return field_map[choice]
        elif choice == "6":
            # ISSUE #4 FIX: Standardize capitalization in prompts
            custom = input("Enter Header Name: ").strip().lower()
            return custom if custom else None
        else:
            print("Invalid choice. Please try again.")
            return self._select_header_field()

    def _select_field_value(self, field: str, intent: str = "refine") -> Optional[str]:
        """Show filterable list and let user pick value.

        Args:
            field: Header field name (from, to, subject, etc.)
            intent: "refine" to show values from matching messages, "include" for all uncovered

        Returns:
            Selected value, or None if cancelled
        """
        # Get values from cache
        # Email fields use two-step selector with domain grouping and consolidation
        if field in ("from", "to", "cc", "bcc", "reply-to"):
            return self._select_email_address_two_step(field, intent=intent)
        elif field == "subject":
            if intent == "refine" and self.rule_builder.conditions:
                # Get subjects from messages matching current conditions
                items = self._extract_values_from_matching_messages(field)
            elif intent == "include":
                # Include intent: show from all uncovered messages (not covered by existing rules)
                items = self._extract_uncovered_values(field)
            else:
                # Remove 500 limit - fetch all unique subjects
                items = self.cache_engine.extract_unique_subjects(limit=999999)
        else:
            if intent == "refine" and self.rule_builder.conditions:
                # Get values from messages matching current conditions
                items = self._extract_values_from_matching_messages(field)
            elif intent == "include":
                # Include intent: show from all uncovered messages (not covered by existing rules)
                items = self._extract_uncovered_values(field)
            else:
                items = self.cache_engine.extract_other_header(field, limit=1000)

        if not items:
            print(f"\nNo values found for header '{field}' in cache.")
            manual = input("Enter value manually (or press Enter to skip): ").strip()
            return manual if manual else None

        # Show filterable list
        print(f"\nShowing {len(items)} unique values for '{field}'...")
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
        input("Press Enter to open selector...")

        selector = FilterableListSelector(items, f"Select {field.title()}")
        selection_result = curses.wrapper(selector.run)

        if selection_result is None:
            # ISSUE #1 FIX: Offer manual entry on cancellation (consistent with keyword selection)
            print("\nSelection cancelled.")
            manual_response = prompt_yes_no("Would you like to enter the value manually?", default=True)
            if manual_response:
                manual_value = input(f"Enter {field} value: ").strip()
                if manual_value:
                    return manual_value
            return None

        # Extract label from (label, data) tuple returned by selector
        if isinstance(selection_result, tuple):
            selected = selection_result[0]
        else:
            selected = selection_result

        # Offer post-selection editing for email fields
        if field in ("to", "reply-to", "cc", "bcc"):
            edited_value = self._edit_email_address(selected, f"{field} address")
            return edited_value if edited_value is not None else None

        # ISSUE #3 FIX: Offer confirmation menu for non-email fields
        # This ensures consistent editing opportunities across all field types
        print(f"\nSelected {field}: {selected}")
        response = prompt_yes_no("Use this value?", default=True)
        if response:
            return selected
        else:
            # User wants to change it
            print("Options:")
            print("  1. Select a different value from the list")
            print("  2. Enter a completely different value")
            print("  3. Cancel (go back)")
            choice = input("  > ").strip()

            if choice == "1":
                # Recursively call to select again, preserving the intent
                return self._select_field_value(field, intent=intent)
            elif choice == "2":
                # Manual entry
                manual = input(f"Enter new {field} value: ").strip()
                return manual if manual else None
            else:
                return None

    def _select_email_address_two_step(self, field: str = "from", intent: str = "refine") -> Optional[str]:
        """Show two-step email address selector with domain grouping and consolidation.

        Works with any email field (from, to, cc, bcc, reply-to) by using domain grouping
        and consolidating addresses with different display names.

        Args:
            field: Email field name ('from', 'to', 'cc', 'bcc', 'reply-to')
            intent: "refine" to show from current criteria, "include" for all uncovered

        Returns:
            Selected email address, '[All {field} domain]' for domain-wide selection,
            or None if cancelled

        Note:
            The '[All {field} domain]' format is consistent with batch mode and gets
            converted to '@domain' pattern by _convert_domain_selection_to_pattern()
        """
        # Step 1: Load all unique addresses for the specified field
        print(f"\nLoading {field} addresses...")

        # In batch mode with refine intent, use cluster data (uncovered messages only)
        if self.batch_mode_active and self.batch_selected_cluster and field == "from" and intent == "refine":
            # Use senders from the domain cluster (uncovered messages only)
            all_addresses = [(addr, count) for addr, count in self.batch_selected_cluster.senders.items()]
            print(f"(Showing {len(all_addresses)} uncovered {field} addresses)")
        elif intent == "refine" and self.rule_builder.conditions and field in ("from", "to"):
            # Refine intent with existing conditions: show addresses from matching messages
            all_addresses = self._extract_values_from_matching_messages(field)
        elif intent == "include":
            # Include intent: show from all uncovered messages (not covered by existing rules)
            all_addresses = self._extract_uncovered_values(field)
        elif field == "from":
            all_addresses = self.cache_engine.extract_unique_from_addresses(limit=2000)
        elif field == "to":
            all_addresses = self.cache_engine.extract_unique_to_addresses(limit=2000)
        else:
            # Use generic extraction for cc, bcc, reply-to
            all_addresses = self.cache_engine.extract_other_header(field, limit=2000)

        if not all_addresses:
            print(f"No {field} addresses found in cache.")
            return None

        # Step 2: Compute domain counts
        domain_counts = compute_domain_counts(all_addresses)

        if not domain_counts:
            print(f"Could not extract domains from {field} addresses.")
            return None

        # Step 3: First selector - select domain
        # In batch mode with refine intent, use the pre-selected domain from cluster
        if self.batch_mode_active and self.batch_selected_cluster and field == "from" and intent == "refine":
            selected_domain = self.batch_selected_cluster.domain
            print(f"\nUsing batch domain: {selected_domain}")
        else:
            print(f"\nFound {len(domain_counts)} unique domains...")
            print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
            input("Press Enter to select domain...")

            selector = FilterableListSelector(domain_counts, f"Select Domain for {field.upper()}")
            selection_result = curses.wrapper(selector.run)

            # Extract label from (label, data) tuple returned by selector
            if isinstance(selection_result, tuple):
                selected_domain = selection_result[0]
            else:
                selected_domain = selection_result

        if not selected_domain:
            # ISSUE #6 FIX: Offer retry on domain cancellation
            print("\nDomain selection cancelled.")
            retry_response = prompt_yes_no("Would you like to try again?", default=True)
            if retry_response:
                return self._select_email_address_two_step(field, intent=intent)
            return None

        # Step 4: Second selector - select email from domain
        domain_emails = get_emails_for_domain(all_addresses, selected_domain)

        if not domain_emails:
            print(f"\nNo {field} addresses found for domain {selected_domain}")
            return None

        # Consolidate email addresses, preserving variations for expand/collapse
        email_groups = consolidate_email_addresses(domain_emails, preserve_variations=True)

        # Convert to expandable item format
        formatted_emails = create_expandable_email_items(email_groups)

        # Prepend "All {field} domain" option for consistency with batch mode
        # ISSUE #5 FIX: Use helper method for consistent format
        domain_total = sum(group.total_count for group in email_groups)
        domain_option = ExpandableItem(
            label=self._format_domain_selection(selected_domain, field),
            count=domain_total,
            is_expandable=False,
            data=None,
            indent_level=0
        )
        selector_items = [domain_option] + formatted_emails

        if len(email_groups) == 1:
            # Auto-select if only one email (offer all or specific option)
            # ISSUE #5 FIX: Use helper method for consistent format
            domain_selection = self._format_domain_selection(selected_domain, field)
            print(f"\nFound 2 options for {selected_domain}:")
            print("(Use arrow keys to navigate, Enter to select, ESC to cancel)")
            input("Press Enter to select option...")

            single_selector_items = [domain_option] + formatted_emails
            selector = FilterableListSelector(single_selector_items, f"Select Email Option from {selected_domain}")
            result = curses.wrapper(selector.run)

            if not result:
                # User cancelled
                print("\nEmail option selection cancelled.")
                retry_response = prompt_yes_no("Would you like to try again?", default=True)
                if retry_response:
                    return self._select_email_address_two_step(field, intent=intent)
                return None

            # Handle both tuple (label, data) from new format and string from legacy
            if isinstance(result, tuple):
                selected_label, selected_data = result
            else:
                # Backward compatibility for legacy string returns
                selected_label = result
                selected_data = None

            # If user selected domain-wide format, return it
            if self._is_domain_selection(selected_label):
                return selected_label

            # Offer post-selection editing
            edited_email = self._edit_email_address(selected_label, f"{field} address")
            return edited_email if edited_email is not None else None

        print(f"\nFound {len(email_groups)} {field} addresses from {selected_domain}...")
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
        input("Press Enter to select email...")

        selector = FilterableListSelector(selector_items, f"Select {field.upper()} Address from {selected_domain}")
        result = curses.wrapper(selector.run)

        if not result:
            # ISSUE #6 FIX: Offer retry/recovery options on email cancellation
            print("\nEmail selection cancelled.")
            retry_response = prompt_yes_no("Would you like to try selecting again?", default=True)
            if retry_response:
                # Go back to domain selection for full retry
                print("\nRestarting from domain selection...")
                return self._select_email_address_two_step(field, intent=intent)

            # Offer alternative: continue with domain-wide match
            # ISSUE #5 FIX: Use helper method for consistent format
            domain_response = prompt_yes_no(
                f"Would you like to match all {field} addresses from {selected_domain} instead?",
                default=False
            )
            if domain_response:
                return self._format_domain_selection(selected_domain, field)

            return None

        # Handle both tuple (label, data) from new format and string from legacy
        if isinstance(result, tuple):
            selected_label, selected_data = result
        else:
            # Backward compatibility for legacy string returns
            selected_label = result
            selected_data = None

        # Check if user selected domain-wide format
        if self._is_domain_selection(selected_label):
            return selected_label

        # Extract email from consolidated label format (remove "[N display names]" suffix if present)
        actual_email = self._extract_email_from_consolidated_label(selected_label, selected_data)

        # Offer post-selection editing for specific email
        edited_email = self._edit_email_address(actual_email, f"{field} address")
        return edited_email if edited_email is not None else None

    def _suggest_patterns(self, field: str, value: str) -> Optional[str]:
        """Show pattern suggestions and let user pick one.

        Args:
            field: Header field name
            value: The selected value

        Returns:
            Chosen pattern string, or None if cancelled
        """
        # For email fields, extract clean address from consolidated labels
        # This handles cases like "email@domain.com [2 display names]" from expandable selectors
        display_value = value
        if field in ("from", "to", "reply-to"):
            display_value = self._extract_email_from_consolidated_label(value)

        # Fast mode: skip pattern effectiveness checking and use first pattern as-is
        if self.fast_mode:
            # In fast mode, we skip pattern counting to save 5-10 seconds per criteria
            if field in ("from", "to", "reply-to"):
                # Get just the first pattern without effectiveness checking
                patterns = self.email_extractor.suggest_patterns(display_value, self.cache_engine, fast_mode=True)
                if patterns:
                    print(f"\n{field.title()}: {display_value}")
                    print(f"  Using pattern: {patterns[0][0]}")
                    return patterns[0][0]
                else:
                    return display_value
            elif field == "subject":
                patterns = self.subject_extractor.suggest_patterns(value, self.cache_engine, fast_mode=True)
                if patterns:
                    print(f"\n{field.title()}: {value}")
                    print(f"  Using pattern: {patterns[0][0]}")
                    return patterns[0][0]
                else:
                    return value
            else:
                # For other fields, just use the value as-is
                print(f"\n{field.title()}: {value}")
                return value

        # Normal mode: generate suggestions and let user pick
        # Generate suggestions based on field type
        if field in ("from", "to", "reply-to"):
            patterns = self.email_extractor.suggest_patterns(display_value, self.cache_engine)
        elif field == "subject":
            patterns = self.subject_extractor.suggest_patterns(value, self.cache_engine)
        else:
            # For other fields, just use the value as-is
            patterns = [(value, "Exact match", 1)]

        if not patterns:
            return display_value

        print(f"\n{field.title()}: {display_value}")
        print("\nSuggested patterns:")
        for i, (pattern, description, count) in enumerate(patterns, 1):
            marker = " [RECOMMENDED]" if i == 2 and len(patterns) > 2 else ""
            print(f"  {i}. {pattern} ({description} - {format_count(count)} messages){marker}")
        print(f"  {len(patterns) + 1}. [Edit manually]")

        choice = input("  > ").strip()

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(patterns):
                return patterns[idx][0]
            elif idx == len(patterns):
                # Manual edit
                manual = input(f"Enter pattern for {field}: ").strip()
                return manual if manual else display_value
            else:
                print("Invalid choice, using original value.")
                return display_value
        except ValueError:
            print("Invalid input, using original value.")
            return display_value

    def _select_match_type(self, field: str) -> Optional[str]:
        """Prompt for match type with field-specific guidance.

        Args:
            field: The header field name (e.g., "from", "subject", "list-id")

        Returns:
            Match type string: 'equals', 'not_equals', 'contains', 'not_contains',
                              'regex', or 'not_regex'
            None if user cancels
        """
        is_email_field = field in ("from", "to", "cc", "bcc", "reply-to")

        if is_email_field:
            print("\n⚠️  NOTE: Email addresses often include display names like \"Name <email@domain.com>\"")
            print("   Use 'Contains' to match any display name format.")
            print("   'Equals' requires exact match (rarely what you want for email addresses).")
            print("\nMatch type:")
            print("  1. Contains (substring match - RECOMMENDED)")
            print("  2. Not Contains (exclude substring - RECOMMENDED)")
            print("  3. Equals (exact match - use with caution)")
            print("  4. Not Equals (exclude exact match - use with caution)")
            print("  5. Regex (regular expression)")
            print("  6. Not Regex (exclude regex pattern)")

            choice = input("  > ").strip()

            match_types = {
                "1": "contains",
                "2": "not_contains",
                "3": "equals",
                "4": "not_equals",
                "5": "regex",
                "6": "not_regex",
            }

            if choice in match_types:
                return match_types[choice]
            elif choice == "":
                # Empty input - use recommended default
                print("Using recommended: 'contains'")
                return "contains"
            else:
                print("Invalid choice, using 'contains' by default.")
                return "contains"
        else:
            # Non-email fields: keep current behavior
            print("\nMatch type:")
            print("  1. Equals (exact match)")
            print("  2. Not Equals (does not match exactly)")
            print("  3. Contains (substring match - case insensitive)")
            print("  4. Not Contains (does not contain substring)")
            print("  5. Regex (regular expression)")
            print("  6. Not Regex (does not match regex pattern)")

            choice = input("  > ").strip()

            match_types = {
                "1": "equals",
                "2": "not_equals",
                "3": "contains",
                "4": "not_contains",
                "5": "regex",
                "6": "not_regex",
            }

            if choice in match_types:
                return match_types[choice]
            else:
                print("Invalid choice, using 'contains' by default.")
                return "contains"

    def _configure_logic(self) -> bool:
        """Configure logic operator for multiple conditions.

        Returns:
            True if configured, False if cancelled
        """
        print("\nMultiple conditions found. How should they be combined?")
        print("  1. ALL (AND) - Message must match ALL conditions")
        print("  2. ANY (OR) - Message must match ANY condition")

        choice = input("  > ").strip()

        if choice == "1":
            self.rule_builder.set_logic("all")
            print("Logic set to: ALL (AND)")
        elif choice == "2":
            self.rule_builder.set_logic("any")
            print("Logic set to: ANY (OR)")
        else:
            print("Invalid choice, using 'ANY' by default.")
            self.rule_builder.set_logic("any")

        return True

    def _configure_grouping(self) -> Optional[GroupingSpec]:
        """Configure grouping for mixed AND/OR logic in conditions.

        Allows users to group conditions and assign different logic operators
        to each group, enabling nested boolean logic like:
        "FROM condition AND (SUBJECT condition1 OR condition2)"

        Returns:
            GroupingSpec if grouping was configured, None for simple flat logic
        """
        if len(self.rule_builder._flat_conditions) < 2:
            return None  # Single condition - no grouping needed

        print(f"\nYou have {len(self.rule_builder._flat_conditions)} conditions.")
        self._display_conditions_numbered()

        print("\nHow should these be combined?")
        print("  1. ALL conditions (AND) - message must match all")
        print("  2. ANY condition (OR) - message must match any")
        print("  3. Create groups for mixed logic (advanced)")

        choice = input("  > ").strip()

        if choice == "1":
            self.rule_builder._flat_logic = "all"
            return None  # Simple flat structure
        elif choice == "2":
            self.rule_builder._flat_logic = "any"
            return None  # Simple flat structure
        elif choice == "3":
            return self._create_grouping_interactive()
        else:
            # Default to ANY
            self.rule_builder._flat_logic = "any"
            return None

    def _create_grouping_interactive(self) -> GroupingSpec:
        """Interactive workflow to create condition groups.

        Users select which conditions to group together, and set the logic
        for each group. Returns a GroupingSpec that describes the grouping.
        """
        conditions = self.rule_builder._flat_conditions
        ungrouped = set(range(len(conditions)))
        groups: List[ConditionGroup] = []

        while ungrouped:
            print("\n" + "=" * 60)
            print("GROUPING CONDITIONS")
            print("=" * 60)

            # Show available conditions
            print("\nAvailable conditions:")
            for i in sorted(ungrouped):
                cond = conditions[i]
                print(f"  {i+1}. {self._format_condition(cond)}")

            if len(ungrouped) == 1:
                # Last condition - auto-add as single item
                idx = list(ungrouped)[0]
                groups.append(ConditionGroup(indices=[idx], logic="any"))
                break

            # Get group selection
            print("\nEnter condition numbers to group (e.g., '2,3,4')")
            print("or press Enter to keep remaining separate:")
            selection = input("  > ").strip()

            if not selection:
                # Keep remaining separate
                for idx in sorted(ungrouped):
                    groups.append(ConditionGroup(indices=[idx], logic="any"))
                break

            # Parse selection
            try:
                selected_indices = self._parse_number_list(selection, len(conditions))
                if not selected_indices or not all(i in ungrouped for i in selected_indices):
                    print("Invalid selection. Try again.")
                    continue
            except ValueError as e:
                print(f"Invalid format: {e}")
                continue

            # Set logic for this group
            print(f"\nGroup: {len(selected_indices)} conditions")
            for idx in selected_indices:
                print(f"  • {self._format_condition(conditions[idx])}")

            print("\nThis group should match:")
            print("  1. ANY (OR) - match if any condition is true")
            print("  2. ALL (AND) - match only if all are true")
            group_logic_choice = input("  > ").strip()

            group_logic = "all" if group_logic_choice == "2" else "any"
            groups.append(ConditionGroup(indices=selected_indices, logic=group_logic))

            # Remove grouped conditions
            ungrouped -= set(selected_indices)

        # Set overall logic
        print("\n" + "=" * 60)
        print("OVERALL LOGIC")
        print("=" * 60)
        self._display_group_structure(groups, conditions)

        print("\nAll groups/conditions must match:")
        print("  1. ALL (AND) - message must satisfy all groups")
        print("  2. ANY (OR) - message must satisfy any group")
        overall_choice = input("  > ").strip()

        overall_logic = "all" if overall_choice == "1" else "any"

        return GroupingSpec(groups=groups, overall_logic=overall_logic)

    def _parse_number_list(self, text: str, max_num: int) -> List[int]:
        """Parse comma-separated numbers into 0-based indices.

        Args:
            text: Input like "2,3,4"
            max_num: Maximum allowed value (for validation)

        Returns:
            List of 0-based indices [1, 2, 3]

        Raises:
            ValueError: If input is invalid
        """
        parts = [p.strip() for p in text.split(",")]
        indices = []
        for part in parts:
            try:
                num = int(part)
                if num < 1 or num > max_num:
                    raise ValueError(f"Numbers must be between 1 and {max_num}")
                indices.append(num - 1)  # Convert to 0-based
            except ValueError:
                raise ValueError(f"Invalid number: '{part}'")
        return indices

    def _format_condition(self, cond: dict) -> str:
        """Format a condition dict for display.

        Args:
            cond: Condition dictionary

        Returns:
            Formatted string like "from contains 'sender@example.com'"
        """
        header = cond.get("header", "?")
        for op in ["contains", "equals", "regex", "not_contains", "not_equals", "not_regex"]:
            if op in cond:
                value = cond[op]
                if len(value) > 40:
                    value = value[:37] + "..."
                return f"{header} {op} '{value}'"
        return str(cond)

    def _display_conditions_numbered(self) -> None:
        """Display all conditions with numbers for selection."""
        for i, cond in enumerate(self.rule_builder._flat_conditions, 1):
            print(f"  {i}. {self._format_condition(cond)}")

    def _display_group_structure(
        self, groups: List[ConditionGroup], conditions: List[dict]
    ) -> None:
        """Display visual tree of groups.

        Args:
            groups: List of ConditionGroup objects
            conditions: Original list of all conditions
        """
        print("\nRule structure:")
        for i, group in enumerate(groups, 1):
            if len(group.indices) == 1:
                cond = conditions[group.indices[0]]
                print(f"  → {self._format_condition(cond)}")
            else:
                logic_name = "ANY" if group.logic == "any" else "ALL"
                print(f"  → [Group {i}: {logic_name} of {len(group.indices)} conditions]")
                for idx in group.indices:
                    print(f"      • {self._format_condition(conditions[idx])}")

    def _extract_uncovered_values(self, field: str) -> List[Tuple[str, int]]:
        """Extract unique values for a field from UNCOVERED messages only.

        This is used for "Include Additional" intent - show values from all
        uncovered messages across all domains, not just matching current conditions.

        Args:
            field: The header field to extract (subject, to, list-id, etc.)

        Returns:
            List of (value, count) tuples from uncovered messages only, sorted by count descending
        """
        from collections import Counter
        from core.rule_engine import _extract_raw_header, _parse_header_map

        if not self.coverage_analyzer:
            # Fallback to cache if no coverage analyzer
            if field == "subject":
                return self.cache_engine.extract_unique_subjects(limit=999999)
            else:
                return self.cache_engine.extract_other_header(field, limit=1000)

        try:
            print(f"\nExtracting {field} values from uncovered messages...")

            # Get all uncovered messages from coverage analyzer
            uncovered_messages = self.coverage_analyzer.get_uncovered_messages()

            if not uncovered_messages:
                print(f"No uncovered messages found")
                return []

            counter = Counter()
            processed = 0

            # Build a map of (folder, uid) tuples for bulk lookup
            message_ids = [(msg.folder, msg.uid) for msg in uncovered_messages]

            # Fetch all uncovered messages in one query for better performance (instead of N+1)
            cursor = self.cache_engine.conn.cursor()

            # Group messages by folder for more efficient querying
            messages_by_folder = {}
            for folder, uid in message_ids:
                if folder not in messages_by_folder:
                    messages_by_folder[folder] = []
                messages_by_folder[folder].append(uid)

            # Fetch all messages using IN clause for each folder
            for folder, uids in messages_by_folder.items():
                if not uids:
                    continue

                # Use WHERE uid IN (...) for bulk fetch instead of individual queries
                placeholders = ','.join('?' * len(uids))
                query = f"SELECT uid, data FROM headers WHERE folder = ? AND uid IN ({placeholders})"
                cursor.execute(query, [folder] + uids)

                for row in cursor.fetchall():
                    processed += 1
                    if processed % 1000 == 0:
                        print(f"  Processed {processed:,} uncovered messages...", end="\r")

                    if not row:
                        continue

                    uid = row[0] if len(row) > 0 else ""
                    data = row[1] if len(row) > 1 else ""

                    # Use cached header parsing to avoid repeated parsing
                    header = self._get_parsed_header(folder, uid, data)

                    # Extract the field value
                    if field == "subject":
                        value = header.get("subject", "").strip()
                    elif field in ("to", "cc", "bcc"):
                        value = header.get(field, "").strip()
                    elif field == "from":
                        value = header.get("from", "").strip()
                    elif field == "list-id":
                        value = header.get("list-id", "").strip()
                    else:
                        value = header.get(field, "").strip()

                    if value:
                        counter[value] += 1

            print(f"  Processed {processed:,} uncovered messages total")
            result = counter.most_common()  # Get all values, sorted by count
            print(f"  Found {len(result)} unique values in uncovered messages")
            return result

        except Exception as e:
            print(f"\n⚠️ Error extracting uncovered values: {e}")
            print("Falling back to all cached values...")
            # Fallback
            if field == "subject":
                return self.cache_engine.extract_unique_subjects(limit=999999)
            else:
                return self.cache_engine.extract_other_header(field, limit=1000)

    def _extract_values_from_matching_messages(self, field: str) -> List[Tuple[str, int]]:
        """Extract unique values for a field from messages matching current conditions.

        This is used for "Refine" intent - show values that exist in messages
        matching the current rule conditions.

        Args:
            field: The header field to extract (subject, to, list-id, etc.)

        Returns:
            List of (value, count) tuples, sorted by count descending
        """
        from collections import Counter
        from core.rule_engine import _extract_raw_header, _parse_header_map, find_matching_rule

        if not self.rule_builder.conditions or not self.cache_engine:
            # Fallback to regular extraction if no conditions
            if field == "subject":
                return self.cache_engine.extract_unique_subjects(limit=999999)
            else:
                return self.cache_engine.extract_other_header(field, limit=1000)

        try:
            print(f"\nFiltering cache for {field} values matching current conditions...")

            # Build a temporary rule from current conditions for matching
            temp_rule = {
                "name": "temporary_matching_rule",
                "conditions": self.rule_builder.conditions,
                "actions": []
            }

            # Query all messages from cache
            cursor = self.cache_engine.conn.cursor()
            cursor.execute("SELECT data FROM headers")
            all_rows = cursor.fetchall()

            counter = Counter()
            checked_count = 0

            for row in all_rows:
                checked_count += 1
                if checked_count % 1000 == 0:
                    print(f"  Checked {checked_count:,} messages...", end="\r")

                data = row[0] if row else ""
                # Parse header
                raw_header = _extract_raw_header(data)
                header = _parse_header_map(raw_header)

                # Check if this message matches current conditions
                try:
                    if find_matching_rule(header, [temp_rule]):
                        # Message matches - extract field value
                        if field == "subject":
                            value = header.get("subject", "").strip()
                        elif field in ("to", "cc", "bcc"):
                            value = header.get(field, "").strip()
                        elif field == "from":
                            value = header.get("from", "").strip()
                        elif field == "list-id":
                            value = header.get("list-id", "").strip()
                        else:
                            value = header.get(field, "").strip()

                        if value:
                            counter[value] += 1
                except Exception:
                    # Skip messages that can't be parsed
                    continue

            print(f"  Checked {checked_count:,} messages total")
            result = counter.most_common(999999)
            print(f"  Found {len(result)} unique values matching criteria")
            return result

        except Exception as e:
            print(f"\n⚠️ Error filtering messages: {e}")
            print("Falling back to showing all values...")
            # Fallback
            if field == "subject":
                return self.cache_engine.extract_unique_subjects(limit=999999)
            else:
                return self.cache_engine.extract_other_header(field, limit=1000)

    def _get_imap_folders(self) -> List[str]:
        """
        Fetch folder list from IMAP server (cached for 6 hours).

        Returns:
            List of folder names (e.g., ['INBOX', 'Archive', 'Banking/NatWest'])
            Empty list if connection fails
        """
        # Try cache first
        cached_folders = self.wizard_cache.get_folders()
        if cached_folders is not None:
            print(f"Using cached folder list ({len(cached_folders)} folders)")
            return cached_folders

        # Cache miss - fetch from server
        client = None
        try:
            print("Connecting to mail server to fetch folder list...")
            client = imap_login(self.config.paths.secrets_file, self.logger)
            folders = list_all_folders(client)

            # Cache the result
            self.wizard_cache.set_folders(folders)
            print(f"Fetched and cached {len(folders)} folders")

            return folders

        except FileNotFoundError:
            print("⚠️ Error: Credentials file not found")
            return []
        except imaplib.IMAP4.error as e:
            print(f"⚠️ Error connecting to mail server: {e}")
            return []
        except Exception as e:
            print(f"⚠️ Error fetching folders: {e}")
            return []
        finally:
            if client is not None:
                try:
                    client.logout()
                except:
                    pass

    def _edit_folder_path(self, folder_path: str) -> Optional[str]:
        """Allow user to edit a folder path after selection.

        This enables using a selected folder as a template to create similar paths.
        For example: select "Banking/NatWest" and edit to "Banking/Barclays".

        Args:
            folder_path: The initially selected folder path

        Returns:
            The edited folder path, or None if cancelled
        """
        print(f"\nSelected folder: {folder_path}")
        print("\nOptions:")
        print("  1. Confirm and use this folder (or just press Enter)")
        print("  2. Edit this folder path")
        print("  3. Enter a completely different path")
        print("  4. Cancel (go back)")

        choice = input("\n  > ").strip()

        if choice == "2":
            # Edit the selected folder
            print("\nEdit folder path:")
            edited = self._input_with_prefill("  > ", folder_path)
            return edited if edited else folder_path

        elif choice == "3":
            # Enter a completely different path
            print("\nEnter a different folder path:")
            new_path = input("  > ").strip()
            return new_path if new_path else None

        elif choice == "4" or choice == "":
            # If empty choice, treat as "use this folder"
            return folder_path if not choice else None

        else:
            print("Invalid choice, using selected folder.")
            return folder_path

    def _edit_email_address(self, email: str, field_name: str = "email") -> Optional[str]:
        """
        Allow user to edit an email address after selection.

        Args:
            email: The selected email address
            field_name: Display name for the field (e.g., "email", "from address", "to address")

        Returns:
            Edited email address, or None if cancelled
        """
        print(f"\nSelected {field_name}: {email}")
        print("\nOptions:")
        print(f"  1. Confirm and use this {field_name} (or just press Enter)")
        print(f"  2. Edit this {field_name}")
        print("  3. Enter a completely different address")
        print("  4. Cancel (go back)")

        choice = input("\n  > ").strip()

        if choice in ("", "1"):
            return email
        elif choice == "2":
            print(f"\nEdit {field_name}:")
            edited = self._input_with_prefill("  > ", email)
            return edited if edited else email
        elif choice == "3":
            print(f"\nEnter new {field_name}:")
            new_email = input("  > ").strip()
            return new_email if new_email else None
        elif choice == "4":
            return None
        else:
            print("Invalid choice. Using original address.")
            return email

    def _select_target_folder(self) -> Optional[str]:
        """Show filterable list of folders from IMAP server and let user pick target.

        Returns:
            Selected folder path, or None if cancelled
        """
        # Get folders from live IMAP server
        folders = self._get_imap_folders()

        if not folders:
            # Connection failed or no folders returned - fall back to manual entry
            print("\nCouldn't fetch folders from server.")
            print("You can enter a folder path manually:")
            manual = input("Enter target folder path (e.g., 'Banking/NatWest'): ").strip()
            return manual if manual else None

        # Convert to format for FilterableListSelector: (folder_name, count)
        # Count is 0 since we don't have message counts from IMAP LIST
        items = [(folder, 0) for folder in folders]

        # Show filterable list
        print(f"\nFound {len(folders)} folders on your mail server...")
        print("You can:")
        print("  - Browse and select an existing folder")
        print("  - Type to filter folders in real-time")
        print("  - Press ESC to enter a custom folder manually")
        print()
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC for manual entry)")
        input("Press Enter to open folder selector...")

        selector = FilterableListSelector(items, "Select Target Folder")
        selection_result = curses.wrapper(selector.run)

        if selection_result is None:
            # User cancelled (ESC) - offer manual entry
            print("\nSelection cancelled.")
            print("Would you like to enter a folder path manually?")
            print("(Press Enter to skip, or type the folder path)")
            manual = input("  > ").strip()
            return manual if manual else None

        # Extract label from (label, data) tuple returned by selector
        if isinstance(selection_result, tuple):
            selected = selection_result[0]
        else:
            selected = selection_result

        # User selected a folder - offer option to edit it
        return self._edit_folder_path(selected)

    def _configure_action(self) -> bool:
        """Configure actions for matching messages (supports multiple actions).

        Returns:
            True if at least one action was added, False if cancelled
        """
        while True:
            if self.rule_builder.actions:
                # Show current actions
                print(f"\nYou have {len(self.rule_builder.actions)} action(s):")
                for i, action in enumerate(self.rule_builder.actions, 1):
                    action_type = action.get("type", "unknown")
                    if action_type == "move":
                        target = action.get("target", "(no target)")
                        print(f"  {i}. Move to '{target}'")
                    elif action_type in ("set_keywords", "remove_keywords"):
                        keywords = action.get("keywords", [])
                        print(f"  {i}. {action_type.replace('_', ' ').title()}: {keywords}")
                    else:
                        print(f"  {i}. {action_type}")

                # Ask if they want to add another
                response = prompt_yes_no("Add another action?", default=False)
                if not response:  # No more actions
                    return True
            else:
                # First action
                print("\nLet's add the first action.")

            # Add a single action
            if not self._add_single_action():
                if not self.rule_builder.actions:
                    return False  # Cancelled on first action
                # Otherwise continue loop

    def _add_single_action(self) -> bool:
        """Add a single action to the rule.

        Returns:
            True if action was added, False if cancelled
        """
        print("\nAction type:")
        print("  1. Move (move messages to a folder)")
        print("  2. Add Keywords (add keywords/labels to messages)")
        print("  3. Remove Keywords (remove keywords/labels from messages)")

        choice = input("  > ").strip()

        if choice == "1":
            target = self._select_target_folder()
            if not target:
                print("Target folder selection cancelled.")
                return False

            self.rule_builder.add_action("move", target=target)
            # ISSUE #2 FIX: Standardized success message format
            print(f"✓ Action: move to '{target}'")
            return True

        elif choice == "2":
            keywords = self._get_keywords()
            if not keywords:
                print("Keywords cancelled.")
                return False

            self.rule_builder.add_action("set_keywords", keywords=keywords)
            # ISSUE #2 FIX: Standardized success message format
            print(f"✓ Action: add keywords {keywords}")
            return True

        elif choice == "3":
            keywords = self._get_keywords()
            if not keywords:
                print("Keywords cancelled.")
                return False

            self.rule_builder.add_action("remove_keywords", keywords=keywords)
            # ISSUE #2 FIX: Standardized success message format
            print(f"✓ Action: remove keywords {keywords}")
            return True

        else:
            print("Invalid choice. Please select 1, 2, or 3.")
            return self._add_single_action()

    def _get_keywords(self) -> List[str]:
        """Get keywords from user - either select from cache or enter manually.

        Returns:
            List of keywords, empty list if cancelled
        """
        # Check if we have predefined keywords
        predefined_keywords = self.keyword_manager.get_keywords()
        predefined_count = len(predefined_keywords)

        # Try to get keywords from cache (NEW: check wizard cache first)
        try:
            # Check wizard cache first (6-hour TTL)
            keyword_tuples = self.wizard_cache.get_keywords()

            if keyword_tuples is None:
                # Cache miss - extract from database and cache result
                print("Extracting keywords from email cache...")
                keyword_tuples = self.cache_engine.extract_unique_keywords(limit=999999, min_count=1)
                self.wizard_cache.set_keywords(keyword_tuples)

        except Exception as e:
            print(f"⚠️  Could not load keywords from cache: {e}")
            keyword_tuples = []

        # Rest of method unchanged
        if predefined_count > 0:
            print(f"\n📌 {predefined_count} predefined keyword(s) available")

        # Offer options based on what we have
        if keyword_tuples or predefined_count > 0:
            print("\nChoose how to select keywords:")
            print("  1. Select from list (predefined + cached)")
            print("  2. Enter manually (comma-separated)")
            if predefined_count > 0:
                print("  3. Manage predefined keywords")

            choice = input("  > ").strip()

            if choice == "1":
                return self._select_keywords_from_list(keyword_tuples)
            elif choice == "2":
                return self._enter_keywords_manually()
            elif choice == "3" and predefined_count > 0:
                return self._manage_keywords()
            else:
                print("Invalid choice. Defaulting to manual entry.")
                return self._enter_keywords_manually()
        else:
            # No keywords in cache or predefined, go straight to manual entry
            print("\nNo predefined or cached keywords found.")
            return self._enter_keywords_manually()

    def _select_keywords_from_list(self, keyword_tuples: List[Tuple[str, int]]) -> Optional[List[str]]:
        """Show filterable list of keywords for selection.

        Displays predefined keywords first (with 📌 indicator), then cached keywords
        (with 📊 indicator), separated by a visual divider.

        Args:
            keyword_tuples: List of (keyword, count) tuples from cache

        Returns:
            List of selected keywords, or None if cancelled
        """
        # Get predefined keywords
        predefined = self.keyword_manager.get_keywords()

        # Build items list: predefined first, then cached (excluding duplicates)
        items = []

        # Add predefined keywords with 📌 indicator and 0 message count
        for kw in predefined:
            items.append((f"📌 {kw}", 0))

        # Add visual separator if there are cached keywords
        cached_keywords_dict = {kw: count for kw, count in keyword_tuples if kw not in predefined}
        if cached_keywords_dict:
            items.append(("─" * 40, 0))  # Visual separator

            # Add cached keywords with 📊 indicator
            for kw, count in keyword_tuples:
                if kw not in predefined:
                    items.append((f"📊 {kw}", count))

        # If no items, show explanation
        if not items:
            print("\nNo predefined keywords or cached keywords found.")
            return self._enter_keywords_manually()

        print("\nSelect keywords to add/remove:")
        print("  📌 PREDEFINED KEYWORDS - always available")
        print("  📊 CACHED KEYWORDS - from your messages")
        print("\nYou can:")
        print("  - Browse and select keywords")
        print("  - Type to filter in real-time")
        print("  - Press ESC to cancel and enter manually")
        print()
        print("(Use arrow keys to navigate, type to filter, Enter to select, ESC to cancel)")
        input("Press Enter to open keyword selector...")

        # Show filterable selector
        selector = FilterableListSelector(items, "Select Keywords")
        selection_result = curses.wrapper(selector.run)

        if selection_result is None:
            # User cancelled - offer manual entry
            print("\nSelection cancelled.")
            response = prompt_yes_no("Would you like to enter keywords manually?", default=False)
            if response:
                return self._enter_keywords_manually()
            else:
                return None

        # Extract label from (label, data) tuple returned by selector
        if isinstance(selection_result, tuple):
            selected_display = selection_result[0]
        else:
            selected_display = selection_result

        # Extract keyword from display text, removing emoji prefix and message count
        # Handles both "📌 keyword" and "📊 keyword (count messages)" formats
        keyword = selected_display
        if keyword.startswith("📌 "):
            keyword = keyword[2:].strip()
        elif keyword.startswith("📊 "):
            keyword = keyword[2:].strip()
            # Remove "(count messages)" suffix if present
            if " (" in keyword:
                keyword = keyword.split(" (")[0]

        # For now, return single keyword
        # TODO: Support multiple selection if FilterableListSelector is enhanced
        selected_keywords = [keyword]

        # ISSUE #3 FIX: Show confirmation/editing menu for selected keywords
        return self._confirm_keywords(selected_keywords)

    def _enter_keywords_manually(self) -> Optional[List[str]]:
        """Prompt user to enter keywords as comma-separated text.

        Returns:
            List of keywords, or None if cancelled
        """
        print("\nEnter keywords (comma-separated):")
        print("Examples: Important, Work, Personal")
        print("IMAP flags: \\Seen, \\Flagged, \\Answered, \\Draft")
        keywords_input = input("  > ").strip()

        if not keywords_input:
            return None

        # Parse keywords and clean them
        keywords = [kw.strip() for kw in keywords_input.split(",") if kw.strip()]
        return keywords if keywords else None

    def _manage_keywords(self) -> Optional[List[str]]:
        """Offer keyword management options (add, remove, list).

        Returns:
            List of selected keywords, or None if cancelled
        """
        while True:
            print("\nManage predefined keywords:")
            print("  1. View all keywords")
            print("  2. Add new keyword")
            print("  3. Remove keyword")
            print("  4. Return to keyword selection")

            choice = input("  > ").strip()

            if choice == "1":
                self._list_keywords()
            elif choice == "2":
                self._add_keyword_interactive()
            elif choice == "3":
                self._remove_keyword_interactive()
            elif choice == "4":
                # Return to main keyword selection
                return self._get_keywords()
            else:
                print("Invalid choice. Please select 1, 2, 3, or 4.")

    def _list_keywords(self) -> None:
        """Display all predefined keywords."""
        keywords = self.keyword_manager.get_keywords()
        if not keywords:
            print("\nNo predefined keywords found.")
            return

        print("\nPredefined Keywords:")
        for i, kw in enumerate(keywords, 1):
            print(f"  {i}. {kw}")

    def _add_keyword_interactive(self) -> None:
        """Interactively add a new keyword."""
        keyword = input("\nEnter new keyword: ").strip()
        if not keyword:
            print("Keyword cannot be empty.")
            return

        if self.keyword_manager.add_keyword(keyword):
            print(f"✓ Added keyword: {keyword}")
        else:
            print(f"⚠️  Keyword already exists: {keyword}")

    def _remove_keyword_interactive(self) -> None:
        """Interactively remove a keyword."""
        self._list_keywords()
        keyword = input("\nEnter keyword to remove: ").strip()
        if not keyword:
            print("Keyword cannot be empty.")
            return

        if self.keyword_manager.remove_keyword(keyword):
            print(f"✓ Removed keyword: {keyword}")
        else:
            print(f"⚠️  Keyword not found: {keyword}")

    def _confirm_keywords(self, keywords: List[str]) -> Optional[List[str]]:
        """ISSUE #3 FIX: Show confirmation and editing menu for selected keywords.

        Allows user to review, edit, or add more keywords after selection.

        Args:
            keywords: List of selected keywords

        Returns:
            Final list of keywords, or None if cancelled
        """
        while True:
            print(f"\nSelected keywords: {keywords}")
            print("\nOptions:")
            print("  1. Confirm and use these keywords (or just press Enter)")
            print("  2. Add another keyword")
            print("  3. Remove a keyword from the list")
            print("  4. Start over and select different keywords")
            print("  5. Cancel (go back)")

            choice = input("\n  > ").strip()

            if choice == "1" or choice == "":
                # Confirm - return the keywords
                return keywords if keywords else None

            elif choice == "2":
                # Add another keyword
                print("\nAdd another keyword:")
                print("  1. Select from list")
                print("  2. Enter manually")

                sub_choice = input("  > ").strip()

                if sub_choice == "1":
                    # Recursive call to select from list
                    additional = self._get_keywords()
                    if additional:
                        # Combine with existing, removing duplicates
                        keywords = list(dict.fromkeys(keywords + additional))
                        print(f"✓ Keywords updated: {keywords}")
                    continue

                elif sub_choice == "2":
                    # Enter manually
                    new_kw = input("Enter keyword: ").strip()
                    if new_kw and new_kw not in keywords:
                        keywords.append(new_kw)
                        print(f"✓ Added: {new_kw}")
                    elif new_kw in keywords:
                        print(f"⚠️  Already in list: {new_kw}")
                    continue

            elif choice == "3":
                # Remove a keyword
                if len(keywords) == 1:
                    print("⚠️  Cannot remove - this is your only keyword.")
                    continue

                print("\nRemove which keyword?")
                for i, kw in enumerate(keywords, 1):
                    print(f"  {i}. {kw}")

                try:
                    idx = int(input("  > ").strip()) - 1
                    if 0 <= idx < len(keywords):
                        removed = keywords.pop(idx)
                        print(f"✓ Removed: {removed}")
                    else:
                        print("Invalid choice.")
                except ValueError:
                    print("Invalid input.")
                continue

            elif choice == "4":
                # Start over
                print("\nStarting keyword selection again...")
                return self._get_keywords()

            elif choice == "5":
                # Cancel
                return None

            else:
                print("Invalid choice. Please select 1-5.")

    def _configure_metadata(self) -> bool:
        """Set rule name and priority.

        Returns:
            True if configured, False if cancelled
        """
        # Auto-suggest name based on actions
        suggested_name = "New Rule"
        if self.rule_builder.actions:
            first_action = self.rule_builder.actions[0]
            action_type = first_action.get("type", "")

            if action_type == "move":
                target = first_action.get("target", "")
                suggested_name = target.replace("/", " » ") if target else "Move"
            elif action_type in ("set_keywords", "remove_keywords"):
                keywords = first_action.get("keywords", [])
                action_desc = action_type.replace("_", " ").title()
                if keywords:
                    suggested_name = f"{action_desc} » {', '.join(keywords)}"
                else:
                    suggested_name = action_desc

        print(f"\nRule name [default: {suggested_name}]:")
        name = input("  > ").strip()
        if not name:
            name = suggested_name

        self.rule_builder.set_name(name)

        # Priority
        print("\nRule priority [default: 100]:")
        print("  (Higher priority rules are evaluated first)")
        priority_str = input("  > ").strip()

        if priority_str:
            try:
                priority = int(priority_str)
                self.rule_builder.set_priority(priority)
            except ValueError:
                print("Invalid priority, using default 100.")

        # Add wizard comment
        self.rule_builder.add_comment("Created with IMAPFilter Rule Wizard")

        return True

    def _preview_and_save(self) -> int:
        """Preview rule, run dry-run, and save if confirmed.

        Returns:
            0 if saved successfully
            130 if cancelled
            1 to loop back and edit
        """
        # Validate rule
        valid, error = self.rule_builder.validate()
        if not valid:
            print(f"\n⚠️  Validation Error: {error}")
            # Offer to fix specific errors
            if "must have either 'contains' or 'regex'" in error:
                import re
                match = re.search(r"Condition (\d+)", error)
                if match and self._fix_missing_match_type(int(match.group(1)) - 1):
                    return self._preview_and_save()  # Retry after fixing
            elif "cannot have both 'contains' and 'regex'" in error:
                import re
                match = re.search(r"Condition (\d+)", error)
                if match and self._fix_duplicate_match_type(int(match.group(1)) - 1):
                    return self._preview_and_save()  # Retry after fixing
            elif "empty match value" in error:
                import re
                match = re.search(r"Condition (\d+)", error)
                if match and self._fix_empty_value(int(match.group(1)) - 1):
                    return self._preview_and_save()  # Retry after fixing
            elif "Action" in error and ("requires" in error or "missing" in error):
                if self._fix_action_error(error):
                    return self._preview_and_save()  # Retry after fixing
            print("\nPlease go back and edit the rule.")
            return 1

        # Generate rule
        try:
            rule = self.rule_builder.generate_rule()
        except ValueError as e:
            print(f"\n⚠️  Error generating rule: {e}")
            print("\nPlease go back and edit the rule.")
            return 1

        # Display rule JSON
        print("\n" + "=" * 60)
        print("Generated Rule:")
        print("=" * 60)
        print(json.dumps(rule, indent=2))
        print("=" * 60)

        # Run dry-run preview
        print("\nRunning dry-run preview...")
        total_matches, folder_matches, stats = self._preview_rule(rule)

        # Display detailed preview summary
        self._display_preview_summary(total_matches, folder_matches, stats)

        # NEW: Offer to view sample of matching emails before saving
        if total_matches > 0 and prompt_yes_no("View sample of matching emails?", default=False):
            self._view_matching_cache(limit=50)

        # Ask to save
        print("\n" + "=" * 60)
        print("Options:")
        print("  1. Save rule and exit (default - press Enter)")
        print("  2. Save rule and edit in rule_manager")
        print("  3. Cancel (discard rule)")
        print("  4. Edit (start over)")
        print("  5. Save rule and create another")

        choice = input("  > ").strip()

        # Default to option 1 (save and exit) if user presses Enter
        if not choice:
            choice = "1"

        if choice == "1":
            # Save rule and exit
            success, message = save_rule(rule, self.config.paths.rules_dir)
            if success:
                # ISSUE #2 FIX: Standardized success message format
                print(f"\n✓ Rule: {message}")
                return 0
            else:
                print(f"\nError saving rule: {message}")
                return 1

        elif choice == "2":
            # Save rule and edit in rule_manager
            success, message = save_rule(rule, self.config.paths.rules_dir)
            if success:
                # ISSUE #2 FIX: Standardized success message format
                print(f"\n✓ Rule: {message}")
                # Extract filename from message like "Saved to /path/to/file.json"
                try:
                    from pathlib import Path
                    file_path = Path(message.split("Saved to ")[-1])
                    print(f"\nLaunching rule manager to edit: {file_path.name}")
                    print("=" * 60)
                    # Try to import and launch rule_manager
                    try:
                        from rule_manager import RuleManager
                        manager = RuleManager()
                        # Refresh to load the new rule
                        manager.refresh_rules()
                        # Find and edit the rule we just created
                        for record in manager.rules:
                            if record.file == file_path:
                                manager.edit_rule(record)
                                break
                        print("=" * 60)
                        print("Returning to wizard...")
                    except Exception as e:
                        print(f"\n⚠️  Could not launch rule manager: {e}")
                        print("You can edit the rule manually using 'python3 rule_manager.py'")
                    return 0
                except Exception as e:
                    print(f"\nError: {e}")
                    return 1
            else:
                print(f"\nError saving rule: {message}")
                return 1

        elif choice == "3":
            print("\nRule discarded.")
            return 130

        elif choice == "4":
            print("\nStarting over...")
            # Reset builder
            self.rule_builder = RuleBuilder()
            return 1

        else:
            # Handle option 5 or invalid choice: save and create new rule
            success, message = save_rule(rule, self.config.paths.rules_dir)
            if success:
                # ISSUE #2 FIX: Standardized success message format
                print(f"\n✓ Rule: {message}")
                print("\nStarting new rule...\n")
                # Reset builder for new rule
                self.rule_builder = RuleBuilder()
                return 1  # Loop back to create another rule
            else:
                print(f"\nError saving rule: {message}")
                return 1

    def _preview_rule(self, rule: dict) -> tuple[int, dict[str, int], dict]:
        """Run dry-run preview and collect detailed match statistics.

        Args:
            rule: The rule dictionary to preview

        Returns:
            Tuple of (total_matches, folder_matches, stats_dict)
            where:
            - total_matches: Total number of matching messages
            - folder_matches: Dict mapping folder names to match counts
            - stats_dict: Dict with 'duration', 'rate', 'rule_name' for display
        """
        from core.rule_engine import rule_match
        from core.logging_utils import PhaseTimer

        timer = PhaseTimer("preview")

        # Get all cached headers with folder information
        cursor = self.cache_engine.conn.cursor()
        total_count = self.cache_engine._get_total_count()
        cursor.execute("SELECT folder, data FROM headers ORDER BY folder")

        progress_bar = tqdm(
            cursor,
            total=total_count,
            desc="🎯 Previewing rule matches",
            unit="msg",
            dynamic_ncols=True,
            disable=not self.show_progress,
        )

        match_count = 0
        folder_match_counts: dict[str, int] = {}
        conditions = rule.get("conditions", {})
        rule_name = rule.get("name", "Preview Rule")

        for row in progress_bar:
            if not row or len(row) < 2:
                continue

            folder, data = row[0], row[1]
            if not data:
                continue

            header = safe_parse_header(data)

            # Evaluate conditions
            matched = False
            if "all" in conditions:
                conds = conditions["all"]
                matched = all(rule_match(header, cond) for cond in conds)
            elif "any" in conditions:
                conds = conditions["any"]
                matched = any(rule_match(header, cond) for cond in conds)

            if matched:
                match_count += 1
                folder_match_counts[folder] = folder_match_counts.get(folder, 0) + 1

        # Prepare stats for display
        stats = {
            "duration": timer.fmt(),
            "rate": f"{timer.rate():.1f}" if timer.elapsed > 0 else "0.0",
            "rule_name": rule_name,
        }

        return match_count, folder_match_counts, stats

    def _display_preview_summary(
        self,
        total_matches: int,
        folder_matches: dict[str, int],
        stats: dict
    ) -> None:
        """Display a detailed preview summary matching the rule manager style.

        Args:
            total_matches: Total number of matching messages
            folder_matches: Dict mapping folder names to match counts
            stats: Dict with 'duration', 'rate', 'rule_name' for display
        """
        print("\n" + "=" * 60)
        print("📊 Summary — Preview Rule")
        print("=" * 60)
        print(f"   🧩  Rules evaluated: 1")
        print(f"   🎯  Matches found: {total_matches}")
        print(f"   ⏱️  Duration: {stats.get('duration', 'N/A')} ({stats.get('rate', 'N/A')} msg/s)")

        # Show matches by folder
        if folder_matches:
            sorted_folders = sorted(folder_matches.items(), key=lambda kv: (-kv[1], kv[0]))
            print(f"   📂  Matches by folder:")
            for folder, count in sorted_folders:
                print(f"      • {folder}: {count}")

        # Show matches by rule
        rule_name = stats.get('rule_name', 'Preview Rule')
        print(f"   🧠  Matches by rule:")
        print(f"      • {rule_name}: {total_matches}")
        print("=" * 60 + "\n")

    def _view_matching_cache(self, limit: int = 1000) -> None:
        """Launch interactive cache viewer showing emails matching current conditions.

        This displays an interactive ASCII table of emails from the cache that match
        the current rule conditions being built.

        Args:
            limit: Maximum number of emails to display (default: 1000)
        """
        try:
            from core.tools.cache_viewer import CacheTableViewer, extract_emails_from_cache
            import curses

            print("\nLoading matching emails from cache...")

            # Extract conditions from current rule builder
            # Note: rule_builder.conditions is a dict like {"all": [...]} or {"any": [...]}
            # Pass the full conditions dict to preserve the all/any logic
            conditions = self.rule_builder.conditions if self.rule_builder.conditions else None

            # Extract emails from cache
            emails = extract_emails_from_cache(
                self.config.paths.db_file,
                conditions=conditions,
                limit=limit,
                show_progress=False,
            )

            if not emails:
                print("No matching emails found in cache.")
                return

            print(f"Found {format_count(len(emails))} matching emails.")
            print("Press Enter to open interactive viewer (press 'q' to close)...")
            input()

            # Launch interactive viewer
            viewer = CacheTableViewer(emails, title=f"Matching Emails ({format_count(len(emails))})")
            curses.wrapper(viewer.run)

            print("\n✓ Cache viewer closed.")

        except ImportError as e:
            print(f"⚠️  Could not load cache viewer: {e}")
        except FileNotFoundError:
            print("⚠️  Cache database not found. Please build cache first.")
        except Exception as e:
            print(f"⚠️  Error viewing cache: {e}")

    def _prompt_text(self, prompt: str, default: str = "") -> str:
        """Prompt for text input.

        Args:
            prompt: The prompt message
            default: Default value if user presses Enter

        Returns:
            User input or default value
        """
        if default:
            print(f"{prompt} [default: {default}]")
        else:
            print(prompt)

        response = input("  > ").strip()
        return response if response else default
