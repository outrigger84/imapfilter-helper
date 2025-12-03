#!/usr/bin/env python3
"""
Pattern Extraction Demo

This script demonstrates how to use EmailPatternExtractor and SubjectPatternExtractor
to analyze email patterns and suggest rule criteria based on cache data.

Usage:
    python3 examples/pattern_extraction_demo.py

Requirements:
    - A built cache database (run with --build-cache first)
    - Cache database located at data/cache.db
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.tools import (
    CacheQueryEngine,
    EmailPatternExtractor,
    SubjectPatternExtractor,
)


def demo_email_patterns(cache_engine: CacheQueryEngine):
    """Demonstrate email pattern extraction."""
    print("\n" + "=" * 80)
    print("EMAIL PATTERN EXTRACTION DEMO")
    print("=" * 80)

    # Get some sample email addresses from cache
    print("\nExtracting unique email addresses from cache...")
    from_addresses = cache_engine.extract_unique_from_addresses(limit=5)

    if not from_addresses:
        print("No email addresses found in cache. Run with --build-cache first.")
        return

    print(f"Found {len(from_addresses)} unique senders in cache")
    print("\nAnalyzing top 3 senders:\n")

    extractor = EmailPatternExtractor()

    for email_addr, count in from_addresses[:3]:
        print("-" * 80)
        print(f"Email: {email_addr}")
        print(f"Original count: {count} messages")
        print("-" * 80)

        patterns = extractor.suggest_patterns(email_addr, cache_engine)

        if patterns:
            print(f"\n{'Pattern':<40} {'Description':<25} {'Count':>10}")
            print("-" * 80)
            for pattern, desc, est_count in patterns:
                print(f"{pattern:<40} {desc:<25} {est_count:>10}")
        else:
            print("No patterns could be extracted")

        print("\nRule Suggestion:")
        if len(patterns) > 1:
            # Suggest using domain pattern for better coverage
            _, desc, pattern_count = patterns[-1]
            print(f"  Consider using: {patterns[-1][0]}")
            print(f"  This would match {pattern_count} messages ({desc})")
        else:
            print(f"  Use exact match: {email_addr}")

        print()


def demo_subject_patterns(cache_engine: CacheQueryEngine):
    """Demonstrate subject pattern extraction."""
    print("\n" + "=" * 80)
    print("SUBJECT PATTERN EXTRACTION DEMO")
    print("=" * 80)

    # Get some sample subjects from cache
    print("\nExtracting unique subjects from cache...")
    subjects = cache_engine.extract_unique_subjects(limit=5)

    if not subjects:
        print("No subjects found in cache. Run with --build-cache first.")
        return

    print(f"Found {len(subjects)} unique subjects in cache")
    print("\nAnalyzing top 3 subjects:\n")

    extractor = SubjectPatternExtractor()

    for subject, count in subjects[:3]:
        # Truncate long subjects for display
        display_subject = subject if len(subject) <= 60 else subject[:57] + "..."

        print("-" * 80)
        print(f"Subject: {display_subject}")
        print(f"Original count: {count} messages")
        print("-" * 80)

        patterns = extractor.suggest_patterns(subject, cache_engine)

        if patterns:
            print(f"\n{'Pattern':<45} {'Description':<25} {'Count':>8}")
            print("-" * 80)
            for pattern, desc, est_count in patterns:
                # Truncate long patterns
                display_pattern = pattern if len(pattern) <= 45 else pattern[:42] + "..."
                print(f"{display_pattern:<45} {desc:<25} {est_count:>8}")
        else:
            print("No patterns could be extracted")

        print("\nRule Suggestion:")
        if len(patterns) > 2:
            # Suggest a middle-ground pattern
            mid_idx = len(patterns) // 2
            pattern, desc, pattern_count = patterns[mid_idx]
            print(f"  Consider using: {pattern}")
            print(f"  This would match {pattern_count} messages ({desc})")
        elif len(patterns) > 1:
            print(f"  Consider using: {patterns[1][0]}")
            print(f"  This would match {patterns[1][2]} messages ({patterns[1][1]})")
        else:
            print(f"  Use exact match: {subject}")

        print()


def demo_custom_patterns(cache_engine: CacheQueryEngine):
    """Demonstrate pattern extraction with custom inputs."""
    print("\n" + "=" * 80)
    print("CUSTOM PATTERN ANALYSIS")
    print("=" * 80)

    email_extractor = EmailPatternExtractor()
    subject_extractor = SubjectPatternExtractor()

    # Example custom email
    custom_email = "noreply@example.com"
    print(f"\nAnalyzing custom email: {custom_email}")
    print("-" * 80)

    email_patterns = email_extractor.suggest_patterns(custom_email, cache_engine)
    if email_patterns:
        for pattern, desc, count in email_patterns:
            print(f"  {pattern:<35} {desc:<25} {count:>8} messages")
    else:
        print("  No patterns found")

    # Example custom subject
    custom_subject = "Order Confirmation #12345"
    print(f"\nAnalyzing custom subject: {custom_subject}")
    print("-" * 80)

    subject_patterns = subject_extractor.suggest_patterns(custom_subject, cache_engine)
    if subject_patterns:
        for pattern, desc, count in subject_patterns:
            print(f"  {pattern:<35} {desc:<25} {count:>8} messages")
    else:
        print("  No patterns found")


def main():
    """Main entry point."""
    # Check if cache database exists
    cache_path = Path("data/cache.db")

    if not cache_path.exists():
        print(f"Error: Cache database not found at {cache_path}")
        print("\nPlease run the following command first:")
        print("  python3 -m core.cli --build-cache")
        return 1

    print(f"Using cache database: {cache_path}")

    # Initialize cache query engine
    try:
        cache_engine = CacheQueryEngine(cache_path)
    except Exception as e:
        print(f"Error connecting to cache database: {e}")
        return 1

    # Run demos
    try:
        demo_email_patterns(cache_engine)
        demo_subject_patterns(cache_engine)
        demo_custom_patterns(cache_engine)

        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print("""
These pattern extractors help create flexible rules by:

1. EmailPatternExtractor:
   - Suggests patterns from specific (exact match) to broad (domain base)
   - Helps catch all emails from a sender or domain
   - Shows estimated impact of each pattern

2. SubjectPatternExtractor:
   - Removes variable content (numbers, IDs)
   - Extracts keywords and common prefixes
   - Matches similar subjects while ignoring tracking codes

Use these tools to build efficient rules that match multiple related messages
without being too broad or too narrow.
        """)

    finally:
        cache_engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
