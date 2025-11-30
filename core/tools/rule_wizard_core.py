#!/usr/bin/env python3
"""Core components for the IMAPFilter rule creation wizard.

This module provides interactive widgets for building rules, including
a filterable list selector with real-time search capabilities, and
a cache query engine for extracting header statistics.
"""
from __future__ import annotations

import curses
import email
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Tuple


def format_count(count: int) -> str:
    """Format a count with thousand separators for readability.

    Args:
        count: The number to format

    Returns:
        Formatted string like "1,234"
    """
    return f"{count:,}"


class FilterableListSelector:
    """Interactive curses widget for selecting items from a list with real-time filtering.

    This widget displays a searchable, scrollable list where users can:
    - Type to filter items in real-time (case-insensitive substring matching)
    - Navigate with arrow keys
    - Select an item with Enter
    - Cancel with ESC

    Example:
        items = [("INBOX", 1234), ("Sent", 567), ("Drafts", 89)]
        selector = FilterableListSelector(items, "Select Folder")
        result = curses.wrapper(selector.run)
        if result:
            print(f"Selected: {result}")
    """

    def __init__(self, items: List[Tuple[str, int]], title: str):
        """Initialize the filterable list selector.

        Args:
            items: List of (label, count) tuples to display
            title: Display title for the selector
        """
        self.all_items = items
        self.title = title
        self.filter_text = ""
        self.filtered_items: List[Tuple[str, int]] = list(items)
        self.selected_index = 0
        self.scroll_offset = 0

    def _update_filtered_items(self) -> None:
        """Update filtered_items based on current filter_text.

        Performs case-insensitive substring matching on item labels.
        Resets selection to 0 after filtering.
        """
        if not self.filter_text:
            self.filtered_items = list(self.all_items)
        else:
            filter_lower = self.filter_text.lower()
            self.filtered_items = [
                (label, count)
                for label, count in self.all_items
                if filter_lower in label.lower()
            ]

        # Reset selection to top of filtered list
        self.selected_index = 0
        self.scroll_offset = 0

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
        title_text = f"{self.title} ({format_count(len(self.all_items))} items)"
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
        list_height = min(max_list_height, len(self.filtered_items))

        # Adjust scroll offset to keep selection visible
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + list_height:
            self.scroll_offset = self.selected_index - list_height + 1

        # Render visible items
        for offset in range(list_height):
            index = self.scroll_offset + offset
            if index >= len(self.filtered_items):
                break

            label, count = self.filtered_items[index]

            # Format: "  123. Label (1,234)"
            display_num = index + 1
            item_text = f"{display_num:>5}. {label} ({format_count(count)})"

            # Add selection marker
            if index == self.selected_index:
                marker = ">"
                item_text = f"{marker} {item_text}"
                attr = curses.A_REVERSE
            else:
                item_text = f"  {item_text}"
                attr = curses.A_NORMAL

            row = list_start_row + offset
            stdscr.addnstr(row, 0, item_text, width - 1, attr)

        # Footer: Help text
        help_text = "↑/↓ navigate  Enter select  Type to filter  Backspace delete  ESC cancel"
        stdscr.addnstr(height - 1, 0, help_text, width - 1, curses.A_DIM)

        stdscr.refresh()

    def _handle_key(self, key: int) -> Optional[str]:
        """Handle a keypress and return selection if complete.

        Args:
            key: The curses key code

        Returns:
            Selected item label if Enter was pressed, None if still navigating,
            empty string "" if cancelled
        """
        # Navigation keys
        if key in (curses.KEY_UP, ord("k")):
            if self.selected_index > 0:
                self.selected_index -= 1

        elif key in (curses.KEY_DOWN, ord("j")):
            if self.selected_index < len(self.filtered_items) - 1:
                self.selected_index += 1

        elif key == curses.KEY_HOME:
            self.selected_index = 0

        elif key == curses.KEY_END:
            self.selected_index = max(0, len(self.filtered_items) - 1)

        elif key == curses.KEY_PPAGE:  # Page Up
            # Move up by ~10 items
            self.selected_index = max(0, self.selected_index - 10)

        elif key == curses.KEY_NPAGE:  # Page Down
            # Move down by ~10 items
            self.selected_index = min(
                len(self.filtered_items) - 1,
                self.selected_index + 10
            )

        # Selection/Cancel keys
        elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            # Return selected item
            if self.filtered_items and 0 <= self.selected_index < len(self.filtered_items):
                return self.filtered_items[self.selected_index][0]
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

    def run(self, stdscr: Any) -> Optional[str]:
        """Show the UI and return the selected item or None if cancelled.

        This is the main entry point that should be called via curses.wrapper():
            result = curses.wrapper(selector.run)

        Args:
            stdscr: The curses screen object (provided by curses.wrapper)

        Returns:
            The selected item label, or None if cancelled (ESC pressed)
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
                # Otherwise return the selected label
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

    def __init__(self, db_path: Path):
        """
        Initialize the query engine and connect to the cache database.

        Args:
            db_path: Path to the SQLite cache database file

        Raises:
            sqlite3.Error: If database connection fails
        """
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

    def extract_unique_from_addresses(self, limit: int = 1000) -> List[Tuple[str, int]]:
        """
        Extract unique From addresses with message counts.

        Args:
            limit: Maximum number of results to return (default: 1000)

        Returns:
            List of (address, count) tuples, sorted by count (descending)
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT data FROM headers")

        counter = Counter()
        for row in cursor:
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
        cursor.execute("SELECT data FROM headers")

        counter = Counter()
        for row in cursor:
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
        cursor.execute("SELECT data FROM headers")

        counter = Counter()
        for row in cursor:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            subject = header.get("subject", "").strip()
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
        cursor.execute("SELECT data FROM headers")

        header_name = name.lower()
        counter = Counter()
        for row in cursor:
            data = row[0] if row else ""
            header = safe_parse_header(data)
            value = header.get(header_name, "").strip()
            if value:
                counter[value] += 1

        return counter.most_common(limit)

    def count_from_contains(self, pattern: str) -> int:
        """
        Count messages where From address contains the given pattern (case-insensitive).

        Args:
            pattern: String pattern to search for in From addresses

        Returns:
            Number of messages with matching From addresses
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT data FROM headers")

        pattern_lower = pattern.lower()
        count = 0
        for row in cursor:
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
        cursor.execute("SELECT data FROM headers")

        pattern_lower = pattern.lower()
        count = 0
        for row in cursor:
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
        self, email_addr: str, cache_engine: CacheQueryEngine
    ) -> List[Tuple[str, str, int]]:
        """Suggest email patterns based on the given email address.

        Args:
            email_addr: Email address to extract patterns from (e.g., "noreply@amazon.com")
            cache_engine: Cache query engine for getting match counts

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

        patterns: List[Tuple[str, str, int]] = []
        email_lower = email_addr.lower().strip()

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
        self, subject: str, cache_engine: CacheQueryEngine
    ) -> List[Tuple[str, str, int]]:
        """Suggest subject patterns based on the given subject line.

        Args:
            subject: Subject line to extract patterns from
            cache_engine: Cache query engine for getting match counts

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

        patterns: List[Tuple[str, str, int]] = []
        subject_clean = subject.strip()

        if not subject_clean:
            return []

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
        self.conditions: List[dict] = []
        self.logic: str = "any"  # Default to "any" for multiple conditions
        self.action_type: str = ""
        self.action_target: str = ""
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
        self.conditions.append(condition)
        return self

    def set_logic(self, logic: str) -> "RuleBuilder":
        """Set the logic operator for multiple conditions.

        Args:
            logic: Either "all" (AND) or "any" (OR) for combining conditions

        Returns:
            Self for method chaining
        """
        if logic.lower() in ("all", "any"):
            self.logic = logic.lower()
        return self

    def set_action(self, action_type: str, target: str) -> "RuleBuilder":
        """Set the action to take when rule matches.

        Args:
            action_type: Type of action (currently only "move" is supported)
            target: Target folder path (e.g., "Banking/NatWest")

        Returns:
            Self for method chaining
        """
        self.action_type = action_type.lower()
        self.action_target = target
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

        if not self.action_type:
            return False, "Action type is required"

        if self.action_type not in ("move",):
            return False, f"Unsupported action type: {self.action_type}"

        if not self.action_target:
            return False, "Action target is required"

        # Validate each condition
        for i, condition in enumerate(self.conditions):
            if "header" not in condition:
                return False, f"Condition {i+1} missing header field"

            if "contains" not in condition and "regex" not in condition:
                return False, f"Condition {i+1} must have either 'contains' or 'regex'"

            if "contains" in condition and "regex" in condition:
                return False, f"Condition {i+1} cannot have both 'contains' and 'regex'"

            # Get the match value
            value = condition.get("contains") or condition.get("regex")
            if not value or not str(value).strip():
                return False, f"Condition {i+1} has empty match value"

        # Validate logic for multiple conditions
        if len(self.conditions) > 1 and self.logic not in ("all", "any"):
            return False, "Logic must be 'all' or 'any' for multiple conditions"

        return True, ""

    def generate_rule(self) -> dict:
        """Generate the complete rule dictionary.

        Returns:
            Dictionary in IMAPFilter rule format

        Raises:
            ValueError: If rule validation fails
        """
        valid, error = self.validate()
        if not valid:
            raise ValueError(f"Invalid rule configuration: {error}")

        # Build rule with correct key order: name, priority, conditions, action, comments
        rule = {
            "name": self.name,
            "priority": self.priority,
        }

        # Build conditions block (same logic for single or multiple conditions)
        rule["conditions"] = {self.logic: self.conditions}

        # Add action
        rule["action"] = {"type": self.action_type, "target": self.action_target}

        # Add comments if any
        if self.comments:
            rule["comments"] = self.comments

        return rule


def slugify(name: str) -> str:
    """Convert a rule name to a filesystem-safe slug.

    This function creates URL/filename-safe versions of rule names by:
    - Converting to lowercase
    - Keeping alphanumeric characters
    - Replacing spaces, hyphens, dots, slashes, and underscores with underscores
    - Removing consecutive underscores
    - Stripping leading/trailing underscores

    Args:
        name: The rule name to slugify (e.g., "Banking » NatWest")

    Returns:
        Slugified string (e.g., "banking_natwest")
        Returns "rule" if the result would be empty

    Examples:
        >>> slugify("Banking » NatWest")
        'banking_natwest'
        >>> slugify("Newsletters/Reddit")
        'newsletters_reddit'
        >>> slugify("Events » SoulCycle [Cancelled]")
        'events_soulcycle_cancelled'
    """
    safe: List[str] = []
    for ch in name.lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {" ", "-", ".", "/", "_", "»", "[", "]", "(", ")", ",", ":"}:
            safe.append("_")

    slug = "".join(safe).strip("_")

    # Collapse multiple consecutive underscores
    while "__" in slug:
        slug = slug.replace("__", "_")

    return slug or "rule"


def generate_filename(name: str, rules_dir: Path) -> Path:
    """Generate the next available filename for a new rule.

    This function generates filenames in the format: {5-digit-id}_{slug}.json
    The ID is determined by finding the maximum numeric prefix in existing
    rule files and incrementing it by 1. If no rules exist, it generates
    an ID from the current timestamp.

    Args:
        name: The rule name to generate a filename for
        rules_dir: Path to the rules directory

    Returns:
        Full path to the new rule file

    Examples:
        >>> # If max existing ID is 99012
        >>> generate_filename("Banking » NatWest", Path("/rules"))
        Path('/rules/99013_banking_natwest.json')
    """
    from datetime import datetime

    slug = slugify(name)
    numeric_prefixes: List[int] = []

    # Scan existing rule files for numeric prefixes
    if rules_dir.exists():
        for rule_file in rules_dir.glob("*.json"):
            stem = rule_file.stem
            prefix = ""
            for ch in stem:
                if ch.isdigit():
                    prefix += ch
                else:
                    break
            if prefix:
                try:
                    numeric_prefixes.append(int(prefix))
                except ValueError:
                    continue

    # Generate next ID (max + 1, or timestamp-based if no existing rules)
    if numeric_prefixes:
        next_id = max(numeric_prefixes) + 1
    else:
        # Generate ID from current timestamp: YYJJJHHMM (year, julian day, hour, minute)
        next_id = int(datetime.now().strftime("%y%j%H%M"))

    filename = f"{next_id:05d}_{slug}.json"
    return rules_dir / filename


def save_rule(rule: dict, rules_dir: Path) -> Tuple[bool, str]:
    """Save a rule to a JSON file in the rules directory.

    This function validates the rule, generates an appropriate filename,
    creates the rules directory if needed, and writes the rule as
    pretty-printed JSON.

    Args:
        rule: The rule dictionary to save
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
    # Basic validation
    required_fields = ["name", "priority", "conditions", "action"]
    for field in required_fields:
        if field not in rule:
            return False, f"Rule missing required field: {field}"

    # Validate conditions structure
    conditions = rule.get("conditions", {})
    if not isinstance(conditions, dict):
        return False, "Conditions must be a dictionary"

    if "all" not in conditions and "any" not in conditions:
        return False, "Conditions must have either 'all' or 'any' key"

    # Validate action structure
    action = rule.get("action", {})
    if not isinstance(action, dict):
        return False, "Action must be a dictionary"

    if "type" not in action or "target" not in action:
        return False, "Action must have 'type' and 'target' fields"

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

    def __init__(self, config):
        """Initialize the rule wizard.

        Args:
            config: AppConfig object containing paths and settings

        Raises:
            ValueError: If cache database doesn't exist
        """
        from core.config import AppConfig

        if not isinstance(config, AppConfig):
            raise TypeError("config must be an AppConfig instance")

        self.config = config
        self.cache_engine: Optional[CacheQueryEngine] = None
        self.email_extractor = EmailPatternExtractor()
        self.subject_extractor = SubjectPatternExtractor()
        self.rule_builder = RuleBuilder()

        # Validate cache exists
        if not self.config.paths.db_file.exists():
            raise ValueError(
                f"Cache database not found at {self.config.paths.db_file}. "
                "Please run 'build-cache' first."
            )

    def run(self) -> int:
        """Run the complete rule wizard workflow.

        Returns:
            Exit code: 0 on success, 1 on error, 130 on user cancellation
        """
        try:
            # Initialize cache engine
            self.cache_engine = CacheQueryEngine(self.config.paths.db_file)

            # Display welcome and validate cache
            if not self._validate_cache():
                return 1

            self._display_welcome()

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

                # Step 2: Configure logic (if multiple conditions)
                if len(self.rule_builder.conditions) > 1:
                    if not self._configure_logic():
                        continue  # Restart if cancelled

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

        except KeyboardInterrupt:
            print("\n\nWizard interrupted.")
            return 130
        except Exception as e:
            print(f"\nError: {e}")
            return 1
        finally:
            if self.cache_engine:
                self.cache_engine.close()

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
                response = self._prompt_yes_no(
                    f"\nYou have {len(self.rule_builder.conditions)} condition(s). "
                    "Add another condition?"
                )
                if response is None:  # Cancelled
                    return None
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

    def _add_single_condition(self) -> bool:
        """Add a single condition to the rule.

        Returns:
            True if condition was added, False if cancelled
        """
        # Step 1: Select header field
        field = self._select_header_field()
        if not field:
            return False

        # Step 2: Select field value
        value = self._select_field_value(field)
        if not value:
            return False

        # Step 3: Suggest patterns and let user choose
        pattern = self._suggest_patterns(field, value)
        if pattern is None:
            return False

        # Step 4: Select match type
        match_type = self._select_match_type()
        if not match_type:
            return False

        # Add condition to builder
        self.rule_builder.add_condition(field, match_type, pattern)
        print(f"\nCondition added: {field} {match_type} '{pattern}'")

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
            custom = input("Enter header name: ").strip().lower()
            return custom if custom else None
        else:
            print("Invalid choice. Please try again.")
            return self._select_header_field()

    def _select_field_value(self, field: str) -> Optional[str]:
        """Show filterable list and let user pick value.

        Args:
            field: Header field name (from, to, subject, etc.)

        Returns:
            Selected value, or None if cancelled
        """
        # Get values from cache
        if field == "from":
            items = self.cache_engine.extract_unique_from_addresses(limit=2000)
        elif field == "to":
            items = self.cache_engine.extract_unique_to_addresses(limit=2000)
        elif field == "subject":
            items = self.cache_engine.extract_unique_subjects(limit=500)
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
        selected = curses.wrapper(selector.run)

        if selected is None:
            print("Selection cancelled.")
            return None

        return selected

    def _suggest_patterns(self, field: str, value: str) -> Optional[str]:
        """Show pattern suggestions and let user pick one.

        Args:
            field: Header field name
            value: The selected value

        Returns:
            Chosen pattern string, or None if cancelled
        """
        # Generate suggestions based on field type
        if field in ("from", "to", "reply-to"):
            patterns = self.email_extractor.suggest_patterns(value, self.cache_engine)
        elif field == "subject":
            patterns = self.subject_extractor.suggest_patterns(value, self.cache_engine)
        else:
            # For other fields, just use the value as-is
            patterns = [(value, "Exact match", 1)]

        if not patterns:
            return value

        print(f"\n{field.title()}: {value}")
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
                return manual if manual else value
            else:
                print("Invalid choice, using original value.")
                return value
        except ValueError:
            print("Invalid input, using original value.")
            return value

    def _select_match_type(self) -> Optional[str]:
        """Prompt for match type (contains or regex).

        Returns:
            'contains' or 'regex', or None if cancelled
        """
        print("\nMatch type:")
        print("  1. Contains (substring match - case insensitive)")
        print("  2. Regex (regular expression)")

        choice = input("  > ").strip()

        if choice == "1":
            return "contains"
        elif choice == "2":
            return "regex"
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

    def _configure_action(self) -> bool:
        """Configure the action (currently only 'move' is supported).

        Returns:
            True if configured, False if cancelled
        """
        print("\nAction type:")
        print("  1. Move (move messages to a folder)")

        choice = input("  > ").strip()

        if choice != "1":
            print("Only 'move' action is currently supported.")

        target = input("\nTarget folder path (e.g., 'Banking/NatWest'): ").strip()
        if not target:
            print("Target folder is required.")
            return False

        self.rule_builder.set_action("move", target)
        print(f"Action set: move to '{target}'")

        return True

    def _configure_metadata(self) -> bool:
        """Set rule name and priority.

        Returns:
            True if configured, False if cancelled
        """
        # Auto-suggest name based on target folder
        target = self.rule_builder.action_target
        suggested_name = target.replace("/", " » ")

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
            print(f"\nError: {error}")
            return 1

        # Generate rule
        try:
            rule = self.rule_builder.generate_rule()
        except ValueError as e:
            print(f"\nError generating rule: {e}")
            return 1

        # Display rule JSON
        print("\n" + "=" * 60)
        print("Generated Rule:")
        print("=" * 60)
        print(json.dumps(rule, indent=2))
        print("=" * 60)

        # Run dry-run preview
        print("\nRunning dry-run preview...")
        match_count = self._preview_rule(rule)
        print(f"\nThis rule will match approximately {format_count(match_count)} messages.")

        # Ask to save
        print("\n" + "=" * 60)
        print("Options:")
        print("  1. Save rule")
        print("  2. Cancel (discard rule)")
        print("  3. Edit (start over)")

        choice = input("  > ").strip()

        if choice == "1":
            # Save rule
            success, message = save_rule(rule, self.config.paths.rules_dir)
            if success:
                print(f"\nSuccess! {message}")
                return 0
            else:
                print(f"\nError saving rule: {message}")
                return 1
        elif choice == "2":
            print("\nRule discarded.")
            return 130
        else:
            print("\nStarting over...")
            # Reset builder
            self.rule_builder = RuleBuilder()
            return 1

    def _preview_rule(self, rule: dict) -> int:
        """Run dry-run preview and count matching messages.

        Args:
            rule: The rule dictionary to preview

        Returns:
            Number of matching messages
        """
        from core.rule_engine import rule_match

        # Get all cached headers
        cursor = self.cache_engine.conn.cursor()
        cursor.execute("SELECT data FROM headers")

        match_count = 0
        conditions = rule.get("conditions", {})

        for row in cursor:
            data = row[0] if row else ""
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

        return match_count

    def _prompt_yes_no(self, prompt: str) -> Optional[bool]:
        """Simple yes/no prompt.

        Args:
            prompt: The question to ask

        Returns:
            True for yes, False for no, None for cancelled
        """
        print(f"\n{prompt}")
        response = input("  (yes/no) > ").strip().lower()

        if response in ("y", "yes"):
            return True
        elif response in ("n", "no"):
            return False
        elif response == "":
            return False  # Default to no
        else:
            print("Please enter 'yes' or 'no'.")
            return self._prompt_yes_no(prompt)

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
