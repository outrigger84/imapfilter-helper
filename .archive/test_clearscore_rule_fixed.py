#!/usr/bin/env python3
"""Test the fixed clearscore rule."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.rule_engine import conditions_match
from core.logging_utils import JsonLogger

def test_clearscore_rule_fixed():
    """Test the fixed rule with actual FROM header values from cache."""

    print("=" * 80)
    print("TESTING FIXED CLEARSCORE RULE")
    print("=" * 80)

    # The fixed rule conditions
    conditions = {
        "all": [
            {
                "header": "from",
                "contains": "@clearscore.com"
            },
            {
                "header": "from",
                "not_contains": "updates@clearscore.com"
            },
            {
                "header": "from",
                "not_contains": "alerts@clearscore.com"
            }
        ]
    }

    # Test with actual FROM values that exist in the cache
    test_cases = [
        ('ClearScore <marketing@clearscore.com>', True, "Marketing with display name"),
        ('"ClearScore" <marketing@clearscore.com>', True, "Marketing with quoted display name"),
        ('Clearscore <marketing@clearscore.com>', True, "Marketing simple display name"),
        ('"ClearScore" <updates@clearscore.com>', False, "Updates with display name (EXCLUDED)"),
        ('"ClearScore" <alerts@clearscore.com>', False, "Alerts with display name (EXCLUDED)"),
        ('"ClearScore" <default@clearscore.com>', True, "Default with display name"),
        ('"Justin Basini, CEO & Co-founder of ClearScore" <marketing@clearscore.com>', True, "Marketing with long display name"),
        ('ClearScore <default@clearscore.com>', True, "Default simple display name"),
    ]

    print("\nConditions: All messages must:")
    print("  1. Contain '@clearscore.com'")
    print("  2. NOT contain 'updates@clearscore.com'")
    print("  3. NOT contain 'alerts@clearscore.com'\n")

    all_passed = True
    for from_header, expected, description in test_cases:
        header = {"from": from_header}
        result = conditions_match(header, conditions)
        status = "✓" if result == expected else "✗"
        print(f"{status} {description}")
        print(f"   FROM: {from_header}")
        print(f"   Expected: {expected}, Got: {result}")
        if result != expected:
            all_passed = False
            print("   ❌ FAILED!")

    print("\n" + "=" * 80)
    if all_passed:
        print("✅ All tests passed! The rule is now working correctly.")
        print("\nThe issue was: not_equals does exact matching, but FROM headers")
        print("include display names like '\"ClearScore\" <updates@clearscore.com>'")
        print("\nThe fix: Changed not_equals to not_contains, which matches the")
        print("substring and works with any display name format.")
        return 0
    else:
        print("❌ Some tests failed!")
        return 1


if __name__ == "__main__":
    sys.exit(test_clearscore_rule_fixed())
