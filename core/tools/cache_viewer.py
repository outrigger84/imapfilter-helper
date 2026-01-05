"""Interactive cache viewer for displaying email metadata in an ASCII table.

This module provides a curses-based interactive table viewer for browsing email
cache contents with sorting, scrolling, and optional filtering by rule conditions.

Example:
    >>> from core.tools.cache_viewer import extract_emails_from_cache, CacheTableViewer
    >>> import curses
    >>> emails = extract_emails_from_cache(config.paths.db_file, limit=100)
    >>> viewer = CacheTableViewer(emails, title="Email Cache")
    >>> curses.wrapper(viewer.run)
"""

from __future__ import annotations

import curses
import email
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from typing import Any, List, Optional, Tuple

from core.ui_components import format_count


def _decode_mime_header(header_value: str) -> str:
    """Decode MIME-encoded header values (RFC 2047).

    Converts encoded text like =?utf-8?B?...?= to readable text.

    Args:
        header_value: Raw header value that may be MIME-encoded

    Returns:
        Decoded header value as a string
    """
    if not header_value:
        return header_value

    try:
        # decode_header returns list of (decoded_bytes, charset) tuples
        decoded_parts = []
        for part, charset in decode_header(header_value):
            if isinstance(part, bytes):
                # Decode bytes using the specified charset or fallback to utf-8
                try:
                    decoded = part.decode(charset or 'utf-8', errors='replace')
                except (TypeError, LookupError):
                    decoded = part.decode('utf-8', errors='replace')
            else:
                decoded = part
            decoded_parts.append(decoded)

        return ''.join(decoded_parts)
    except Exception:
        # If decoding fails, return original value
        return header_value


@dataclass
class EmailRow:
    """Represents a single email for display in the cache viewer."""

    folder: str
    uid: str
    from_addr: str
    to_addr: str
    subject: str
    date: str  # Formatted as "YYYY-MM-DD HH:MM"
    raw_data: str  # Keep for potential filtering


@dataclass
class SortState:
    """Manages current sort column and direction."""

    column: int = 3  # 0=From, 1=To, 2=Subject, 3=Date
    ascending: bool = False  # Default: sort by date descending (newest first)


def safe_parse_header(data: str) -> dict[str, str]:
    """Parse header data from cache into a lowercase-keyed dictionary.

    Handles both raw headers and JSON-wrapped headers gracefully.
    Returns empty dict on any parsing errors.

    Args:
        data: Raw data from cache (JSON string or raw header)

    Returns:
        Dictionary with lowercase header keys and their values
    """
    if not data:
        return {}

    # Try to extract from JSON wrapper
    try:
        import json

        payload = json.loads(data)
        if isinstance(payload, dict):
            raw_header = payload.get("header", "")
            data = raw_header
    except (json.JSONDecodeError, ValueError):
        pass  # Already raw header or invalid JSON

    # Parse email headers
    try:
        message = email.message_from_string(data)
        return {key.lower(): value for key, value in message.items()}
    except Exception:
        return {}


def _parse_internaldate(date_str: Optional[str]) -> Optional[datetime]:
    """Parse IMAP INTERNALDATE format into a datetime object.

    Handles formats like:
    - "28-Oct-2025 07:30:19 +0000"
    - "28-Oct-2025 07:30:19" (without timezone)

    Args:
        date_str: IMAP INTERNALDATE string

    Returns:
        datetime object (timezone-aware if timezone present) or None if parsing fails
    """
    if not date_str:
        return None

    formats_to_try = [
        "%d-%b-%Y %H:%M:%S %z",  # With timezone
        "%d-%b-%Y %H:%M:%S",  # Without timezone
    ]

    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            continue

    return None


def extract_emails_from_cache(
    db_path: Path,
    conditions: Optional[List[dict]] = None,
    limit: int = 1000,
    show_progress: bool = True,
) -> List[EmailRow]:
    """Extract emails from cache database, optionally filtered by rule conditions.

    Args:
        db_path: Path to cache.db
        conditions: Optional list of rule conditions to filter by
        limit: Maximum number of emails to load
        show_progress: Whether to show progress messages

    Returns:
        List of EmailRow objects

    Raises:
        sqlite3.Error: If cache database cannot be accessed
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Cache database not found at {db_path}")

    if show_progress:
        print(f"Loading up to {format_count(limit)} emails from cache...")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    emails: List[EmailRow] = []
    skipped_count = 0

    try:
        # Query all headers with limit
        cursor.execute("SELECT folder, uid, data FROM headers LIMIT ?", (limit * 2,))

        for row in cursor:
            if len(emails) >= limit:
                break

            try:
                folder = row["folder"]
                uid = row["uid"]
                data = row["data"]

                # Parse header data
                header_dict = safe_parse_header(data)

                # Extract fields and decode MIME-encoded headers
                from_addr = _decode_mime_header(header_dict.get("from", "")).strip()
                to_addr = _decode_mime_header(header_dict.get("to", "")).strip()
                subject = _decode_mime_header(header_dict.get("subject", "")).strip()

                # Parse internaldate
                import json

                try:
                    payload = json.loads(data)
                    if isinstance(payload, dict):
                        internaldate_str = payload.get("internaldate", "")
                    else:
                        internaldate_str = ""
                except (json.JSONDecodeError, ValueError):
                    internaldate_str = ""

                date_obj = _parse_internaldate(internaldate_str)
                date_str = (
                    date_obj.strftime("%Y-%m-%d %H:%M") if date_obj else "Unknown"
                )

                # Filter by conditions if provided
                if conditions:
                    if not _matches_conditions(header_dict, conditions):
                        continue

                emails.append(
                    EmailRow(
                        folder=folder,
                        uid=uid,
                        from_addr=from_addr,
                        to_addr=to_addr,
                        subject=subject,
                        date=date_str,
                        raw_data=data,
                    )
                )

            except Exception as e:
                skipped_count += 1
                continue

    finally:
        conn.close()

    if show_progress:
        message = f"Loaded {format_count(len(emails))} emails"
        if skipped_count > 0:
            message += f" ({skipped_count} skipped)"
        print(message)

    return emails


def _convert_wizard_condition_to_rule_engine_format(condition: dict) -> dict:
    """Convert wizard condition format to rule engine format.

    Wizard format: {"field": "from", "match_type": "contains", "value": "pattern"}
    Rule engine format: {"header": "from", "contains": "pattern"}

    Handles both formats and returns rule engine format.

    Args:
        condition: Wizard-format condition or rule engine format condition

    Returns:
        Rule engine-format condition
    """
    # Check if already in rule engine format (has "header" key)
    if "header" in condition:
        # Already in rule engine format, return as-is
        return condition

    # Convert from wizard format
    field = condition.get("field", "")
    match_type = condition.get("match_type", "")
    value = condition.get("value", "")

    # Build rule engine format condition
    rule_condition = {"header": field}

    # Map match_type to rule engine operator keys
    match_type_map = {
        "contains": "contains",
        "not_contains": "not_contains",
        "equals": "equals",
        "not_equals": "not_equals",
        "regex": "regex",
        "not_regex": "not_regex",
    }

    operator = match_type_map.get(match_type, match_type)
    if value:  # Only add operator if value is present
        rule_condition[operator] = value

    return rule_condition


def _matches_conditions(
    header_dict: dict[str, str], conditions: Optional[List[dict] | dict] = None
) -> bool:
    """Check if header matches rule conditions.

    Uses the rule engine to match emails against conditions.
    Handles both wizard-format and rule engine-format conditions.
    Supports both "all" (AND) and "any" (OR) logic.

    Args:
        header_dict: Parsed email headers
        conditions: Rule conditions to match. Can be:
                   - None or empty list: matches everything
                   - List of conditions: uses AND logic (legacy format)
                   - Dict with "all" or "any" key: uses specified logic

    Returns:
        True if header matches conditions according to logic, False otherwise
    """
    if not conditions:
        return True

    try:
        from core.rule_engine import rule_match

        # Handle conditions in dict format with "all"/"any" keys
        if isinstance(conditions, dict):
            if "all" in conditions:
                # AND logic: all conditions must match
                conds = conditions["all"]
                return all(rule_match(header_dict, _convert_wizard_condition_to_rule_engine_format(cond)) for cond in conds)
            elif "any" in conditions:
                # OR logic: any condition can match
                conds = conditions["any"]
                return any(rule_match(header_dict, _convert_wizard_condition_to_rule_engine_format(cond)) for cond in conds)
            else:
                # Empty dict or unrecognized format
                return True

        # Handle conditions as a simple list (legacy format)
        if isinstance(conditions, list):
            # Convert wizard conditions to rule engine format
            converted_conditions = [
                _convert_wizard_condition_to_rule_engine_format(cond)
                for cond in conditions
            ]

            # Use AND logic for list format (matching original behavior)
            return all(rule_match(header_dict, cond) for cond in converted_conditions)

        # Unrecognized format
        return True

    except Exception as e:
        # If rule engine fails, skip filtering
        return True


class CacheTableViewer:
    """Interactive curses-based table viewer for email cache.

    Displays a sortable, scrollable table with the following columns:
    - From: Email address of sender
    - To: Primary recipient
    - Subject: Message subject
    - Date: Message date in YYYY-MM-DD HH:MM format

    Keyboard Controls:
    - 1-4: Sort by column (From, To, Subject, Date)
    - s: Toggle sort direction (ascending/descending)
    - Arrow Up/Down: Scroll one line
    - Page Up/Down: Scroll one page (~10 lines)
    - Home/End: Jump to top/bottom
    - q or ESC: Exit viewer
    """

    def __init__(self, emails: List[EmailRow], title: str = "Email Cache"):
        """Initialize the cache table viewer.

        Args:
            emails: List of EmailRow objects to display
            title: Display title for the viewer
        """
        self.all_emails = emails
        self.title = title
        self.sort_state = SortState(column=3, ascending=False)  # Date descending
        self.selected_index = 0
        self.scroll_offset = 0
        self._sorted_emails = list(emails)
        self._apply_sort()

    def _apply_sort(self) -> None:
        """Sort emails based on current sort state."""
        column_getters = {
            0: lambda r: r.from_addr.lower(),
            1: lambda r: r.to_addr.lower(),
            2: lambda r: r.subject.lower(),
            3: lambda r: r.date,  # Already sortable string format
        }

        self._sorted_emails = sorted(
            self.all_emails,
            key=column_getters[self.sort_state.column],
            reverse=not self.sort_state.ascending,
        )

    def _calculate_column_widths(self, terminal_width: int) -> Tuple[int, int, int, int]:
        """Calculate optimal column widths based on terminal size.

        Args:
            terminal_width: Available terminal width in characters

        Returns:
            Tuple of (from_width, to_width, subject_width, date_width)
        """
        min_width = 80

        if terminal_width < min_width:
            return (15, 15, 15, 12)
        elif terminal_width < 120:
            return (20, 20, 20, 16)
        else:
            # Wider terminals: give Subject more space
            extra = terminal_width - 100
            subject_width = 20 + extra
            return (20, 20, subject_width, 16)

    def _truncate_text(self, text: str, width: int, is_email: bool = False) -> str:
        """Truncate text to fit column width with ellipsis.

        For email addresses, prefers to show just the address part (without display name).

        Args:
            text: Text to truncate
            width: Maximum width in characters
            is_email: If True, extract email address from "Name <email@domain>" format

        Returns:
            Truncated text, left-padded to width
        """
        if is_email and '<' in text and '>' in text:
            # Extract email address from "Name <email@domain>" format
            start = text.rfind('<') + 1
            end = text.rfind('>')
            if start > 0 and end > start:
                email_only = text[start:end]
                if len(email_only) <= width:
                    return email_only.ljust(width)
                text = email_only

        if len(text) <= width:
            return text.ljust(width)
        return (text[: width - 3] + "...").ljust(width)

    def _format_row(
        self,
        email: EmailRow,
        from_width: int,
        to_width: int,
        subject_width: int,
        date_width: int,
    ) -> str:
        """Format a single email row for display.

        Args:
            email: EmailRow to format
            from_width: Width for From column
            to_width: Width for To column
            subject_width: Width for Subject column
            date_width: Width for Date column

        Returns:
            Formatted row string
        """
        # For email addresses, extract just the address part (without display name)
        from_text = self._truncate_text(email.from_addr, from_width, is_email=True)
        to_text = self._truncate_text(email.to_addr, to_width, is_email=True)
        subject_text = self._truncate_text(email.subject, subject_width)
        date_text = email.date.rjust(date_width)  # Right-align dates

        return f"{from_text} {to_text} {subject_text} {date_text}"

    def _get_sort_indicator(self) -> str:
        """Get sort direction indicator for current sort column.

        Returns:
            "↑" for ascending, "↓" for descending
        """
        return "↑" if self.sort_state.ascending else "↓"

    def _get_column_name(self, column: int) -> str:
        """Get display name for column.

        Args:
            column: Column index (0-3)

        Returns:
            Column name or empty string
        """
        names = ["From", "To", "Subject", "Date"]
        return names[column] if 0 <= column < len(names) else ""

    def _render(self, stdscr: Any) -> None:
        """Render the current state of the viewer.

        Args:
            stdscr: The curses screen object
        """
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Calculate column widths
        from_width, to_width, subject_width, date_width = (
            self._calculate_column_widths(width)
        )
        total_header_width = from_width + to_width + subject_width + date_width + 3

        # Layout
        header_height = 3  # Title, divider, column headers
        footer_height = 2  # Status line, help text
        max_list_height = max(1, height - header_height - footer_height)

        current_row = 0

        # Title line
        sort_col_name = self._get_column_name(self.sort_state.column)
        sort_indicator = self._get_sort_indicator()
        title_text = (
            f"{self.title} ({format_count(len(self.all_emails))} messages) "
            f"- Sorted by {sort_col_name} {sort_indicator}"
        )
        stdscr.addnstr(current_row, 0, title_text, width - 1, curses.A_BOLD)
        current_row += 1

        # Divider
        divider = "=" * min(total_header_width, width - 1)
        stdscr.addnstr(current_row, 0, divider, width - 1)
        current_row += 1

        # Column headers
        from_header = "From".ljust(from_width)
        to_header = "To".ljust(to_width)
        subject_header = "Subject".ljust(subject_width)
        date_header = "Date".rjust(date_width)
        header_line = (
            f"{from_header} {to_header} {subject_header} {date_header}"
        )
        stdscr.addnstr(current_row, 0, header_line, width - 1, curses.A_UNDERLINE)
        current_row += 1

        # Calculate visible area
        list_start_row = current_row
        list_height = min(max_list_height, len(self._sorted_emails))

        # Adjust scroll to keep selection visible
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + list_height:
            self.scroll_offset = self.selected_index - list_height + 1

        # Render visible rows
        for offset in range(list_height):
            index = self.scroll_offset + offset
            if index >= len(self._sorted_emails):
                break

            email = self._sorted_emails[index]
            row_text = self._format_row(
                email, from_width, to_width, subject_width, date_width
            )

            row_display = row_text
            attr = curses.A_NORMAL

            # Highlight selected row
            if index == self.selected_index:
                attr = curses.A_REVERSE

            row = list_start_row + offset
            stdscr.addnstr(row, 0, row_display, width - 1, attr)

        # Status line
        current_row = height - 2
        if len(self._sorted_emails) > 0:
            page_num = self.scroll_offset // max(1, list_height) + 1
            total_pages = max(1, (len(self._sorted_emails) - 1) // max(1, list_height) + 1)
            status = (
                f"Page {page_num}/{total_pages} | "
                f"↑/↓ scroll | 1-4 sort column | s toggle | q quit"
            )
        else:
            status = "No emails to display"
        stdscr.addnstr(current_row, 0, status, width - 1, curses.A_DIM)

        # Help text
        current_row = height - 1
        help_text = "Press a key to continue..."
        stdscr.addnstr(current_row, 0, help_text, width - 1, curses.A_DIM)

        stdscr.refresh()

    def _handle_key(self, key: int) -> bool:
        """Handle keyboard input.

        Args:
            key: The curses key code

        Returns:
            False if viewer should exit, True to continue
        """
        if not self._sorted_emails:
            return False  # Exit if no emails

        # Exit keys
        if key == ord("q") or key == 27:  # 'q' or ESC
            return False

        # Navigation keys
        if key == curses.KEY_UP:
            if self.selected_index > 0:
                self.selected_index -= 1

        elif key == curses.KEY_DOWN:
            if self.selected_index < len(self._sorted_emails) - 1:
                self.selected_index += 1

        elif key == curses.KEY_HOME:
            self.selected_index = 0

        elif key == curses.KEY_END:
            self.selected_index = max(0, len(self._sorted_emails) - 1)

        elif key == curses.KEY_PPAGE:  # Page Up
            self.selected_index = max(0, self.selected_index - 10)

        elif key == curses.KEY_NPAGE:  # Page Down
            self.selected_index = min(
                len(self._sorted_emails) - 1, self.selected_index + 10
            )

        # Sort by column: 1, 2, 3, 4
        elif key == ord("1"):
            if self.sort_state.column == 0:
                self.sort_state.ascending = not self.sort_state.ascending
            else:
                self.sort_state.column = 0
                self.sort_state.ascending = False
            self._apply_sort()
            self.selected_index = 0
            self.scroll_offset = 0

        elif key == ord("2"):
            if self.sort_state.column == 1:
                self.sort_state.ascending = not self.sort_state.ascending
            else:
                self.sort_state.column = 1
                self.sort_state.ascending = False
            self._apply_sort()
            self.selected_index = 0
            self.scroll_offset = 0

        elif key == ord("3"):
            if self.sort_state.column == 2:
                self.sort_state.ascending = not self.sort_state.ascending
            else:
                self.sort_state.column = 2
                self.sort_state.ascending = False
            self._apply_sort()
            self.selected_index = 0
            self.scroll_offset = 0

        elif key == ord("4"):
            if self.sort_state.column == 3:
                self.sort_state.ascending = not self.sort_state.ascending
            else:
                self.sort_state.column = 3
                self.sort_state.ascending = False
            self._apply_sort()
            self.selected_index = 0
            self.scroll_offset = 0

        # Toggle sort direction
        elif key == ord("s"):
            self.sort_state.ascending = not self.sort_state.ascending
            self._apply_sort()
            self.selected_index = 0
            self.scroll_offset = 0

        return True  # Continue viewing

    def run(self, stdscr: Any) -> None:
        """Launch the interactive viewer.

        This is the main entry point that should be called via curses.wrapper():

            viewer = CacheTableViewer(emails, title="Email Cache")
            curses.wrapper(viewer.run)

        Args:
            stdscr: The curses screen object (provided by curses.wrapper)
        """
        # Check minimum terminal size
        height, width = stdscr.getmaxyx()
        if height < 10 or width < 60:
            stdscr.clear()
            stdscr.addstr(0, 0, "Terminal too small. Minimum: 10 rows x 60 columns")
            stdscr.refresh()
            stdscr.getch()
            return

        # Initialize curses settings
        curses.curs_set(0)  # Hide cursor
        stdscr.keypad(True)  # Enable keypad mode for special keys

        try:
            curses.use_default_colors()
        except curses.error:
            pass  # Not all terminals support this

        # Main event loop
        while True:
            try:
                self._render(stdscr)
            except curses.error:
                # Handle terminal resize or other curses errors gracefully
                pass

            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                return

            if not self._handle_key(key):
                return  # Exit


def launch_cache_viewer(
    config: Any,
    limit: int = 1000,
    folder: Optional[str] = None,
) -> int:
    """Launch standalone cache viewer from CLI.

    Args:
        config: AppConfig object containing paths
        limit: Maximum emails to load
        folder: Optional folder name to filter by

    Returns:
        0 on success, 1 on error
    """
    import curses

    try:
        print(f"Loading up to {format_count(limit)} emails from cache...")

        emails = extract_emails_from_cache(
            config.paths.db_file, limit=limit, show_progress=True
        )

        if folder:
            emails = [e for e in emails if e.folder == folder]

        if not emails:
            print("No emails found in cache.")
            return 1

        print(f"Loaded {format_count(len(emails))} emails")
        print("\nLaunching cache viewer...")

        viewer = CacheTableViewer(emails, title="Email Cache")
        curses.wrapper(viewer.run)

        print("\nCache viewer closed.")
        return 0

    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1
    except Exception as e:
        print(f"Error launching cache viewer: {e}")
        import traceback

        traceback.print_exc()
        return 1
