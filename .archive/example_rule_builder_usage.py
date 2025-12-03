#!/usr/bin/env python3
"""
Example usage of RuleBuilder for creating IMAPFilter rules.

This script demonstrates how to use the RuleBuilder class to create
various types of email filtering rules.
"""

from pathlib import Path
from core.tools.rule_wizard_core import RuleBuilder, save_rule


def example_1_simple_newsletter():
    """Example 1: Simple newsletter rule with multiple sender addresses."""
    print("=" * 70)
    print("Example 1: Newsletter Rule (Multiple 'contains' conditions)")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Newsletters » Reddit")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "noreply@redditmail.com")
    builder.add_condition("from", "contains", "community@reddit.com")
    builder.add_condition("from", "contains", "notifications@reddit.com")
    builder.set_logic("any")  # Match if ANY condition is true
    builder.set_action("move", "Newsletters/Reddit")
    builder.add_comment("Created with rule wizard")
    builder.add_comment("Reddit newsletters and notifications")

    # Validate and generate
    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule validated successfully")
        print(f"Rule name: {rule['name']}")
        print(f"Conditions: {len(rule['conditions']['any'])} 'any' conditions")
        print(f"Action: Move to '{rule['action']['target']}'")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_2_banking_with_domain():
    """Example 2: Banking rule with full addresses and domain patterns."""
    print("\n" + "=" * 70)
    print("Example 2: Banking Rule (Mixed exact and domain patterns)")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Banking » NatWest")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "info@notifications.natwest.com")
    builder.add_condition("from", "contains", "alerts@natwest.com")
    builder.add_condition("from", "contains", "natwest.com")  # Broader pattern
    builder.set_logic("any")
    builder.set_action("move", "Banking/NatWest")
    builder.add_comment("NatWest banking notifications and alerts")

    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule validated successfully")
        print(f"Rule name: {rule['name']}")
        print(f"Action: {rule['action']['type']} to '{rule['action']['target']}'")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_3_event_with_all_logic():
    """Example 3: Event rule requiring ALL conditions to match."""
    print("\n" + "=" * 70)
    print("Example 3: Event Rule (ALL conditions must match)")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Events » SoulCycle [Cancelled]")
    builder.set_priority(100)
    builder.add_condition("subject", "contains", "Your SoulCycle class has been cancelled")
    builder.add_condition("from", "contains", "@mg.soul-cycle.com")
    builder.set_logic("all")  # Must match BOTH conditions
    builder.set_action("move", "Events/SoulCycle")
    builder.add_comment("Cancelled class notifications")
    builder.add_comment("Requires both subject and sender to match")

    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule validated successfully")
        print(f"Rule name: {rule['name']}")
        print(f"Conditions: {len(rule['conditions']['all'])} 'all' conditions (AND logic)")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_4_regex_patterns():
    """Example 4: Using regex patterns for advanced matching."""
    print("\n" + "=" * 70)
    print("Example 4: Server Notifications (Using regex patterns)")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Server » Outrigger")
    builder.set_priority(100)
    builder.add_condition("from", "regex", r"@server\.outrigger\.uk$")
    builder.add_condition("from", "regex", r"^noreply@.*\.outrigger\.uk$")
    builder.set_logic("any")
    builder.set_action("move", "Server/Outrigger")
    builder.add_comment("Server notifications with regex matching")

    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule validated successfully")
        print(f"Rule name: {rule['name']}")
        print("Using regex patterns for flexible domain matching")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_5_mixed_conditions():
    """Example 5: Mixing contains and regex conditions."""
    print("\n" + "=" * 70)
    print("Example 5: Travel Booking (Mixed contains and regex)")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Travel » Booking Confirmations")
    builder.set_priority(100)
    # Exact addresses
    builder.add_condition("from", "contains", "noreply@booking.com")
    builder.add_condition("from", "contains", "reservations@hotels.com")
    # Regex for multiple TLDs
    builder.add_condition("from", "regex", r"@booking\.(com|co\.uk|fr)$")
    # Subject patterns
    builder.add_condition("subject", "contains", "Booking Confirmation")
    builder.add_condition("subject", "contains", "Reservation Confirmed")
    builder.set_logic("any")
    builder.set_action("move", "Travel/Bookings")
    builder.add_comment("Travel booking confirmations from various providers")

    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule validated successfully")
        print(f"Rule name: {rule['name']}")
        print(f"Conditions: {len(rule['conditions']['any'])} mixed conditions")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_6_save_to_file():
    """Example 6: Creating and saving a rule to file."""
    print("\n" + "=" * 70)
    print("Example 6: Creating and Saving Rule to File")
    print("=" * 70)

    builder = RuleBuilder()
    builder.set_name("Finance » PayPal")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "service@paypal.com")
    builder.add_condition("from", "contains", "@paypal.com")
    builder.set_logic("any")
    builder.set_action("move", "Finance/PayPal")
    builder.add_comment("PayPal transaction notifications")
    builder.add_comment("Created: 2025-11-30")

    rule = builder.generate_rule()

    # Save to test directory
    test_dir = Path("/tmp/example_rules")
    success, msg = save_rule(rule, test_dir)

    if success:
        print(f"✓ Rule saved successfully")
        print(f"  {msg}")
        return rule
    else:
        print(f"✗ Save failed: {msg}")
        return None


def example_7_method_chaining():
    """Example 7: Using method chaining for concise rule building."""
    print("\n" + "=" * 70)
    print("Example 7: Method Chaining (Fluent Interface)")
    print("=" * 70)

    # All methods return self, allowing chaining
    builder = (
        RuleBuilder()
        .set_name("Receipts » Amazon")
        .set_priority(101)  # Higher priority
        .add_condition("from", "contains", "order-update@amazon.com")
        .add_condition("from", "contains", "shipment-tracking@amazon.com")
        .add_condition("subject", "contains", "Your Amazon.com order")
        .set_logic("any")
        .set_action("move", "Receipts/Amazon")
        .add_comment("Amazon order confirmations and shipping updates")
    )

    valid, msg = builder.validate()
    if valid:
        rule = builder.generate_rule()
        print("✓ Rule created using method chaining")
        print(f"Rule name: {rule['name']}")
        print(f"Priority: {rule['priority']}")
        return rule
    else:
        print(f"✗ Validation failed: {msg}")
        return None


def example_8_validation_errors():
    """Example 8: Demonstrating validation error handling."""
    print("\n" + "=" * 70)
    print("Example 8: Validation Error Handling")
    print("=" * 70)

    # Test 1: Missing name
    print("\nTest 1: Missing name")
    builder1 = RuleBuilder()
    builder1.add_condition("from", "contains", "test@example.com")
    builder1.set_action("move", "Test")
    valid1, msg1 = builder1.validate()
    print(f"  Valid: {valid1}, Error: {msg1}")

    # Test 2: Missing conditions
    print("\nTest 2: Missing conditions")
    builder2 = RuleBuilder()
    builder2.set_name("Test Rule")
    builder2.set_action("move", "Test")
    valid2, msg2 = builder2.validate()
    print(f"  Valid: {valid2}, Error: {msg2}")

    # Test 3: Missing action
    print("\nTest 3: Missing action")
    builder3 = RuleBuilder()
    builder3.set_name("Test Rule")
    builder3.add_condition("from", "contains", "test@example.com")
    valid3, msg3 = builder3.validate()
    print(f"  Valid: {valid3}, Error: {msg3}")

    # Test 4: Invalid action type
    print("\nTest 4: Invalid action type")
    builder4 = RuleBuilder()
    builder4.set_name("Test Rule")
    builder4.add_condition("from", "contains", "test@example.com")
    builder4.set_action("delete", "")  # 'delete' not supported yet
    valid4, msg4 = builder4.validate()
    print(f"  Valid: {valid4}, Error: {msg4}")

    print("\nValidation tests completed")


def main():
    """Run all examples."""
    print("\n")
    print("*" * 70)
    print("*" + " " * 68 + "*")
    print("*" + " IMAPFilter RuleBuilder - Usage Examples".center(68) + "*")
    print("*" + " " * 68 + "*")
    print("*" * 70)
    print()

    # Run examples
    example_1_simple_newsletter()
    example_2_banking_with_domain()
    example_3_event_with_all_logic()
    example_4_regex_patterns()
    example_5_mixed_conditions()
    example_6_save_to_file()
    example_7_method_chaining()
    example_8_validation_errors()

    print("\n" + "=" * 70)
    print("All examples completed!")
    print("=" * 70)
    print()


if __name__ == "__main__":
    main()
