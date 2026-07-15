"""Interactive curses widgets: expandable items and the filterable list selector."""
from __future__ import annotations

import curses
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple, Union


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
