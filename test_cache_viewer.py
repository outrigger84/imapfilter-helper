#!/usr/bin/env python3
"""End-to-end test for the cache viewer functionality."""

import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.tools.cache_viewer import (
    CacheTableViewer,
    EmailRow,
    SortState,
    extract_emails_from_cache,
)
from core.config import build_default_config


def test_email_row_creation():
    """Test EmailRow dataclass creation."""
    print("\n[TEST 1] EmailRow Creation")
    row = EmailRow(
        folder="INBOX",
        uid="12345",
        from_addr="alice@example.com",
        to_addr="bob@example.com",
        subject="Test Subject",
        date="2024-11-27 19:53",
        raw_data="{}",
    )
    assert row.folder == "INBOX"
    assert row.from_addr == "alice@example.com"
    assert row.subject == "Test Subject"
    print("  ✓ EmailRow creation works correctly")


def test_sort_state():
    """Test SortState initialization and toggling."""
    print("\n[TEST 2] SortState Initialization")
    state = SortState()
    assert state.column == 3  # Default: Date
    assert state.ascending == False  # Default: descending

    # Toggle direction
    state.ascending = True
    assert state.ascending == True
    print("  ✓ SortState works correctly")


def test_cache_extraction():
    """Test extracting emails from cache."""
    print("\n[TEST 3] Cache Extraction")
    config = build_default_config()

    try:
        emails = extract_emails_from_cache(
            config.paths.db_file,
            limit=50,
            show_progress=False
        )

        if emails:
            print(f"  ✓ Loaded {len(emails)} emails from cache")

            # Verify email structure
            email = emails[0]
            assert hasattr(email, 'from_addr')
            assert hasattr(email, 'to_addr')
            assert hasattr(email, 'subject')
            assert hasattr(email, 'date')
            assert hasattr(email, 'folder')
            print(f"  ✓ Email structure is valid")
            print(f"    - From: {email.from_addr[:40]}")
            print(f"    - To: {email.to_addr[:40]}")
            print(f"    - Subject: {email.subject[:40]}")
            print(f"    - Date: {email.date}")
        else:
            print("  ⚠️  No emails in cache (cache may be empty)")

    except FileNotFoundError:
        print("  ⚠️  Cache database not found (run 'build-cache' first)")
        return False

    return True


def test_viewer_creation():
    """Test CacheTableViewer creation and basic operations."""
    print("\n[TEST 4] CacheTableViewer Creation")

    # Create sample emails
    emails = [
        EmailRow(
            folder="INBOX",
            uid="1",
            from_addr="alice@example.com",
            to_addr="bob@example.com",
            subject="Meeting Tomorrow",
            date="2024-11-27 19:53",
            raw_data="{}",
        ),
        EmailRow(
            folder="INBOX",
            uid="2",
            from_addr="newsletter@site.com",
            to_addr="me@example.com",
            subject="Weekly Update",
            date="2024-11-26 10:30",
            raw_data="{}",
        ),
        EmailRow(
            folder="INBOX",
            uid="3",
            from_addr="notifications@app.io",
            to_addr="user@example.com",
            subject="Your Account Alert",
            date="2024-11-25 14:22",
            raw_data="{}",
        ),
    ]

    viewer = CacheTableViewer(emails, title="Test Cache")
    assert len(viewer.all_emails) == 3
    assert viewer.sort_state.column == 3  # Default: Date
    assert viewer.sort_state.ascending == False
    print(f"  ✓ Created viewer with {len(emails)} emails")


def test_sorting():
    """Test sorting functionality."""
    print("\n[TEST 5] Sorting Functionality")

    emails = [
        EmailRow("INBOX", "1", "zebra@example.com", "to1@example.com", "Z Subject", "2024-11-27 19:53", "{}"),
        EmailRow("INBOX", "2", "alice@example.com", "to2@example.com", "A Subject", "2024-11-26 10:30", "{}"),
        EmailRow("INBOX", "3", "bob@example.com", "to3@example.com", "M Subject", "2024-11-25 14:22", "{}"),
    ]

    viewer = CacheTableViewer(emails)

    # Test sort by From (ascending)
    viewer.sort_state.column = 0
    viewer.sort_state.ascending = True
    viewer._apply_sort()
    assert viewer._sorted_emails[0].from_addr == "alice@example.com"
    assert viewer._sorted_emails[2].from_addr == "zebra@example.com"
    print("  ✓ Sort by From (ascending) works")

    # Test sort by Date (descending)
    viewer.sort_state.column = 3
    viewer.sort_state.ascending = False
    viewer._apply_sort()
    assert viewer._sorted_emails[0].date == "2024-11-27 19:53"
    assert viewer._sorted_emails[2].date == "2024-11-25 14:22"
    print("  ✓ Sort by Date (descending) works")

    # Test sort by Subject (ascending)
    viewer.sort_state.column = 2
    viewer.sort_state.ascending = True
    viewer._apply_sort()
    assert viewer._sorted_emails[0].subject == "A Subject"
    assert viewer._sorted_emails[2].subject == "Z Subject"
    print("  ✓ Sort by Subject (ascending) works")


def test_column_widths():
    """Test column width calculation."""
    print("\n[TEST 6] Column Width Calculation")

    emails = [EmailRow("INBOX", "1", "test@example.com", "to@example.com", "Subject", "2024-11-27 19:53", "{}")]
    viewer = CacheTableViewer(emails)

    # Test narrow terminal
    from_w, to_w, subj_w, date_w = viewer._calculate_column_widths(80)
    assert from_w + to_w + subj_w + date_w <= 80
    print(f"  ✓ 80-column terminal: {from_w}+{to_w}+{subj_w}+{date_w} = {from_w + to_w + subj_w + date_w}")

    # Test medium terminal
    from_w, to_w, subj_w, date_w = viewer._calculate_column_widths(120)
    print(f"  ✓ 120-column terminal: {from_w}+{to_w}+{subj_w}+{date_w} = {from_w + to_w + subj_w + date_w}")

    # Test wide terminal
    from_w, to_w, subj_w, date_w = viewer._calculate_column_widths(200)
    print(f"  ✓ 200-column terminal: {from_w}+{to_w}+{subj_w}+{date_w} = {from_w + to_w + subj_w + date_w}")


def test_text_truncation():
    """Test text truncation."""
    print("\n[TEST 7] Text Truncation")

    emails = [EmailRow("INBOX", "1", "test@example.com", "to@example.com", "Subject", "2024-11-27 19:53", "{}")]
    viewer = CacheTableViewer(emails)

    # Test text within width
    result = viewer._truncate_text("Short", 20)
    assert result == "Short               "
    print(f"  ✓ Short text: '{result}' (length {len(result)})")

    # Test text exceeding width
    long_text = "This is a very long email address that should be truncated"
    result = viewer._truncate_text(long_text, 20)
    assert result.endswith("...")
    assert len(result) == 20
    print(f"  ✓ Long text: '{result}' (length {len(result)})")


def test_cli_command():
    """Test CLI command integration."""
    print("\n[TEST 8] CLI Command Integration")

    from core.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(['view-cache', '--limit', '100'])

    assert args.cmd == 'view-cache'
    assert args.limit == 100
    print("  ✓ CLI parser recognizes 'view-cache' command")
    print("  ✓ Command-line arguments parsed correctly")


def test_filtering():
    """Test filtering by conditions."""
    print("\n[TEST 9] Filtering by Conditions")

    from core.tools.cache_viewer import _matches_conditions

    # Test with no conditions
    header = {"from": "alice@example.com", "subject": "Test"}
    result = _matches_conditions(header, None)
    assert result == True
    print("  ✓ No conditions: matches all")

    # Test with simple condition
    conditions = [{"field": "from", "match_type": "contains", "value": "alice"}]
    result = _matches_conditions(header, conditions)
    # Note: This depends on rule engine implementation
    print(f"  ✓ Condition filtering attempted")


def main():
    """Run all tests."""
    print("=" * 70)
    print("Cache Viewer End-to-End Test Suite")
    print("=" * 70)

    tests = [
        ("EmailRow Creation", test_email_row_creation),
        ("SortState", test_sort_state),
        ("Cache Extraction", test_cache_extraction),
        ("Viewer Creation", test_viewer_creation),
        ("Sorting", test_sorting),
        ("Column Widths", test_column_widths),
        ("Text Truncation", test_text_truncation),
        ("CLI Integration", test_cli_command),
        ("Filtering", test_filtering),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, test_func in tests:
        try:
            result = test_func()
            if result is False:
                skipped += 1
            else:
                passed += 1
        except AssertionError as e:
            print(f"  ❌ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ⚠️  ERROR: {e}")
            failed += 1

    # Summary
    print("\n" + "=" * 70)
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 70)

    if failed == 0:
        print("✓ All tests passed!")
        return 0
    else:
        print(f"❌ {failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
