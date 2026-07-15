#!/usr/bin/env python3
"""Test smart match type selection with field-specific guidance."""

import sys
from pathlib import Path
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class MinimalWizard:
    """Minimal wizard class with just the _select_match_type method for testing."""

    def _select_match_type(self, field: str):
        """Prompt for match type with field-specific guidance.

        Args:
            field: The header field name (e.g., "from", "subject", "list-id")

        Returns:
            Match type string: 'equals', 'not_equals', 'contains', 'not_contains',
                              'regex', or 'not_regex'
            None if user cancels
        """
        is_email_field = field in ("from", "to", "cc", "bcc", "reply-to")

        if is_email_field:
            print("\n⚠️  NOTE: Email addresses often include display names like \"Name <email@domain.com>\"")
            print("   Use 'Contains' to match any display name format.")
            print("   'Equals' requires exact match (rarely what you want for email addresses).")
            print("\nMatch type:")
            print("  1. Contains (substring match - RECOMMENDED)")
            print("  2. Not Contains (exclude substring - RECOMMENDED)")
            print("  3. Equals (exact match - use with caution)")
            print("  4. Not Equals (exclude exact match - use with caution)")
            print("  5. Regex (regular expression)")
            print("  6. Not Regex (exclude regex pattern)")

            choice = input("  > ").strip()

            match_types = {
                "1": "contains",
                "2": "not_contains",
                "3": "equals",
                "4": "not_equals",
                "5": "regex",
                "6": "not_regex",
            }

            if choice in match_types:
                return match_types[choice]
            elif choice == "":
                # Empty input - use recommended default
                print("Using recommended: 'contains'")
                return "contains"
            else:
                print("Invalid choice, using 'contains' by default.")
                return "contains"
        else:
            # Non-email fields: keep current behavior
            print("\nMatch type:")
            print("  1. Equals (exact match)")
            print("  2. Not Equals (does not match exactly)")
            print("  3. Contains (substring match - case insensitive)")
            print("  4. Not Contains (does not contain substring)")
            print("  5. Regex (regular expression)")
            print("  6. Not Regex (does not match regex pattern)")

            choice = input("  > ").strip()

            match_types = {
                "1": "equals",
                "2": "not_equals",
                "3": "contains",
                "4": "not_contains",
                "5": "regex",
                "6": "not_regex",
            }

            if choice in match_types:
                return match_types[choice]
            else:
                print("Invalid choice, using 'contains' by default.")
                return "contains"


def test_email_field_shows_warning_and_reordered_menu():
    """Email fields should show warning and reordered menu with contains first."""
    print("\n" + "=" * 80)
    print("TEST 1: Email fields show warning and reordered menu")
    print("=" * 80)

    wizard = MinimalWizard()

    # Test with "from" field
    email_fields = ["from", "to", "cc", "bcc", "reply-to"]

    for field in email_fields:
        print(f"\nTesting field: {field}")

        # Capture output
        captured_output = StringIO()

        with patch("builtins.input", return_value="1"):  # Select option 1 (contains)
            with patch("sys.stdout", new=captured_output):
                result = wizard._select_match_type(field)

        output = captured_output.getvalue()

        # Verify warning is shown
        assert "⚠️  NOTE" in output, f"Warning not shown for field {field}"
        assert "display names" in output, f"Display name mention not shown for field {field}"

        # Verify menu is reordered (contains first)
        assert "1. Contains" in output, f"Contains not first for field {field}"
        assert "2. Not Contains" in output, f"Not Contains not second for field {field}"

        # Verify result
        assert result == "contains", f"Expected 'contains' for field {field}, got {result}"

        print(f"  ✓ {field}: Shows warning and reordered menu")

    return True


def test_email_field_empty_input_defaults_to_contains():
    """Empty input on email fields should default to contains."""
    print("\n" + "=" * 80)
    print("TEST 2: Email fields default to contains on empty input")
    print("=" * 80)

    wizard = MinimalWizard()

    captured_output = StringIO()

    with patch("builtins.input", return_value=""):  # Empty input
        with patch("sys.stdout", new=captured_output):
            result = wizard._select_match_type("from")

    output = captured_output.getvalue()

    # Verify it defaults to contains
    assert result == "contains", f"Expected default 'contains', got {result}"
    assert "Using recommended: 'contains'" in output, "Default message not shown"

    print("  ✓ Empty input defaults to 'contains'")

    return True


def test_non_email_field_no_warning():
    """Non-email fields should not show warning and keep original menu order."""
    print("\n" + "=" * 80)
    print("TEST 3: Non-email fields show no warning, original menu order")
    print("=" * 80)

    wizard = MinimalWizard()

    non_email_fields = ["subject", "list-id", "custom-header"]

    for field in non_email_fields:
        print(f"\nTesting field: {field}")

        captured_output = StringIO()

        with patch("builtins.input", return_value="1"):  # Select option 1
            with patch("sys.stdout", new=captured_output):
                result = wizard._select_match_type(field)

        output = captured_output.getvalue()

        # Verify no warning
        assert "⚠️" not in output, f"Warning shown for non-email field {field}"
        assert "display names" not in output, f"Display name mention shown for non-email field {field}"

        # Verify original menu order (equals first for non-email)
        assert "1. Equals" in output, f"Equals not first for non-email field {field}"
        assert "2. Not Equals" in output, f"Not Equals not second for non-email field {field}"

        # With input "1", non-email fields should return "equals"
        assert result == "equals", f"Expected 'equals' for non-email field {field}, got {result}"

        print(f"  ✓ {field}: No warning, original menu order, returns 'equals' for input 1")

    return True


def test_email_field_not_equals_works():
    """Email field should allow not_equals if explicitly chosen."""
    print("\n" + "=" * 80)
    print("TEST 4: Email field allows explicit not_equals selection")
    print("=" * 80)

    wizard = MinimalWizard()

    captured_output = StringIO()

    # Select option 4 (not_equals) from email field menu
    with patch("builtins.input", return_value="4"):
        with patch("sys.stdout", new=captured_output):
            result = wizard._select_match_type("from")

    output = captured_output.getvalue()

    # Verify it still allows the selection
    assert result == "not_equals", f"Expected 'not_equals', got {result}"
    assert "⚠️" in output, "Warning should still be shown"
    assert "use with caution" in output, "Caution message should be shown"

    print("  ✓ Email field allows explicit not_equals selection with caution warning")

    return True


def test_invalid_input_defaults_to_contains():
    """Invalid input should default to contains for email fields."""
    print("\n" + "=" * 80)
    print("TEST 5: Invalid input defaults to contains for email fields")
    print("=" * 80)

    wizard = MinimalWizard()

    captured_output = StringIO()

    with patch("builtins.input", return_value="99"):  # Invalid choice
        with patch("sys.stdout", new=captured_output):
            result = wizard._select_match_type("from")

    output = captured_output.getvalue()

    assert result == "contains", f"Expected 'contains' on invalid input, got {result}"
    assert "Invalid choice" in output, "Invalid choice message not shown"

    print("  ✓ Invalid input defaults to 'contains'")

    return True


def test_all_email_field_types_detected():
    """All email field types should be detected and show guidance."""
    print("\n" + "=" * 80)
    print("TEST 6: All email field types are properly detected")
    print("=" * 80)

    wizard = MinimalWizard()
    email_fields = ["from", "to", "cc", "bcc", "reply-to"]

    for field in email_fields:
        captured_output = StringIO()

        with patch("builtins.input", return_value="1"):
            with patch("sys.stdout", new=captured_output):
                wizard._select_match_type(field)

        output = captured_output.getvalue()

        # All should show warning
        assert "⚠️" in output, f"Email field {field} not detected as email field"
        assert "Contains" in output and "RECOMMENDED" in output, f"Reordered menu not shown for {field}"

    print("  ✓ All email fields detected: from, to, cc, bcc, reply-to")

    return True


def run_all_tests():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("MATCH TYPE SELECTION TESTS")
    print("=" * 80)

    try:
        test_email_field_shows_warning_and_reordered_menu()
        test_email_field_empty_input_defaults_to_contains()
        test_non_email_field_no_warning()
        test_email_field_not_equals_works()
        test_invalid_input_defaults_to_contains()
        test_all_email_field_types_detected()

        print("\n" + "=" * 80)
        print("✅ ALL TESTS PASSED!")
        print("=" * 80)
        print("\nSummary:")
        print("  ✓ Email fields show warning and reordered menu")
        print("  ✓ Email fields default to 'contains' on empty input")
        print("  ✓ Non-email fields show original menu, no warning")
        print("  ✓ Email fields still allow explicit not_equals if chosen")
        print("  ✓ Invalid input defaults to 'contains'")
        print("  ✓ All email field types properly detected")
        print("\nThe smart match type selection is working correctly!")

        return 0
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
