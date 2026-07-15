"""Tests for KeywordConfig."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import KeywordConfig, build_default_config

CONFIG_PATH = ROOT / "data" / "config.json"


@pytest.fixture()
def kw_config() -> KeywordConfig:
    return KeywordConfig.load_from_file(CONFIG_PATH)


def test_config_json_exists():
    assert CONFIG_PATH.exists(), "config.json does not exist"


def test_load_from_file(kw_config):
    assert kw_config is not None, "Failed to load config"
    assert len(kw_config.predefined_keywords) > 0, "No keywords loaded"
    assert len(kw_config.age_presets) > 0, "No age presets loaded"


def test_get_system_flags(kw_config):
    system_flags = kw_config.get_system_flags()

    assert len(system_flags) > 0, "No system flags found"
    assert all(flag.startswith("\\") for flag in system_flags), "Not all flags start with backslash"

    expected_flags = ["\\Seen", "\\Flagged", "\\Answered", "\\Deleted", "\\Draft"]
    for flag in expected_flags:
        assert flag in system_flags, f"Missing expected flag: {flag}"


def test_get_custom_keywords(kw_config):
    custom_keywords = kw_config.get_custom_keywords()

    assert len(custom_keywords) > 0, "No custom keywords found"
    assert not any(kw.startswith("\\") for kw in custom_keywords), "System flag in custom keywords"

    expected_keywords = ["newsletter", "work", "receipts"]
    for keyword in expected_keywords:
        assert keyword in custom_keywords, f"Missing expected keyword: {keyword}"


def test_get_all_keywords(kw_config):
    all_keywords = kw_config.get_all_keywords()

    assert len(all_keywords) > 0, "No keywords found"
    assert len(all_keywords) == len(kw_config.predefined_keywords), "Mismatch in keyword count"


def test_validate_keyword(kw_config):
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

    for keyword, should_be_valid, description in test_cases:
        is_valid, error_msg = kw_config.validate_keyword(keyword)

        if should_be_valid:
            assert is_valid, f"Expected '{keyword}' to be valid ({description}), but got error: {error_msg}"
        else:
            assert not is_valid, f"Expected '{keyword}' to be invalid ({description})"


def test_paths_config_integration():
    app_config = build_default_config()

    assert hasattr(app_config.paths, "config_file"), "PathsConfig missing config_file attribute"
    assert app_config.paths.config_file.name == "config.json", "config_file has wrong name"


def test_fallback_to_defaults(tmp_path):
    kw_config = KeywordConfig.load_from_file(tmp_path / "nonexistent.json")

    assert kw_config is not None, "Failed to create default config"
    assert len(kw_config.predefined_keywords) > 0, "No default keywords"
    assert len(kw_config.age_presets) > 0, "No default age presets"


def test_age_presets(kw_config):
    age_presets = kw_config.age_presets

    assert len(age_presets) > 0, "No age presets found"

    for preset in age_presets:
        assert "label" in preset, "Age preset missing 'label'"
        assert "days" in preset, "Age preset missing 'days'"
        assert isinstance(preset["days"], int), "Days should be integer"
