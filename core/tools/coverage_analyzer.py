"""Rule coverage analysis for batch mode in the rule wizard."""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from email.header import decode_header
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from core.rule_engine import (
    _extract_raw_header,
    _parse_header_map,
    find_matching_rule,
    load_rules,
)


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
class CoverageStats:
    """Statistics about rule coverage in the cache."""

    total_messages: int
    covered_messages: int
    uncovered_messages: int
    coverage_by_rule: dict[str, int] = field(default_factory=dict)

    @property
    def coverage_percentage(self) -> float:
        """Calculate coverage percentage."""
        if self.total_messages == 0:
            return 0.0
        return (self.covered_messages / self.total_messages) * 100


@dataclass
class UncoveredMessage:
    """Details of a single uncovered message."""

    uid: str
    folder: str
    from_address: str
    subject: str
    domain: str

    @classmethod
    def from_header(cls, uid: str, folder: str, header: dict[str, str]) -> UncoveredMessage:
        """Create from parsed header dictionary.

        Decodes MIME-encoded headers (subjects and display names).
        """
        # Decode MIME-encoded headers
        from_addr = _decode_mime_header(header.get("from", "")).strip()
        subject = _decode_mime_header(header.get("subject", "")).strip()

        # Extract domain from email
        if "@" in from_addr:
            # Handle "Name <email@domain.com>" format
            email_part = from_addr.split("<")[-1].rstrip(">").strip()
            domain = email_part.split("@")[-1] if "@" in email_part else "unknown"
        else:
            domain = "unknown"

        return cls(
            uid=uid,
            folder=folder,
            from_address=from_addr,
            subject=subject,
            domain=domain,
        )


@dataclass
class DomainCluster:
    """Group of uncovered emails from the same domain."""

    domain: str
    total_count: int
    senders: dict[str, int] = field(default_factory=dict)
    messages: list[UncoveredMessage] = field(default_factory=list)


@dataclass
class BatchTarget:
    """Target for batch rule creation."""

    target_type: str  # 'domain' or 'email'
    value: str
    estimated_count: int
    sample_messages: list[UncoveredMessage] = field(default_factory=list)


class RuleCoverageAnalyzer:
    """Analyzes which cached messages are covered by existing rules."""

    def __init__(
        self,
        db_path: Path,
        rules_dir: Path,
        logger=None,
    ):
        """Initialize the coverage analyzer.

        Args:
            db_path: Path to the cache database
            rules_dir: Path to the rules directory
            logger: Optional JsonLogger for logging
        """
        self.db_path = Path(db_path)
        self.rules_dir = Path(rules_dir)
        self.logger = logger

        # Connect to database
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        # Cached analysis results
        self._coverage_stats: Optional[CoverageStats] = None
        self._uncovered_messages: list[UncoveredMessage] = []
        self._domain_clusters: list[DomainCluster] = []

    def analyze_coverage(self) -> CoverageStats:
        """Analyze which cached messages are covered by existing rules.

        This method:
        1. Loads all rules from the rules directory
        2. Queries all cached messages
        3. Evaluates each message against all rules
        4. Returns coverage statistics

        Returns:
            CoverageStats with coverage information
        """
        # Load rules
        if self.logger:
            print("📜 Loading rules...")
        rules = load_rules(self.rules_dir, self.logger or self._create_dummy_logger())
        if not rules:
            print("⚠️  No rules found")
            rules = []

        # Sort rules by priority (descending) to match executor behavior
        rules.sort(key=lambda r: r.get("priority", 100), reverse=True)

        # Query all messages from cache
        cursor = self.conn.cursor()
        cursor.execute("SELECT folder, uid, data FROM headers ORDER BY folder, uid")
        all_rows = cursor.fetchall()
        total_count = len(all_rows)

        if total_count == 0:
            # No messages in cache
            self._coverage_stats = CoverageStats(
                total_messages=0,
                covered_messages=0,
                uncovered_messages=0,
                coverage_by_rule={},
            )
            self._uncovered_messages = []
            self._domain_clusters = []
            return self._coverage_stats

        # Analyze coverage
        covered_count = 0
        uncovered_messages: list[UncoveredMessage] = []
        coverage_by_rule: dict[str, int] = defaultdict(int)

        print(f"🔍 Analyzing {total_count:,} messages...")
        for folder, uid, data in tqdm(all_rows, desc="Coverage analysis", unit="msg"):
            # Parse header
            raw_header = _extract_raw_header(data)
            header = _parse_header_map(raw_header)

            # Find matching rule
            matching_rule = find_matching_rule(header, rules)

            if matching_rule:
                covered_count += 1
                rule_name = matching_rule.get("name", "Unknown")
                coverage_by_rule[rule_name] += 1
            else:
                # Uncovered message
                msg = UncoveredMessage.from_header(uid, folder, header)
                uncovered_messages.append(msg)

        # Store results
        self._uncovered_messages = uncovered_messages
        self._coverage_stats = CoverageStats(
            total_messages=total_count,
            covered_messages=covered_count,
            uncovered_messages=len(uncovered_messages),
            coverage_by_rule=dict(coverage_by_rule),
        )

        # Build domain clusters
        self._build_domain_clusters()

        return self._coverage_stats

    def _build_domain_clusters(self) -> None:
        """Build domain clusters from uncovered messages."""
        clusters_dict: dict[str, DomainCluster] = {}

        for msg in self._uncovered_messages:
            if msg.domain not in clusters_dict:
                clusters_dict[msg.domain] = DomainCluster(
                    domain=msg.domain,
                    total_count=0,
                    senders={},
                    messages=[],
                )

            cluster = clusters_dict[msg.domain]
            cluster.total_count += 1
            cluster.messages.append(msg)

            # Count senders
            if msg.from_address not in cluster.senders:
                cluster.senders[msg.from_address] = 0
            cluster.senders[msg.from_address] += 1

        # Sort clusters by count (descending)
        self._domain_clusters = sorted(
            clusters_dict.values(), key=lambda c: c.total_count, reverse=True
        )

    def get_coverage_stats(self) -> CoverageStats:
        """Get the latest coverage statistics.

        Returns:
            CoverageStats from the most recent analysis
        """
        if self._coverage_stats is None:
            raise RuntimeError("analyze_coverage() must be called first")
        return self._coverage_stats

    def get_uncovered_messages(self) -> list[UncoveredMessage]:
        """Get all uncovered messages.

        Returns:
            List of UncoveredMessage objects
        """
        if not hasattr(self, "_uncovered_messages"):
            raise RuntimeError("analyze_coverage() must be called first")
        return self._uncovered_messages

    def get_domain_clusters(self) -> list[DomainCluster]:
        """Get domain clusters of uncovered messages.

        Returns:
            List of DomainCluster objects sorted by message count
        """
        if not hasattr(self, "_domain_clusters"):
            raise RuntimeError("analyze_coverage() must be called first")
        return self._domain_clusters

    def find_cluster(self, domain: str) -> Optional[DomainCluster]:
        """Find a domain cluster by domain name.

        Args:
            domain: Domain name to search for

        Returns:
            DomainCluster if found, None otherwise
        """
        for cluster in self.get_domain_clusters():
            if cluster.domain == domain:
                return cluster
        return None

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    @staticmethod
    def _create_dummy_logger():
        """Create a dummy logger that ignores all output."""

        class DummyLogger:
            def log(self, *args, **kwargs):
                pass

        return DummyLogger()
