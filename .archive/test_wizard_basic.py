#!/usr/bin/env python3
"""Basic test to verify RuleWizard can be instantiated."""

from core.config import build_default_config
from core.tools.rule_wizard_core import RuleWizard

# Test 1: Check that config validation works
print("Test 1: Config validation...")
try:
    config = build_default_config()
    print(f"  Config created: {config.paths.base_dir}")
    print(f"  Cache path: {config.paths.db_file}")
    print("  ✓ Config validation passed")
except Exception as e:
    print(f"  ✗ Config validation failed: {e}")

# Test 2: Check that wizard requires cache
print("\nTest 2: Cache requirement validation...")
try:
    wizard = RuleWizard(config)
    print("  ✗ Should have raised ValueError for missing cache")
except ValueError as e:
    print(f"  ✓ Correctly raised ValueError: {e}")
except Exception as e:
    print(f"  ✗ Unexpected error: {e}")

# Test 3: Check all components are initialized
print("\nTest 3: Component initialization...")
try:
    # Create a dummy cache file
    config.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
    config.paths.db_file.touch()

    wizard = RuleWizard(config)

    assert wizard.cache_engine is None, "Cache engine should be None before run()"
    assert wizard.email_extractor is not None, "Email extractor should be initialized"
    assert wizard.subject_extractor is not None, "Subject extractor should be initialized"
    assert wizard.rule_builder is not None, "Rule builder should be initialized"

    print("  ✓ All components initialized correctly")
except Exception as e:
    print(f"  ✗ Component initialization failed: {e}")

print("\nAll basic tests completed!")
