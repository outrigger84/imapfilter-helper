#!/usr/bin/env python3
"""
Smoke test for rule_wizard.py entry point.

This test verifies that:
1. rule_wizard.py can be imported without errors
2. The main() function can be called
3. It properly validates the cache
4. It initializes all components
5. It handles errors gracefully
6. It returns appropriate exit codes

The test focuses on initialization and error handling without requiring
interactive user input.
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch

# Make repo-root imports (rule_wizard, core.*) work from any cwd
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Test results tracking
results = {
    "passed": [],
    "failed": [],
    "warnings": []
}


def test_result(test_name: str, passed: bool, message: str = ""):
    """Record test result."""
    if passed:
        results["passed"].append(test_name)
        print(f"  ✓ {test_name}")
        if message:
            print(f"    {message}")
    else:
        results["failed"].append((test_name, message))
        print(f"  ✗ {test_name}")
        if message:
            print(f"    ERROR: {message}")


def warning(test_name: str, message: str):
    """Record a warning."""
    results["warnings"].append((test_name, message))
    print(f"  ⚠ {test_name}")
    print(f"    WARNING: {message}")


def section_header(title: str):
    """Print a section header."""
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ==============================================================================
# Test 1: Import Tests
# ==============================================================================

section_header("Test 1: Import Tests")

print("\n1.1 Import rule_wizard module...")
try:
    import rule_wizard
    test_result("Import rule_wizard", True, f"Module path: {rule_wizard.__file__}")
except ImportError as e:
    test_result("Import rule_wizard", False, str(e))
    sys.exit(1)
except Exception as e:
    test_result("Import rule_wizard", False, f"Unexpected error: {e}")
    sys.exit(1)

print("\n1.2 Verify main() function exists...")
try:
    assert hasattr(rule_wizard, 'main'), "main() function not found"
    assert callable(rule_wizard.main), "main is not callable"
    test_result("main() function exists", True)
except AssertionError as e:
    test_result("main() function exists", False, str(e))
    sys.exit(1)

print("\n1.3 Verify RuleWizard class exists...")
try:
    assert hasattr(rule_wizard, 'RuleWizard'), "RuleWizard class not found"
    assert isinstance(rule_wizard.RuleWizard, type), "RuleWizard is not a class"
    test_result("RuleWizard class exists", True)
except AssertionError as e:
    test_result("RuleWizard class exists", False, str(e))
    sys.exit(1)

print("\n1.4 Verify all dependencies can be imported...")
try:
    from core.config import build_default_config, AppConfig
    from core.tools.rule_wizard_core import (  # noqa: F401 — imported to verify availability
        CacheQueryEngine,
        EmailPatternExtractor,
        SubjectPatternExtractor,
        RuleBuilder,
        FilterableListSelector,
        format_count,
        save_rule,
    )
    test_result("All dependencies imported", True)
except ImportError as e:
    test_result("All dependencies imported", False, str(e))
    sys.exit(1)

# ==============================================================================
# Test 2: Configuration and Cache Validation
# ==============================================================================

section_header("Test 2: Configuration and Cache Validation")

print("\n2.1 Load default configuration...")
try:
    from core.config import build_default_config
    config = build_default_config()
    test_result("Configuration loaded", True, f"Base dir: {config.paths.base_dir}")
except Exception as e:
    test_result("Configuration loaded", False, str(e))
    sys.exit(1)

print("\n2.2 Verify cache file exists...")
cache_path = config.paths.db_file
if cache_path.exists():
    size_mb = cache_path.stat().st_size / 1024 / 1024
    test_result("Cache file exists", True, f"Path: {cache_path}, Size: {size_mb:.1f} MB")
else:
    test_result("Cache file exists", False, f"Cache not found at {cache_path}")
    warning("Cache missing", "Run 'python -m core.cli build-cache' to create cache")
    print("\nStopping tests - cache is required for remaining tests")
    sys.exit(1)

print("\n2.3 Verify cache database is valid...")
try:
    import time

    # Try to connect with timeout and retry logic
    max_retries = 3
    retry_delay = 1
    conn = None

    for attempt in range(max_retries):
        try:
            # Use a timeout to avoid hanging on locked database
            conn = sqlite3.connect(str(cache_path), timeout=5.0)
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                print(f"    Database locked, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
            else:
                raise

    if conn is None:
        raise sqlite3.OperationalError("Failed to connect to database after retries")

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='headers'")
    table_exists = cursor.fetchone() is not None

    if not table_exists:
        test_result("Cache schema valid", False, "headers table not found")
        conn.close()
        sys.exit(1)

    cursor.execute("SELECT COUNT(*) FROM headers")
    count = cursor.fetchone()[0]
    conn.close()

    if count == 0:
        test_result("Cache has data", False, "Cache is empty")
        warning("Empty cache", "Build cache with messages before running wizard")
        sys.exit(1)

    test_result("Cache schema valid", True, f"Found {count:,} messages in cache")
except sqlite3.OperationalError as e:
    if "locked" in str(e).lower():
        test_result("Cache schema valid", False, "Database is locked by another process")
        warning("Database locked", "Close other processes using the cache database")
    else:
        test_result("Cache schema valid", False, str(e))
    sys.exit(1)
except Exception as e:
    test_result("Cache schema valid", False, str(e))
    sys.exit(1)

# ==============================================================================
# Test 3: Component Initialization
# ==============================================================================

section_header("Test 3: Component Initialization")

print("\n3.1 Initialize RuleWizard with config...")
try:
    wizard = rule_wizard.RuleWizard(config)
    test_result("RuleWizard instantiation", True)
except Exception as e:
    test_result("RuleWizard instantiation", False, str(e))
    sys.exit(1)

print("\n3.2 Verify RuleWizard attributes...")
try:
    assert hasattr(wizard, 'config'), "Missing config attribute"
    assert hasattr(wizard, 'cache_path'), "Missing cache_path attribute"
    assert hasattr(wizard, 'rules_dir'), "Missing rules_dir attribute"
    assert hasattr(wizard, 'builder'), "Missing builder attribute"
    test_result("RuleWizard attributes", True)
except AssertionError as e:
    test_result("RuleWizard attributes", False, str(e))

print("\n3.3 Verify cache engine can be initialized...")
try:
    from core.tools.rule_wizard_core import CacheQueryEngine
    engine = CacheQueryEngine(cache_path, show_progress=False)

    # Test basic query
    cursor = engine.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM headers")
    msg_count = cursor.fetchone()[0]

    engine.close()
    test_result("CacheQueryEngine initialization", True, f"{msg_count:,} messages accessible")
except Exception as e:
    test_result("CacheQueryEngine initialization", False, str(e))

print("\n3.4 Test cache query methods...")
try:
    from core.tools.rule_wizard_core import CacheQueryEngine
    engine = CacheQueryEngine(cache_path, show_progress=False)

    # Test extract_unique_from_addresses
    from_addrs = engine.extract_unique_from_addresses(limit=5)
    assert len(from_addrs) > 0, "No From addresses found"
    assert all(isinstance(item, tuple) and len(item) == 2 for item in from_addrs), "Invalid From address format"

    # Test extract_unique_subjects
    subjects = engine.extract_unique_subjects(limit=5)
    assert len(subjects) > 0, "No subjects found"

    # Test count methods
    if from_addrs:
        test_email = from_addrs[0][0]
        count = engine.count_from_contains(test_email)
        assert count > 0, "count_from_contains returned 0"

    engine.close()
    test_result("Cache query methods", True, f"Found {len(from_addrs)} senders, {len(subjects)} subjects")
except Exception as e:
    test_result("Cache query methods", False, str(e))

print("\n3.5 Test EmailPatternExtractor...")
try:
    from core.tools.rule_wizard_core import EmailPatternExtractor, CacheQueryEngine
    extractor = EmailPatternExtractor()
    engine = CacheQueryEngine(cache_path, show_progress=False)

    from_addrs = engine.extract_unique_from_addresses(limit=1)
    if from_addrs:
        test_email = from_addrs[0][0]
        patterns = extractor.suggest_patterns(test_email, engine)
        assert len(patterns) > 0, "No patterns generated"
        assert all(isinstance(p, tuple) and len(p) == 3 for p in patterns), "Invalid pattern format"
        test_result("EmailPatternExtractor", True, f"Generated {len(patterns)} patterns for {test_email[:30]}")
    else:
        warning("EmailPatternExtractor", "No emails to test with")

    engine.close()
except Exception as e:
    test_result("EmailPatternExtractor", False, str(e))

print("\n3.6 Test SubjectPatternExtractor...")
try:
    from core.tools.rule_wizard_core import SubjectPatternExtractor, CacheQueryEngine
    extractor = SubjectPatternExtractor()
    engine = CacheQueryEngine(cache_path, show_progress=False)

    subjects = engine.extract_unique_subjects(limit=1)
    if subjects:
        test_subject = subjects[0][0]
        patterns = extractor.suggest_patterns(test_subject, engine)
        assert len(patterns) > 0, "No patterns generated"
        test_result("SubjectPatternExtractor", True, f"Generated {len(patterns)} patterns")
    else:
        warning("SubjectPatternExtractor", "No subjects to test with")

    engine.close()
except Exception as e:
    test_result("SubjectPatternExtractor", False, str(e))

print("\n3.7 Test RuleBuilder...")
try:
    from core.tools.rule_wizard_core import RuleBuilder
    builder = RuleBuilder()

    builder.set_name("Test Smoke Rule")
    builder.set_priority(100)
    builder.add_condition("from", "contains", "test@example.com")
    builder.add_action("move", "Test/Folder")

    valid, error = builder.validate()
    assert valid, f"Validation failed: {error}"

    rule = builder.generate_rule()
    assert "name" in rule, "Generated rule missing 'name'"
    assert "priority" in rule, "Generated rule missing 'priority'"
    assert "conditions" in rule, "Generated rule missing 'conditions'"
    assert "action" in rule, "Generated rule missing 'action'"

    test_result("RuleBuilder", True, "Built and validated test rule")
except Exception as e:
    test_result("RuleBuilder", False, str(e))

# ==============================================================================
# Test 4: Entry Point Validation
# ==============================================================================

section_header("Test 4: Entry Point Validation")

print("\n4.1 Test that wizard checks cache existence...")
try:
    # Create a config with non-existent cache
    from core.config import AppConfig, PathsConfig
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        paths = PathsConfig(base_dir=tmp_path)
        test_config = AppConfig(paths=paths)

        # Try to create wizard without cache
        test_wizard = rule_wizard.RuleWizard(test_config)

        # Call _check_cache() which should return False
        has_cache = test_wizard._check_cache()
        assert not has_cache, "_check_cache should return False for missing cache"

    test_result("Cache existence check", True, "Correctly detects missing cache")
except Exception as e:
    test_result("Cache existence check", False, str(e))

print("\n4.2 Test cache validation with empty cache...")
try:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        paths = PathsConfig(base_dir=tmp_path)
        test_config = AppConfig(paths=paths)

        # Create empty cache
        test_config.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(test_config.paths.db_file))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS headers (
                folder TEXT,
                uid TEXT,
                data TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()

        test_wizard = rule_wizard.RuleWizard(test_config)
        has_cache = test_wizard._check_cache()
        assert not has_cache, "_check_cache should return False for empty cache"

    test_result("Empty cache validation", True, "Correctly detects empty cache")
except Exception as e:
    test_result("Empty cache validation", False, str(e))

print("\n4.3 Test KeyboardInterrupt handling...")
try:
    # Mock the wizard.run() to raise KeyboardInterrupt
    with patch.object(rule_wizard.RuleWizard, 'run', side_effect=KeyboardInterrupt):
        exit_code = rule_wizard.main()
        assert exit_code == 130, f"Expected exit code 130, got {exit_code}"

    test_result("KeyboardInterrupt handling", True, "Returns exit code 130")
except Exception as e:
    test_result("KeyboardInterrupt handling", False, str(e))

print("\n4.4 Test general exception handling...")
try:
    # Mock the wizard.run() to raise a general exception
    with patch.object(rule_wizard.RuleWizard, 'run', side_effect=RuntimeError("Test error")):
        exit_code = rule_wizard.main()
        assert exit_code == 1, f"Expected exit code 1, got {exit_code}"

    test_result("General exception handling", True, "Returns exit code 1 on error")
except Exception as e:
    test_result("General exception handling", False, str(e))

print("\n4.5 Test successful wizard initialization (mock UI)...")
try:
    # Mock the run() method to return 0 (success) without user interaction
    with patch.object(rule_wizard.RuleWizard, 'run', return_value=0):
        exit_code = rule_wizard.main()
        assert exit_code == 0, f"Expected exit code 0, got {exit_code}"

    test_result("Successful initialization path", True, "Returns exit code 0 on success")
except Exception as e:
    test_result("Successful initialization path", False, str(e))

# ==============================================================================
# Test 5: Helper Functions
# ==============================================================================

section_header("Test 5: Helper Functions")

print("\n5.1 Test clear_screen()...")
try:
    rule_wizard.clear_screen()
    test_result("clear_screen()", True)
except Exception as e:
    test_result("clear_screen()", False, str(e))

print("\n5.2 Test print_banner()...")
try:
    # Capture output
    from io import StringIO
    old_stdout = sys.stdout
    sys.stdout = StringIO()

    rule_wizard.print_banner()
    output = sys.stdout.getvalue()

    sys.stdout = old_stdout

    assert "IMAPFilter Rule Wizard" in output, "Banner doesn't contain expected text"
    test_result("print_banner()", True)
except Exception as e:
    sys.stdout = old_stdout
    test_result("print_banner()", False, str(e))

print("\n5.3 Test print_section_header()...")
try:
    from io import StringIO
    old_stdout = sys.stdout
    sys.stdout = StringIO()

    rule_wizard.print_section_header("Test Section")
    output = sys.stdout.getvalue()

    sys.stdout = old_stdout

    assert "Test Section" in output, "Section header doesn't contain expected text"
    test_result("print_section_header()", True)
except Exception as e:
    sys.stdout = old_stdout
    test_result("print_section_header()", False, str(e))

print("\n5.4 Test format_count() function...")
try:
    from core.tools.rule_wizard_core import format_count

    assert format_count(1234) == "1,234", "format_count failed"
    assert format_count(1234567) == "1,234,567", "format_count failed for large number"
    test_result("format_count()", True)
except Exception as e:
    test_result("format_count()", False, str(e))

# ==============================================================================
# Test Summary
# ==============================================================================

section_header("Test Summary")

total_tests = len(results["passed"]) + len(results["failed"])
pass_rate = (len(results["passed"]) / total_tests * 100) if total_tests > 0 else 0

print()
print(f"Total tests run: {total_tests}")
print(f"Passed: {len(results['passed'])} ({pass_rate:.1f}%)")
print(f"Failed: {len(results['failed'])}")
print(f"Warnings: {len(results['warnings'])}")

if results["failed"]:
    print("\nFailed tests:")
    for test_name, error_msg in results["failed"]:
        print(f"  ✗ {test_name}")
        if error_msg:
            print(f"    {error_msg}")

if results["warnings"]:
    print("\nWarnings:")
    for test_name, warning_msg in results["warnings"]:
        print(f"  ⚠ {test_name}")
        if warning_msg:
            print(f"    {warning_msg}")

print()
print("=" * 70)

# Final readiness assessment
if len(results["failed"]) == 0:
    print("STATUS: ✓ READY TO RUN")
    print()
    print("The rule_wizard.py entry point is fully functional and ready to use.")
    print()
    print("Run the wizard with:")
    print("  python3 rule_wizard.py")
    print()
    print("Or:")
    print("  python3 -c 'import rule_wizard; exit(rule_wizard.main())'")
    exit_code = 0
else:
    print("STATUS: ✗ NEEDS FIXES")
    print()
    print("The following issues need to be resolved:")
    for test_name, error_msg in results["failed"]:
        print(f"  - {test_name}: {error_msg}")
    exit_code = 1

print("=" * 70)
print()

sys.exit(exit_code)
