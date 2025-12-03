#!/usr/bin/env python3
"""Test script to demonstrate EmailPatternExtractor and SubjectPatternExtractor."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from core.tools.rule_wizard_core import (
    CacheQueryEngine,
    EmailPatternExtractor,
    SubjectPatternExtractor,
)


def create_test_database() -> Path:
    """Create a temporary test database with sample message headers."""
    db_path = Path("/tmp/test_pattern_cache.db")

    # Remove existing test db
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE headers ("
        "folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
        "PRIMARY KEY (folder, uid))"
    )

    # Create sample headers for testing
    sample_headers = [
        # Amazon emails
        {
            "from": "noreply@amazon.com",
            "subject": "Your Amazon order has shipped",
        },
        {
            "from": "noreply@amazon.co.uk",
            "subject": "Your Amazon Prime subscription",
        },
        {
            "from": "auto-confirm@amazon.com",
            "subject": "Order Confirmation #123-4567890-1234567",
        },
        {
            "from": "shipment-tracking@amazon.com",
            "subject": "Order #123-9876543-9876543 has shipped",
        },
        # Booking.com emails
        {
            "from": "noreply@booking.com",
            "subject": "Your Booking Confirmation For BRS-SRS-36558426",
        },
        {
            "from": "noreply@booking.com",
            "subject": "Your Booking Confirmation For LHR-NYC-12345678",
        },
        {
            "from": "noreply@booking.com",
            "subject": "Your Booking Confirmation For PAR-LON-98765432",
        },
        # GitHub notifications
        {
            "from": "notifications@github.com",
            "subject": "[myrepo] New issue opened #123",
        },
        {
            "from": "notifications@github.com",
            "subject": "[myrepo] New issue opened #456",
        },
        {
            "from": "notifications@github.com",
            "subject": "[otherrepo] Pull request merged #789",
        },
        # Newsletter
        {
            "from": "newsletter@example.com",
            "subject": "Weekly Newsletter - Week 45",
        },
        {
            "from": "newsletter@example.com",
            "subject": "Weekly Newsletter - Week 46",
        },
    ]

    # Insert sample data
    for i, header_dict in enumerate(sample_headers):
        # Create a minimal RFC 822 header string
        header_lines = []
        for key, value in header_dict.items():
            header_lines.append(f"{key.title()}: {value}")
        raw_header = "\r\n".join(header_lines)

        data = json.dumps({"header": raw_header})
        conn.execute(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
            ("INBOX", str(i + 1), data, "2025-01-01T00:00:00Z"),
        )

    conn.commit()
    conn.close()

    print(f"Created test database: {db_path}")
    print(f"Inserted {len(sample_headers)} sample messages")
    return db_path


def test_email_patterns():
    """Test EmailPatternExtractor with sample data."""
    print("\n" + "=" * 70)
    print("TESTING EmailPatternExtractor")
    print("=" * 70)

    db_path = create_test_database()
    cache = CacheQueryEngine(db_path)
    extractor = EmailPatternExtractor()

    test_emails = [
        "noreply@amazon.com",
        "notifications@github.com",
        "newsletter@example.com",
    ]

    for email_addr in test_emails:
        print(f"\n>>> Email: {email_addr}")
        print("-" * 70)
        patterns = extractor.suggest_patterns(email_addr, cache)

        if patterns:
            print(f"{'Pattern':<35} {'Description':<25} {'Count':>8}")
            print("-" * 70)
            for pattern, desc, count in patterns:
                print(f"{pattern:<35} {desc:<25} {count:>8}")
        else:
            print("No patterns generated")

    cache.close()


def test_subject_patterns():
    """Test SubjectPatternExtractor with sample data."""
    print("\n" + "=" * 70)
    print("TESTING SubjectPatternExtractor")
    print("=" * 70)

    db_path = Path("/tmp/test_pattern_cache.db")
    cache = CacheQueryEngine(db_path)
    extractor = SubjectPatternExtractor()

    test_subjects = [
        "Your Booking Confirmation For BRS-SRS-36558426",
        "Order Confirmation #123-4567890-1234567",
        "[myrepo] New issue opened #123",
        "Weekly Newsletter - Week 45",
    ]

    for subject in test_subjects:
        print(f"\n>>> Subject: {subject}")
        print("-" * 70)
        patterns = extractor.suggest_patterns(subject, cache)

        if patterns:
            print(f"{'Pattern':<50} {'Description':<25} {'Count':>8}")
            print("-" * 70)
            for pattern, desc, count in patterns:
                # Truncate long patterns for display
                display_pattern = pattern if len(pattern) <= 50 else pattern[:47] + "..."
                print(f"{display_pattern:<50} {desc:<25} {count:>8}")
        else:
            print("No patterns generated")

    cache.close()


def test_edge_cases():
    """Test edge cases and error handling."""
    print("\n" + "=" * 70)
    print("TESTING Edge Cases")
    print("=" * 70)

    db_path = Path("/tmp/test_pattern_cache.db")
    cache = CacheQueryEngine(db_path)
    email_extractor = EmailPatternExtractor()
    subject_extractor = SubjectPatternExtractor()

    # Test empty inputs
    print("\n>>> Empty email: ''")
    print(f"Result: {email_extractor.suggest_patterns('', cache)}")

    print("\n>>> Empty subject: ''")
    print(f"Result: {subject_extractor.suggest_patterns('', cache)}")

    # Test email without @
    print("\n>>> Email without @: 'amazon.com'")
    patterns = email_extractor.suggest_patterns('amazon.com', cache)
    for pattern, desc, count in patterns:
        print(f"  {pattern} -> {desc} ({count} messages)")

    # Test very short subject
    print("\n>>> Very short subject: 'Hi'")
    patterns = subject_extractor.suggest_patterns('Hi', cache)
    print(f"Result: {patterns}")

    # Test subject with only numbers
    print("\n>>> Subject with only numbers: '123456'")
    patterns = subject_extractor.suggest_patterns('123456', cache)
    for pattern, desc, count in patterns:
        print(f"  {pattern} -> {desc} ({count} messages)")

    cache.close()


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("Pattern Extractor Test Suite")
    print("=" * 70)

    test_email_patterns()
    test_subject_patterns()
    test_edge_cases()

    print("\n" + "=" * 70)
    print("All tests completed!")
    print("=" * 70)
