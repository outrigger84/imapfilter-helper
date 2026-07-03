#!/usr/bin/env python3
"""Test to reproduce the clearscore.com rule evaluation issue."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.rule_engine import conditions_match, find_matching_rule, _evaluate_condition_node


def test_clearscore_rule_evaluation():
    """Test the clearscore rule with not_equals conditions."""

    # The user's actual rule
    rule = {
        "name": "Newsletters » Clearscore",
        "priority": 100,
        "conditions": {
            "all": [
                {
                    "header": "from",
                    "contains": "@clearscore.com"
                },
                {
                    "header": "from",
                    "not_equals": "updates@clearscore.com"
                },
                {
                    "header": "from",
                    "not_equals": "alerts@clearscore.com"
                }
            ]
        },
        "actions": [
            {
                "type": "move",
                "target": "Newsletters/Clearscore"
            }
        ]
    }

    # Test cases from the user's list
    test_cases = [
        ("marketing@clearscore.com", True, "Should match - contains @clearscore.com and not in exclusions"),
        ("default@clearscore.com", True, "Should match - contains @clearscore.com and not in exclusions"),
        ("updates@clearscore.com", False, "Should NOT match - explicitly excluded"),
        ("alerts@clearscore.com", False, "Should NOT match - explicitly excluded"),
        ("other@clearscore.com", True, "Should match - contains @clearscore.com and not in exclusions"),
    ]

    print("=" * 80)
    print("Testing Clearscore Rule Evaluation")
    print("=" * 80)
    print(f"\nRule: {rule['name']}")
    print(f"Priority: {rule['priority']}")
    print(f"Conditions: {rule['conditions']}\n")

    all_passed = True
    for email, expected_match, explanation in test_cases:
        header = {"from": email}
        result = conditions_match(header, rule["conditions"])

        status = "✅" if result == expected_match else "❌"
        print(f"{status} {email:<35} Expected: {expected_match:<5} Got: {result:<5}")
        print(f"   └─ {explanation}")

        if result != expected_match:
            all_passed = False

    print("\n" + "=" * 80)

    # Now test the full find_matching_rule with the rule
    print("\nTesting find_matching_rule() (used by coverage analyzer):")
    print("-" * 80)

    for email, expected_match, explanation in test_cases:
        header = {"from": email}
        matching = find_matching_rule(header, [rule])

        found = matching is not None
        status = "✅" if found == expected_match else "❌"
        print(f"{status} {email:<35} Should find rule: {expected_match:<5} Found: {found:<5}")

        if found != expected_match:
            all_passed = False

    print("\n" + "=" * 80)
    if all_passed:
        print("✅ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed - this indicates a bug in rule evaluation!")
        return 1


if __name__ == "__main__":
    sys.exit(test_clearscore_rule_evaluation())
