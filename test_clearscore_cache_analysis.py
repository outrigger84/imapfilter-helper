#!/usr/bin/env python3
"""Analyze what clearscore.com senders are actually in the cache."""

import sys
import sqlite3
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))

from core.rule_engine import _extract_raw_header, _parse_header_map


def test_clearscore_cache_senders():
    """Check what clearscore senders are in the cache database."""

    db_path = Path("/root/imapfilter/data/cache.db")

    print("=" * 80)
    print("CLEARSCORE SENDERS IN CACHE")
    print("=" * 80)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Count total clearscore messages
    cursor.execute("SELECT COUNT(*) as count FROM headers WHERE data LIKE '%clearscore.com%'")
    total_clearscore = cursor.fetchone()["count"]
    print(f"\nTotal clearscore.com messages in cache: {total_clearscore:,}")

    # Get all clearscore senders
    print("\n" + "=" * 80)
    print("ALL CLEARSCORE SENDERS")
    print("=" * 80)

    cursor.execute("SELECT data FROM headers WHERE data LIKE '%clearscore.com%'")
    rows = cursor.fetchall()

    senders = Counter()
    for row in rows:
        data = row[0]
        raw_header = _extract_raw_header(data)
        header = _parse_header_map(raw_header)
        from_addr = header.get("from", "").strip()
        if "@clearscore.com" in from_addr:
            senders[from_addr] += 1

    print(f"\nFound {len(senders)} unique senders:\n")
    for sender, count in senders.most_common():
        print(f"  {sender}: {count:,}")

    # Check specifically for the excluded senders
    print("\n" + "=" * 80)
    print("EXCLUDED SENDERS (should have messages if there's an issue)")
    print("=" * 80)

    updates_count = senders.get("updates@clearscore.com", 0)
    alerts_count = senders.get("alerts@clearscore.com", 0)

    print(f"\nupdates@clearscore.com: {updates_count:,}")
    print(f"alerts@clearscore.com: {alerts_count:,}")

    if updates_count == 0 and alerts_count == 0:
        print("\n⚠️  No messages from excluded senders in cache!")
        print("This means ALL clearscore.com messages are from OTHER senders,")
        print("so the rule correctly marks them all as covered.")
    else:
        print("\n✓ Found messages from excluded senders")
        print("This means these should appear as uncovered messages!")

    conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(test_clearscore_cache_senders())
