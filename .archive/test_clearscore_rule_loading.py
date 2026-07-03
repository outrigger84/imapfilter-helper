#!/usr/bin/env python3
"""Test to verify the clearscore rule is loaded correctly."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.rule_engine import load_rules, conditions_match, find_matching_rule
from core.logging_utils import JsonLogger


def test_clearscore_rule_loading():
    """Load the clearscore rule and test its conditions."""

    rules_dir = Path("/root/imapfilter/rules")

    print("=" * 80)
    print("CLEARSCORE RULE LOADING AND EVALUATION TEST")
    print("=" * 80)

    # Create a dummy logger
    logger = JsonLogger(log_file="/tmp/dummy_log.json")

    # Load rules
    rules = load_rules(rules_dir, logger)
    print(f"\nLoaded {len(rules)} rules total")

    # Find clearscore rule
    clearscore_rule = None
    for rule in rules:
        if rule.get("name") == "Newsletters » Clearscore":
            clearscore_rule = rule
            break

    if not clearscore_rule:
        print("\n❌ Clearscore rule not found!")
        return 1

    print(f"\n✓ Found: {clearscore_rule['name']}")
    print(f"  Priority: {clearscore_rule.get('priority', 100)}")

    # Print the rule structure
    print("\n" + "=" * 80)
    print("RULE STRUCTURE")
    print("=" * 80)
    print(json.dumps(clearscore_rule.get("conditions", {}), indent=2))

    # Test evaluation
    print("\n" + "=" * 80)
    print("CONDITION EVALUATION")
    print("=" * 80)

    test_emails = [
        ("marketing@clearscore.com", True, "Should match - in domain, not excluded"),
        ("default@clearscore.com", True, "Should match - in domain, not excluded"),
        ("updates@clearscore.com", False, "Should NOT match - explicitly excluded"),
        ("alerts@clearscore.com", False, "Should NOT match - explicitly excluded"),
        ("other@clearscore.com", True, "Should match - in domain, not excluded"),
    ]

    all_passed = True
    for email, expected, explanation in test_emails:
        header = {"from": email}
        result = conditions_match(header, clearscore_rule.get("conditions", {}))
        status = "✓" if result == expected else "✗"
        print(f"\n{status} {email}")
        print(f"   Expected: {expected}, Got: {result}")
        print(f"   Reason: {explanation}")
        if result != expected:
            all_passed = False

    # Test with find_matching_rule (what coverage analyzer uses)
    print("\n" + "=" * 80)
    print("FIND_MATCHING_RULE TEST (Coverage Analyzer)")
    print("=" * 80)

    for email, expected, _ in test_emails:
        header = {"from": email}
        matching = find_matching_rule(header, [clearscore_rule])
        found = matching is not None
        status = "✓" if found == expected else "✗"
        print(f"\n{status} {email}")
        print(f"   Expected match: {expected}, Got match: {found}")
        if found != expected:
            all_passed = False

    print("\n" + "=" * 80)
    if all_passed:
        print("✅ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed - there's a problem with the rule evaluation!")
        return 1


if __name__ == "__main__":
    sys.exit(test_clearscore_rule_loading())
