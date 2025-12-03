#!/usr/bin/env python3
"""Test to verify rule format matches existing rules exactly."""

import json
from pathlib import Path
from core.tools.rule_wizard_core import RuleBuilder, save_rule


def test_format_matching():
    """Test that generated rules match the expected format."""
    print("=" * 70)
    print("Testing Rule Format Matching")
    print("=" * 70)
    print()

    # Test 1: Match the Banking » NatWest format (any with multiple conditions)
    print("Test 1: Recreating Banking » NatWest rule format")
    print("-" * 70)

    builder1 = RuleBuilder()
    builder1.set_name("Banking » NatWest")
    builder1.set_priority(100)
    builder1.add_condition("from", "contains", "info@notifications.natwest.com")
    builder1.add_condition("from", "contains", "natwest.com")
    builder1.set_logic("any")
    builder1.set_action("move", "Banking/NatWest")
    builder1.add_comment("NatWest banking notifications and alerts")

    rule1 = builder1.generate_rule()
    print(json.dumps(rule1, indent=2))
    print()

    # Test 2: Match the SoulCycle format (all with multiple conditions)
    print("Test 2: Recreating Events » SoulCycle [Cancelled] rule format")
    print("-" * 70)

    builder2 = RuleBuilder()
    builder2.set_name("Events » Soulcycle [Cancelled]")
    builder2.set_priority(100)
    builder2.add_condition("subject", "contains", "Your SoulCycle class has been cancelled.")
    builder2.add_condition("from", "contains", "@mg.soul-cycle.com")
    builder2.set_logic("all")
    builder2.set_action("move", "Events/Soulcycle")
    builder2.add_comment("Rule disabled in Thunderbird")
    builder2.add_comment("Unsupported action: Mark unread")

    rule2 = builder2.generate_rule()
    print(json.dumps(rule2, indent=2))
    print()

    # Test 3: Match regex format
    print("Test 3: Recreating regex-based rule format")
    print("-" * 70)

    builder3 = RuleBuilder()
    builder3.set_name("Newsletters » Confused.com")
    builder3.set_priority(100)
    builder3.add_condition("from", "contains", "news@newsletter.confused.com")
    builder3.add_condition("from", "regex", "@message\\.confused\\.com$")
    builder3.add_condition("from", "regex", "@offer\\.confused\\.com$")
    builder3.set_logic("any")
    builder3.set_action("move", "Newsletters/Confused.com")

    rule3 = builder3.generate_rule()
    print(json.dumps(rule3, indent=2))
    print()

    # Load actual rule for comparison
    print("Test 4: Compare field order with actual rule")
    print("-" * 70)

    actual_rule_path = Path("/root/imapfilter/rules/99007_banking_natwest.json")
    if actual_rule_path.exists():
        with open(actual_rule_path, "r") as f:
            actual_rule = json.load(f)

        print("Actual rule keys order:", list(actual_rule.keys()))
        print("Generated rule keys order:", list(rule1.keys()))
        print()

        # Check key presence
        required_keys = ["name", "priority", "conditions", "action"]
        optional_keys = ["comments"]

        print("Required keys present in generated rule:")
        for key in required_keys:
            status = "✓" if key in rule1 else "✗"
            print(f"  {status} {key}")

        print()
        print("Optional keys present in generated rule:")
        for key in optional_keys:
            status = "✓" if key in rule1 else "—"
            print(f"  {status} {key}")

        print()
        print("Conditions structure check:")
        print(f"  Actual has 'any' key: {'any' in actual_rule['conditions']}")
        print(f"  Generated has 'any' key: {'any' in rule1['conditions']}")
        print(f"  Actual conditions count: {len(actual_rule['conditions']['any'])}")
        print(f"  Generated conditions count: {len(rule1['conditions']['any'])}")

        print()
        print("Action structure check:")
        print(f"  Actual action keys: {list(actual_rule['action'].keys())}")
        print(f"  Generated action keys: {list(rule1['action'].keys())}")

    print()
    print("=" * 70)
    print("Format validation completed!")
    print("=" * 70)


if __name__ == "__main__":
    test_format_matching()
