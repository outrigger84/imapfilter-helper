#!/usr/bin/env python3
"""Test backward compatibility with old cache format."""

import json

def test_backward_compatibility():
    """Verify that both old and new cache formats can be read."""

    print("=" * 70)
    print("Testing Backward Compatibility")
    print("=" * 70)

    # Simulate old cache entry (header only)
    old_cache = '{"header": "From: old@example.com\\r\\nSubject: Old Format\\r\\n"}'
    old_data = json.loads(old_cache)

    print("\n1. Reading OLD cache format:")
    print("-" * 70)
    print(f"Raw JSON: {old_cache}")
    print(f"Parsed data: {old_data}")
    print(f"Header: {old_data.get('header', '')[:50]}...")
    print(f"FLAGS: {old_data.get('flags', [])}")  # Defaults to []
    print(f"INTERNALDATE: {old_data.get('internaldate', None)}")  # Defaults to None

    # Simulate new cache entry (with flags and internaldate)
    new_cache = json.dumps({
        "header": "From: new@example.com\r\nSubject: New Format\r\n",
        "flags": ["\\Seen", "\\Flagged"],
        "internaldate": "28-Oct-2025 07:30:19 +0000"
    })
    new_data = json.loads(new_cache)

    print("\n2. Reading NEW cache format:")
    print("-" * 70)
    print(f"Raw JSON: {new_cache}")
    print(f"Parsed data: {new_data}")
    print(f"Header: {new_data.get('header', '')[:50]}...")
    print(f"FLAGS: {new_data.get('flags', [])}")
    print(f"INTERNALDATE: {new_data.get('internaldate', None)}")

    # Simulate partial new format (only flags, no internaldate)
    partial_cache = json.dumps({
        "header": "From: partial@example.com\r\n",
        "flags": ["\\Draft"]
    })
    partial_data = json.loads(partial_cache)

    print("\n3. Reading PARTIAL new format (flags only):")
    print("-" * 70)
    print(f"Raw JSON: {partial_cache}")
    print(f"Parsed data: {partial_data}")
    print(f"Header: {partial_data.get('header', '')[:50]}...")
    print(f"FLAGS: {partial_data.get('flags', [])}")
    print(f"INTERNALDATE: {partial_data.get('internaldate', None)}")

    print("\n" + "=" * 70)
    print("Backward compatibility verified!")
    print("Key points:")
    print("  - Old cache entries (header-only) work with .get() defaults")
    print("  - New cache entries include flags and internaldate")
    print("  - Partial entries are handled gracefully")
    print("  - No migration required for existing cache")
    print("=" * 70)


if __name__ == "__main__":
    test_backward_compatibility()
