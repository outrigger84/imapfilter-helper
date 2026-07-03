#!/usr/bin/env python3
"""Test to check if coverage analyzer correctly identifies uncovered clearscore addresses."""

import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.rule_engine import load_rules, find_matching_rule, _extract_raw_header, _parse_header_map
from core.tools.coverage_analyzer import RuleCoverageAnalyzer


def test_coverage_analysis_with_clearscore_rule():
    """Test that coverage analyzer correctly identifies uncovered messages with clearscore rule."""

    db_path = Path("/root/imapfilter/data/cache.db")
    rules_dir = Path("/root/imapfilter/rules")

    if not db_path.exists():
        print(f"❌ Cache database not found at {db_path}")
        return 1

    print("=" * 80)
    print("Testing Coverage Analysis with Clearscore Rule")
    print("=" * 80)

    # Create coverage analyzer
    analyzer = RuleCoverageAnalyzer(db_path, rules_dir)

    try:
        # Analyze coverage
        print("\nRunning coverage analysis...")
        stats = analyzer.analyze_coverage()

        print(f"\nCoverage Statistics:")
        print(f"  Total messages: {stats.total_messages:,}")
        print(f"  Covered messages: {stats.covered_messages:,}")
        print(f"  Uncovered messages: {stats.uncovered_messages:,}")
        print(f"  Coverage percentage: {stats.coverage_percentage:.1f}%")

        print(f"\nCoverage by rule:")
        for rule_name, count in sorted(stats.coverage_by_rule.items(), key=lambda x: x[1], reverse=True):
            print(f"  {rule_name}: {count:,}")

        # Check specifically for clearscore messages
        print("\n" + "=" * 80)
        print("Checking Clearscore Addresses Coverage:")
        print("=" * 80)

        uncovered = analyzer.get_uncovered_messages()
        clearscore_uncovered = [msg for msg in uncovered if "@clearscore.com" in msg.from_address]

        print(f"\nFound {len(clearscore_uncovered)} uncovered clearscore.com messages:")
        for msg in clearscore_uncovered[:20]:  # Show first 20
            print(f"  - {msg.from_address}")

        # Also check covered messages
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        print("\nChecking covered clearscore.com messages:")
        cursor.execute("SELECT folder, uid, data FROM headers ORDER BY uid LIMIT 10000")
        rows = cursor.fetchall()

        rules = load_rules(rules_dir)
        rules.sort(key=lambda r: r.get("priority", 100), reverse=True)

        clearscore_covered = {}
        for folder, uid, data in rows:
            raw_header = _extract_raw_header(data)
            header = _parse_header_map(raw_header)
            from_addr = header.get("from", "").strip()

            if "@clearscore.com" not in from_addr:
                continue

            matching_rule = find_matching_rule(header, rules)
            if matching_rule:
                rule_name = matching_rule.get("name", "Unknown")
                if rule_name not in clearscore_covered:
                    clearscore_covered[rule_name] = []
                clearscore_covered[rule_name].append(from_addr)

        if clearscore_covered:
            print(f"Found {sum(len(v) for v in clearscore_covered.values())} covered clearscore.com messages:")
            for rule_name, addresses in clearscore_covered.items():
                print(f"\n  Rule: {rule_name}")
                unique_addresses = set(addresses)
                for addr in sorted(unique_addresses):
                    count = addresses.count(addr)
                    print(f"    - {addr} ({count})")
        else:
            print("No covered clearscore.com messages found")

        conn.close()

        # Check the domain clusters for clearscore
        print("\n" + "=" * 80)
        print("Domain Clusters (Uncovered Messages Only):")
        print("=" * 80)

        clusters = analyzer.get_domain_clusters()
        clearscore_cluster = next((c for c in clusters if c.domain == "clearscore.com"), None)

        if clearscore_cluster:
            print(f"\nClearscore Domain Cluster:")
            print(f"  Total count: {clearscore_cluster.total_count}")
            print(f"  Senders:")
            for sender, count in sorted(clearscore_cluster.senders.items(), key=lambda x: x[1], reverse=True):
                print(f"    - {sender}: {count}")
        else:
            print("\n❌ No clearscore.com domain cluster found in uncovered messages!")
            print("   This suggests all clearscore.com messages are marked as covered.")

        analyzer.close()

    except Exception as e:
        print(f"\n❌ Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(test_coverage_analysis_with_clearscore_rule())
