#!/usr/bin/env python3
"""
Example showing how other phases can integrate with the KeywordConfig system.

This demonstrates the API that Phase 1 provides for other phases.
"""

from pathlib import Path
from core.config import KeywordConfig, build_default_config


def example_usage():
    """Show typical usage patterns for other phases."""

    print("=" * 70)
    print("PHASE 1 INTEGRATION EXAMPLE")
    print("=" * 70)
    print()

    # 1. Load configuration (typical usage)
    print("1. Loading Configuration")
    print("-" * 70)
    app_config = build_default_config()
    kw_config = KeywordConfig.load_from_file(app_config.paths.config_file)
    print(f"✓ Loaded from: {app_config.paths.config_file}")
    print()

    # 2. Get available keywords for UI dropdown/selection
    print("2. Getting Keywords for UI")
    print("-" * 70)
    all_keywords = kw_config.get_all_keywords()
    print(f"All keywords available: {all_keywords}")
    print()

    # 3. Separate system flags from custom keywords
    print("3. Separating System Flags from Custom Keywords")
    print("-" * 70)
    system_flags = kw_config.get_system_flags()
    custom_keywords = kw_config.get_custom_keywords()
    print(f"System flags: {system_flags}")
    print(f"Custom keywords: {custom_keywords}")
    print()

    # 4. Validate user input
    print("4. Validating User Input")
    print("-" * 70)
    test_inputs = [
        "my-new-keyword",  # Valid
        "Invalid Keyword",  # Invalid (space)
        r"\Seen",  # Valid system flag
        r"\InvalidFlag",  # Invalid system flag
    ]

    for keyword in test_inputs:
        is_valid, error_msg = kw_config.validate_keyword(keyword)
        if is_valid:
            print(f"✓ '{keyword}' is valid")
        else:
            print(f"✗ '{keyword}' is invalid: {error_msg}")
    print()

    # 5. Access age presets for UI
    print("5. Age Presets for Date Filtering")
    print("-" * 70)
    for preset in kw_config.age_presets:
        print(f"  • {preset['label']:20} = {preset['days']:4} days")
    print()

    # 6. Example: Building a keyword selector UI
    print("6. Example: Building a Keyword Selector")
    print("-" * 70)
    print("System Flags (read-only):")
    for flag in system_flags:
        matching = [k for k in kw_config.predefined_keywords if k["name"] == flag]
        if matching:
            print(f"  [ ] {flag:20} - {matching[0]['description']}")

    print("\nCustom Keywords:")
    for keyword in custom_keywords:
        matching = [k for k in kw_config.predefined_keywords if k["name"] == keyword]
        if matching:
            print(f"  [ ] {keyword:20} - {matching[0]['description']}")
    print()

    # 7. Example: Validating and storing a rule action
    print("7. Example: Validating Rule Actions")
    print("-" * 70)

    def validate_keyword_action(action: dict) -> bool:
        """Example validation function for rule actions."""
        if action.get("type") != "add_keyword":
            return True  # Only validate keyword actions

        keyword = action.get("keyword", "")
        is_valid, error_msg = kw_config.validate_keyword(keyword)

        if not is_valid:
            print(f"✗ Invalid keyword '{keyword}': {error_msg}")
            return False

        print(f"✓ Valid keyword action: {keyword}")
        return True

    # Test some rule actions
    test_actions = [
        {"type": "add_keyword", "keyword": "newsletter"},
        {"type": "add_keyword", "keyword": r"\Seen"},
        {"type": "add_keyword", "keyword": "invalid keyword"},
        {"type": "move", "folder": "Archive"},  # Non-keyword action
    ]

    for action in test_actions:
        validate_keyword_action(action)

    print()
    print("=" * 70)
    print("✓ Integration example complete")
    print("=" * 70)


if __name__ == "__main__":
    example_usage()
