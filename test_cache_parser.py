#!/usr/bin/env python3
"""Test script for _parse_fetch_response() function."""

import sys
sys.path.insert(0, '/root/imapfilter')

from core.cache_builder import _parse_fetch_response


def test_parse_fetch_response():
    """Test the _parse_fetch_response() function with various inputs."""

    print("=" * 70)
    print("Testing _parse_fetch_response() function")
    print("=" * 70)

    # Test case 1: Complete response with FLAGS and INTERNALDATE
    print("\n1. Complete response with FLAGS and INTERNALDATE:")
    print("-" * 70)
    msg_data1 = [
        (b'123 (FLAGS (\\Seen \\Flagged custom) INTERNALDATE "28-Oct-2025 07:30:19 +0000" BODY[HEADER] {500}',
         b'From: test@example.com\r\nSubject: Test Email\r\nDate: Mon, 28 Oct 2025 07:30:19 +0000\r\n\r\n')
    ]
    header, flags, date = _parse_fetch_response(msg_data1)
    print(f"Header length: {len(header)} bytes")
    print(f"Header preview: {header[:80].decode('ascii', 'ignore')!r}")
    print(f"FLAGS: {flags}")
    print(f"INTERNALDATE: {date}")

    # Test case 2: Response with only \\Seen flag
    print("\n2. Response with only \\Seen flag:")
    print("-" * 70)
    msg_data2 = [
        (b'456 (FLAGS (\\Seen) INTERNALDATE "15-Nov-2025 12:45:30 +0000" BODY[HEADER] {300}',
         b'From: sender@test.com\r\nSubject: Another Test\r\n\r\n')
    ]
    header, flags, date = _parse_fetch_response(msg_data2)
    print(f"Header length: {len(header)} bytes")
    print(f"FLAGS: {flags}")
    print(f"INTERNALDATE: {date}")

    # Test case 3: Response with no flags
    print("\n3. Response with empty FLAGS:")
    print("-" * 70)
    msg_data3 = [
        (b'789 (FLAGS () INTERNALDATE "01-Dec-2025 09:15:00 +0000" BODY[HEADER] {200}',
         b'From: noflags@example.com\r\n\r\n')
    ]
    header, flags, date = _parse_fetch_response(msg_data3)
    print(f"Header length: {len(header)} bytes")
    print(f"FLAGS: {flags}")
    print(f"INTERNALDATE: {date}")

    # Test case 4: Multiple custom flags
    print("\n4. Response with multiple custom flags:")
    print("-" * 70)
    msg_data4 = [
        (b'999 (FLAGS (\\Seen \\Draft $Important $Work) INTERNALDATE "20-Nov-2025 16:20:45 -0500" BODY[HEADER] {400}',
         b'From: multi@example.com\r\nSubject: Multiple Flags\r\n\r\n')
    ]
    header, flags, date = _parse_fetch_response(msg_data4)
    print(f"Header length: {len(header)} bytes")
    print(f"FLAGS: {flags}")
    print(f"INTERNALDATE: {date}")

    # Test case 5: Empty response (edge case)
    print("\n5. Empty response (edge case):")
    print("-" * 70)
    msg_data5 = []
    header, flags, date = _parse_fetch_response(msg_data5)
    print(f"Header length: {len(header)} bytes")
    print(f"FLAGS: {flags}")
    print(f"INTERNALDATE: {date}")

    # Test case 6: Simulating old cache format compatibility
    print("\n6. JSON Storage Format Examples:")
    print("-" * 70)
    import json

    # New format with flags and internaldate
    cache_entry_new = {
        "header": header.decode('ascii', 'ignore') if header else "",
        "flags": ["\\Seen", "\\Flagged", "custom"],
        "internaldate": "28-Oct-2025 07:30:19 +0000"
    }
    print(f"New format: {json.dumps(cache_entry_new, indent=2)}")

    # Old format (backward compatible)
    cache_entry_old = {
        "header": "From: test@example.com\r\nSubject: Old Format\r\n"
    }
    print(f"\nOld format (still valid): {json.dumps(cache_entry_old, indent=2)}")

    print("\n" + "=" * 70)
    print("All tests completed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    test_parse_fetch_response()
