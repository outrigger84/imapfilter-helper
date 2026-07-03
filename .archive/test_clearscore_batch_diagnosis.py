#!/usr/bin/env python3
"""Diagnose the batch mode clearscore issue by analyzing actual coverage data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.tools.coverage_analyzer import RuleCoverageAnalyzer


def test_batch_mode_clearscore():
    """Test what appears in batch mode for clearscore.com."""

    db_path = Path("/root/imapfilter/data/cache.db")
    rules_dir = Path("/root/imapfilter/rules")

    print("=" * 80)
    print("BATCH MODE DIAGNOSIS - Clearscore.com")
    print("=" * 80)

    analyzer = RuleCoverageAnalyzer(db_path, rules_dir)

    try:
        # Analyze coverage
        print("\nAnalyzing coverage (this may take a moment)...")
        stats = analyzer.analyze_coverage()

        print(f"\n✅ Coverage analysis complete!")
        print(f"   Total: {stats.total_messages:,}")
        print(f"   Covered: {stats.covered_messages:,}")
        print(f"   Uncovered: {stats.uncovered_messages:,}")

        # Get domain clusters (what appears in batch mode)
        clusters = analyzer.get_domain_clusters()
        print(f"\n📊 Total domain clusters (from uncovered messages): {len(clusters)}")

        # Find clearscore cluster
        clearscore_cluster = None
        for cluster in clusters:
            if cluster.domain == "clearscore.com":
                clearscore_cluster = cluster
                break

        print("\n" + "=" * 80)
        print("CLEARSCORE.COM STATUS IN BATCH MODE")
        print("=" * 80)

        if clearscore_cluster:
            print(f"\n✅ Clearscore.com IS in the batch domain list")
            print(f"   Total uncovered messages: {clearscore_cluster.total_count}")
            print(f"   Senders:")
            for sender, count in sorted(clearscore_cluster.senders.items(), key=lambda x: x[1], reverse=True):
                print(f"      - {sender}: {count}")
        else:
            print(f"\n❌ Clearscore.com is NOT in the batch domain list")
            print(f"   This means either:")
            print(f"   1. All clearscore.com messages are covered by existing rules")
            print(f"   2. There are no clearscore.com messages in the cache")

        # Check uncovered clearscore messages directly
        print("\n" + "=" * 80)
        print("UNCOVERED CLEARSCORE MESSAGES (Direct check)")
        print("=" * 80)

        uncovered = analyzer.get_uncovered_messages()
        clearscore_uncovered = [msg for msg in uncovered if "@clearscore.com" in msg.from_address]

        print(f"\nUncovered clearscore.com messages: {len(clearscore_uncovered)}")
        if clearscore_uncovered:
            print("Sample uncovered messages:")
            for msg in clearscore_uncovered[:10]:
                print(f"  - {msg.from_address}")
        else:
            print("  (none)")

        # Check what rules cover clearscore messages
        print("\n" + "=" * 80)
        print("RULE COVERAGE ANALYSIS")
        print("=" * 80)

        coverage_by_rule = stats.coverage_by_rule
        clearscore_covering_rules = {}

        # Parse the coverage analysis
        print(f"\nRules with any coverage:")
        for rule_name, count in sorted(coverage_by_rule.items(), key=lambda x: x[1], reverse=True)[:10]:
            if "clearscore" in rule_name.lower():
                print(f"  ✓ {rule_name}: {count:,} messages")
                clearscore_covering_rules[rule_name] = count
            elif rule_name.startswith("Newsletters") or rule_name.startswith("Personal") or rule_name.startswith("Bulk"):
                print(f"  • {rule_name}: {count:,} messages")

        if clearscore_covering_rules:
            print(f"\n✓ Found {len(clearscore_covering_rules)} clearscore-related rule(s)")
        else:
            print(f"\n⚠️  No clearscore-related rules found in coverage!")

        analyzer.close()

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(test_batch_mode_clearscore())
