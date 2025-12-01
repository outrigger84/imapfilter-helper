#!/usr/bin/env python3
"""Test script for KeywordConfig implementation."""

import sys
from pathlib import Path

# Add core to path
sys.path.insert(0, str(Path(__file__).parent))

from core.config import KeywordConfig, PathsConfig, build_default_config


def test_config_json_exists():
    """Test that config.json exists."""
    config_path = Path("/root/imapfilter/data/config.json")
    assert config_path.exists(), "config.json does not exist"
    print("✓ config.json exists")


def test_load_from_file():
    """Test loading configuration from file."""
    config_path = Path("/root/imapfilter/data/config.json")
    kw_config = KeywordConfig.load_from_file(config_path)

    assert kw_config is not None, "Failed to load config"
    assert len(kw_config.predefined_keywords) > 0, "No keywords loaded"
    assert len(kw_config.age_presets) > 0, "No age presets loaded"

    print(f"✓ Loaded {len(kw_config.predefined_keywords)} keywords")
    print(f"✓ Loaded {len(kw_config.age_presets)} age presets")

    return kw_config


def test_get_system_flags(kw_config):
    """Test getting system flags."""
    system_flags = kw_config.get_system_flags()

    assert len(system_flags) > 0, "No system flags found"
    assert all(flag.startswith("\\") for flag in system_flags), "Not all flags start with backslash"

    expected_flags = ["\\Seen", "\\Flagged", "\\Answered", "\\Deleted", "\\Draft"]
    for flag in expected_flags:
        assert flag in system_flags, f"Missing expected flag: {flag}"

    print(f"✓ Found {len(system_flags)} system flags:")
    for flag in system_flags:
        print(f"  - {flag}")


def test_get_custom_keywords(kw_config):
    """Test getting custom keywords."""
    custom_keywords = kw_config.get_custom_keywords()

    assert len(custom_keywords) > 0, "No custom keywords found"
    assert not any(kw.startswith("\\") for kw in custom_keywords), "System flag in custom keywords"

    expected_keywords = ["newsletter", "work", "receipts"]
    for keyword in expected_keywords:
        assert keyword in custom_keywords, f"Missing expected keyword: {keyword}"

    print(f"✓ Found {len(custom_keywords)} custom keywords:")
    for keyword in custom_keywords:
        print(f"  - {keyword}")


def test_get_all_keywords(kw_config):
    """Test getting all keywords."""
    all_keywords = kw_config.get_all_keywords()

    assert len(all_keywords) > 0, "No keywords found"
    assert len(all_keywords) == len(kw_config.predefined_keywords), "Mismatch in keyword count"

    print(f"✓ Found {len(all_keywords)} total keywords")


def test_validate_keyword(kw_config):
    """Test keyword validation."""
    # Valid custom keywords
    test_cases = [
        # (keyword, should_be_valid, description)
        ("newsletter", True, "valid custom keyword"),
        ("my-keyword", True, "custom keyword with hyphen"),
        ("my_keyword", True, "custom keyword with underscore"),
        ("keyword123", True, "custom keyword with numbers"),
        (r"\Seen", True, "valid system flag"),
        (r"\Flagged", True, "valid system flag"),
        ("", False, "empty keyword"),
        ("my keyword", False, "keyword with space"),
        (r"\InvalidFlag", False, "invalid system flag"),
        ("my@keyword", False, "keyword with special char"),
    ]

    print("✓ Testing keyword validation:")
    for keyword, should_be_valid, description in test_cases:
        is_valid, error_msg = kw_config.validate_keyword(keyword)

        if should_be_valid:
            assert is_valid, f"Expected '{keyword}' to be valid ({description}), but got error: {error_msg}"
            print(f"  ✓ '{keyword}' - {description}")
        else:
            assert not is_valid, f"Expected '{keyword}' to be invalid ({description})"
            print(f"  ✓ '{keyword}' correctly rejected - {description}")


def test_paths_config_integration():
    """Test that PathsConfig includes config_file."""
    app_config = build_default_config()

    assert hasattr(app_config.paths, "config_file"), "PathsConfig missing config_file attribute"
    assert app_config.paths.config_file.name == "config.json", "config_file has wrong name"

    print(f"✓ PathsConfig includes config_file: {app_config.paths.config_file}")


def test_fallback_to_defaults():
    """Test fallback to defaults when file doesn't exist."""
    nonexistent_path = Path("/root/imapfilter/data/nonexistent.json")
    kw_config = KeywordConfig.load_from_file(nonexistent_path)

    assert kw_config is not None, "Failed to create default config"
    assert len(kw_config.predefined_keywords) > 0, "No default keywords"
    assert len(kw_config.age_presets) > 0, "No default age presets"

    print("✓ Fallback to defaults works correctly")


def test_age_presets(kw_config):
    """Test age presets are loaded correctly."""
    age_presets = kw_config.age_presets

    assert len(age_presets) > 0, "No age presets found"

    # Verify structure
    for preset in age_presets:
        assert "label" in preset, "Age preset missing 'label'"
        assert "days" in preset, "Age preset missing 'days'"
        assert isinstance(preset["days"], int), "Days should be integer"

    print(f"✓ Found {len(age_presets)} age presets:")
    for preset in age_presets:
        print(f"  - {preset['label']}: {preset['days']} days")


def main():
    """Run all tests."""
    print("=" * 60)
    print("Testing KeywordConfig Implementation")
    print("=" * 60)
    print()

    try:
        # Test 1: Check config.json exists
        test_config_json_exists()
        print()

        # Test 2: Load from file
        kw_config = test_load_from_file()
        print()

        # Test 3: Get system flags
        test_get_system_flags(kw_config)
        print()

        # Test 4: Get custom keywords
        test_get_custom_keywords(kw_config)
        print()

        # Test 5: Get all keywords
        test_get_all_keywords(kw_config)
        print()

        # Test 6: Validate keywords
        test_validate_keyword(kw_config)
        print()

        # Test 7: Age presets
        test_age_presets(kw_config)
        print()

        # Test 8: PathsConfig integration
        test_paths_config_integration()
        print()

        # Test 9: Fallback to defaults
        test_fallback_to_defaults()
        print()

        print("=" * 60)
        print("✓ All tests passed successfully!")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
