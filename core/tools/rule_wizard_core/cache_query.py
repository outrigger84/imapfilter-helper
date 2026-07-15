"""Header cache querying: safe header parsing and the cache query engine."""
from __future__ import annotations

import email
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import List, Tuple

from tqdm import tqdm

from core.tools.coverage_analyzer import _decode_mime_header


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
