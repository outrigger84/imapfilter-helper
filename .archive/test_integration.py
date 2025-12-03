#!/usr/bin/env python3
"""
Integration test for RuleBuilder workflow.

This demonstrates a complete workflow from building to saving rules.
"""

import json
from pathlib import Path
from core.tools.rule_wizard_core import (
    RuleBuilder,
    slugify,
    generate_filename,
    save_rule
)


def test_complete_workflow():
    """Test complete workflow: build -> validate -> generate -> save -> verify."""
    print("=" * 70)
    print("Integration Test: Complete Workflow")
    print("=" * 70)
    print()

    # Step 1: Create rule using builder
    print("Step 1: Building rule...")
    builder = RuleBuilder()
    builder.set_name("Integration Test » Workflow")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "test@workflow.com")
    builder.add_condition("subject", "contains", "Integration Test")
    builder.set_logic("all")
    builder.set_action("move", "Test/Integration")
    builder.add_comment("Created by integration test")
    builder.add_comment("Tests complete workflow from build to save")
    print("  ✓ Rule built")

    # Step 2: Validate rule
    print("\nStep 2: Validating rule...")
    valid, msg = builder.validate()
    if not valid:
        print(f"  ✗ Validation failed: {msg}")
        return False
    print("  ✓ Rule validated successfully")

    # Step 3: Generate rule dictionary
    print("\nStep 3: Generating rule dictionary...")
    try:
        rule = builder.generate_rule()
        print("  ✓ Rule generated")
        print(f"    - Name: {rule['name']}")
        print(f"    - Priority: {rule['priority']}")
        print(f"    - Conditions: {list(rule['conditions'].keys())[0]} "
              f"({len(rule['conditions']['all'])} conditions)")
        print(f"    - Action: {rule['action']['type']} → {rule['action']['target']}")
        print(f"    - Comments: {len(rule['comments'])} comments")
    except ValueError as e:
        print(f"  ✗ Generation failed: {e}")
        return False

    # Step 4: Test slugify
    print("\nStep 4: Testing slugify function...")
    slug = slugify(rule['name'])
    print(f"  ✓ Slugified name: '{rule['name']}' → '{slug}'")

    # Step 5: Test filename generation
    print("\nStep 5: Testing filename generation...")
    test_dir = Path("/tmp/integration_test_rules")
    filepath = generate_filename(rule['name'], test_dir)
    print(f"  ✓ Generated filename: {filepath.name}")
    print(f"    - Numeric ID: {filepath.stem.split('_')[0]}")
    print(f"    - Slug: {'_'.join(filepath.stem.split('_')[1:])}")

    # Step 6: Save rule
    print("\nStep 6: Saving rule to file...")
    success, save_msg = save_rule(rule, test_dir)
    if not success:
        print(f"  ✗ Save failed: {save_msg}")
        return False
    print(f"  ✓ {save_msg}")

    # Step 7: Verify saved file
    print("\nStep 7: Verifying saved file...")
    saved_file = Path(save_msg.replace("Saved to ", ""))
    if not saved_file.exists():
        print(f"  ✗ File not found: {saved_file}")
        return False
    print(f"  ✓ File exists: {saved_file}")

    # Step 8: Load and verify content
    print("\nStep 8: Loading and verifying file content...")
    try:
        with open(saved_file, "r", encoding="utf-8") as f:
            loaded_rule = json.load(f)

        # Verify all fields
        checks = [
            ("name", rule['name'] == loaded_rule['name']),
            ("priority", rule['priority'] == loaded_rule['priority']),
            ("conditions", rule['conditions'] == loaded_rule['conditions']),
            ("action", rule['action'] == loaded_rule['action']),
            ("comments", rule['comments'] == loaded_rule['comments']),
        ]

        all_passed = True
        for field, passed in checks:
            status = "✓" if passed else "✗"
            print(f"  {status} {field} matches")
            if not passed:
                all_passed = False

        if all_passed:
            print("\n  ✓ All fields verified successfully")
        else:
            print("\n  ✗ Some fields did not match")
            return False

    except Exception as e:
        print(f"  ✗ Failed to load/verify: {e}")
        return False

    # Step 9: Format validation
    print("\nStep 9: Validating JSON format...")
    try:
        # Check JSON formatting
        with open(saved_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify it's pretty-printed (has indentation)
        if '  "name"' in content:
            print("  ✓ JSON is pretty-printed (indented)")
        else:
            print("  ✗ JSON is not properly indented")
            return False

        # Verify trailing newline
        if content.endswith('\n'):
            print("  ✓ File has trailing newline")
        else:
            print("  ✗ File missing trailing newline")
            return False

        # Verify key order
        keys_in_order = list(loaded_rule.keys())
        expected_order = ["name", "priority", "conditions", "action", "comments"]
        if keys_in_order == expected_order:
            print(f"  ✓ Keys in correct order: {keys_in_order}")
        else:
            print(f"  ✗ Key order incorrect")
            print(f"    Expected: {expected_order}")
            print(f"    Got: {keys_in_order}")
            return False

    except Exception as e:
        print(f"  ✗ Format validation failed: {e}")
        return False

    print("\n" + "=" * 70)
    print("Integration Test: ALL CHECKS PASSED")
    print("=" * 70)
    return True


def test_multiple_rule_creation():
    """Test creating multiple rules with incrementing IDs."""
    print("\n" + "=" * 70)
    print("Integration Test: Multiple Rule Creation")
    print("=" * 70)
    print()

    test_dir = Path("/tmp/multi_rules_test")

    # Clean up test directory
    if test_dir.exists():
        for file in test_dir.glob("*.json"):
            file.unlink()

    print("Creating 3 rules in sequence...")

    rules = [
        ("Test » Rule One", "test1@example.com"),
        ("Test » Rule Two", "test2@example.com"),
        ("Test » Rule Three", "test3@example.com"),
    ]

    created_files = []

    for name, email in rules:
        builder = RuleBuilder()
        builder.set_name(name)
        builder.set_priority(100)
        builder.add_condition("from", "contains", email)
        builder.set_action("move", "Test")

        rule = builder.generate_rule()
        success, msg = save_rule(rule, test_dir)

        if success:
            filepath = Path(msg.replace("Saved to ", ""))
            created_files.append(filepath)
            numeric_id = filepath.stem.split("_")[0]
            print(f"  ✓ Created {filepath.name} (ID: {numeric_id})")
        else:
            print(f"  ✗ Failed to create rule: {msg}")
            return False

    # Verify IDs are sequential
    print("\nVerifying sequential IDs...")
    ids = [int(f.stem.split("_")[0]) for f in created_files]
    ids_sorted = sorted(ids)

    if ids == ids_sorted:
        print("  ✓ IDs are in order")
    else:
        print("  ✗ IDs are not in order")
        return False

    # Check if IDs are sequential
    sequential = all(ids[i] + 1 == ids[i + 1] for i in range(len(ids) - 1))
    if sequential:
        print(f"  ✓ IDs are sequential: {ids}")
    else:
        print(f"  ⚠ IDs are not sequential: {ids} (this is OK)")

    print("\n" + "=" * 70)
    print("Multiple Rule Creation: PASSED")
    print("=" * 70)
    return True


def main():
    """Run all integration tests."""
    print("\n")
    print("*" * 70)
    print("*" + " RuleBuilder Integration Tests ".center(68) + "*")
    print("*" * 70)
    print()

    # Run tests
    test1_passed = test_complete_workflow()
    test2_passed = test_multiple_rule_creation()

    print("\n" + "*" * 70)
    print("*" + " Test Results ".center(68) + "*")
    print("*" * 70)
    print()
    print(f"  Complete Workflow Test: {'PASSED ✓' if test1_passed else 'FAILED ✗'}")
    print(f"  Multiple Rule Creation Test: {'PASSED ✓' if test2_passed else 'FAILED ✗'}")
    print()

    if test1_passed and test2_passed:
        print("  " + "=" * 66)
        print("  " + " ALL INTEGRATION TESTS PASSED ".center(66))
        print("  " + "=" * 66)
    else:
        print("  " + "=" * 66)
        print("  " + " SOME TESTS FAILED ".center(66))
        print("  " + "=" * 66)

    print("\n")


if __name__ == "__main__":
    main()
