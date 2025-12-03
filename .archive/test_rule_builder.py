#!/usr/bin/env python3
"""Test script for RuleBuilder and file generation functions."""

from pathlib import Path
from core.tools.rule_wizard_core import RuleBuilder, slugify, generate_filename, save_rule


def test_slugify():
    """Test the slugify function."""
    print("Testing slugify()...")

    test_cases = [
        ("Banking » NatWest", "banking_natwest"),
        ("Newsletters/Reddit", "newsletters_reddit"),
        ("Events » SoulCycle [Cancelled]", "events_soulcycle_cancelled"),
        ("The Economist", "the_economist"),
        ("Notifications >> Thortful", "notifications_thortful"),
    ]

    for input_val, expected in test_cases:
        result = slugify(input_val)
        status = "✓" if result == expected else "✗"
        print(f"  {status} slugify('{input_val}') = '{result}' (expected: '{expected}')")

    print()


def test_rule_builder():
    """Test the RuleBuilder class."""
    print("Testing RuleBuilder...")

    # Test 1: Simple rule with one condition
    print("\n  Test 1: Single condition rule")
    builder = RuleBuilder()
    builder.set_name("Test » Simple")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "test@example.com")
    builder.set_action("move", "Test/Simple")
    builder.add_comment("Test rule with single condition")

    valid, msg = builder.validate()
    print(f"    Valid: {valid}, Message: {msg}")

    if valid:
        rule = builder.generate_rule()
        print(f"    Generated rule: {rule}")

    # Test 2: Multiple conditions with "any" logic
    print("\n  Test 2: Multiple conditions (any)")
    builder2 = RuleBuilder()
    builder2.set_name("Newsletters » Reddit")
    builder2.set_priority(100)
    builder2.add_condition("from", "contains", "noreply@redditmail.com")
    builder2.add_condition("from", "contains", "community@reddit.com")
    builder2.set_logic("any")
    builder2.set_action("move", "Newsletters/Reddit")
    builder2.add_comment("Created with rule wizard")

    valid2, msg2 = builder2.validate()
    print(f"    Valid: {valid2}, Message: {msg2}")

    if valid2:
        rule2 = builder2.generate_rule()
        print(f"    Generated rule name: {rule2['name']}")
        print(f"    Conditions logic: {list(rule2['conditions'].keys())[0]}")
        print(f"    Number of conditions: {len(rule2['conditions']['any'])}")

    # Test 3: Multiple conditions with "all" logic
    print("\n  Test 3: Multiple conditions (all)")
    builder3 = RuleBuilder()
    builder3.set_name("Events » SoulCycle [Cancelled]")
    builder3.set_priority(100)
    builder3.add_condition("subject", "contains", "Your SoulCycle class has been cancelled")
    builder3.add_condition("from", "contains", "@mg.soul-cycle.com")
    builder3.set_logic("all")
    builder3.set_action("move", "Events/SoulCycle")

    valid3, msg3 = builder3.validate()
    print(f"    Valid: {valid3}, Message: {msg3}")

    if valid3:
        rule3 = builder3.generate_rule()
        print(f"    Generated rule name: {rule3['name']}")
        print(f"    Conditions logic: {list(rule3['conditions'].keys())[0]}")

    # Test 4: Regex condition
    print("\n  Test 4: Regex condition")
    builder4 = RuleBuilder()
    builder4.set_name("Server » Outrigger")
    builder4.set_priority(100)
    builder4.add_condition("from", "regex", "@server\\.outrigger\\.uk$")
    builder4.set_action("move", "Server/Outrigger")

    valid4, msg4 = builder4.validate()
    print(f"    Valid: {valid4}, Message: {msg4}")

    if valid4:
        rule4 = builder4.generate_rule()
        print(f"    Generated rule: {rule4}")

    # Test 5: Invalid rule (missing name)
    print("\n  Test 5: Invalid rule (missing name)")
    builder5 = RuleBuilder()
    builder5.add_condition("from", "contains", "test@example.com")
    builder5.set_action("move", "Test")

    valid5, msg5 = builder5.validate()
    print(f"    Valid: {valid5}, Message: {msg5}")

    print()


def test_generate_filename():
    """Test the generate_filename function."""
    print("Testing generate_filename()...")

    rules_dir = Path("/root/imapfilter/rules")

    if rules_dir.exists():
        # Test with actual rules directory
        filename = generate_filename("Test » New Rule", rules_dir)
        print(f"  Generated filename: {filename.name}")
        print(f"  Full path: {filename}")

        # Check that it follows the pattern
        stem = filename.stem
        parts = stem.split("_", 1)
        if len(parts) == 2:
            numeric_part, slug_part = parts
            print(f"    Numeric ID: {numeric_part}")
            print(f"    Slug: {slug_part}")
            print(f"    ID is 5 digits: {len(numeric_part) == 5 and numeric_part.isdigit()}")
    else:
        print(f"  Rules directory not found: {rules_dir}")

    print()


def test_save_rule():
    """Test the save_rule function."""
    print("Testing save_rule()...")

    # Create a test rule
    builder = RuleBuilder()
    builder.set_name("Test » Rule Builder Test")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "test@rulebuilder.com")
    builder.set_action("move", "Test/RuleBuilder")
    builder.add_comment("Test rule created by test_rule_builder.py")
    builder.add_comment("This can be safely deleted")

    rule = builder.generate_rule()

    # Test save to test directory
    test_dir = Path("/tmp/test_rules")
    success, msg = save_rule(rule, test_dir)

    print(f"  Save result: {success}")
    print(f"  Message: {msg}")

    if success:
        # Verify the file exists and can be read
        import json
        filepath = Path(msg.replace("Saved to ", ""))
        if filepath.exists():
            with open(filepath, "r") as f:
                loaded_rule = json.load(f)
            print(f"  File exists: ✓")
            print(f"  Rule name matches: {loaded_rule['name'] == rule['name']}")
            print(f"  Rule has conditions: {'conditions' in loaded_rule}")
            print(f"  Rule has action: {'action' in loaded_rule}")

    print()


if __name__ == "__main__":
    print("=" * 60)
    print("RuleBuilder and File Generation Tests")
    print("=" * 60)
    print()

    test_slugify()
    test_rule_builder()
    test_generate_filename()
    test_save_rule()

    print("=" * 60)
    print("Tests completed!")
    print("=" * 60)
