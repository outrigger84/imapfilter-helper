#!/usr/bin/env python3
"""Integration test for RuleWizard - verify all components work together."""

from core.config import build_default_config
from core.tools.rule_wizard_core import (
    RuleWizard,
    CacheQueryEngine,
    EmailPatternExtractor,
    SubjectPatternExtractor,
    RuleBuilder,
)

print("=" * 70)
print("RuleWizard Integration Test")
print("=" * 70)

# Test 1: Verify config and cache
print("\n1. Testing configuration and cache...")
config = build_default_config()
print(f"   Base dir: {config.paths.base_dir}")
print(f"   Rules dir: {config.paths.rules_dir}")
print(f"   Cache file: {config.paths.db_file}")

if not config.paths.db_file.exists():
    print("   ✗ Cache file not found - run 'build-cache' first")
    exit(1)

print(f"   ✓ Cache file exists ({config.paths.db_file.stat().st_size / 1024 / 1024:.1f} MB)")

# Test 2: Initialize wizard
print("\n2. Initializing RuleWizard...")
try:
    wizard = RuleWizard(config)
    print("   ✓ Wizard initialized successfully")
except Exception as e:
    print(f"   ✗ Failed to initialize wizard: {e}")
    exit(1)

# Test 3: Test cache engine
print("\n3. Testing CacheQueryEngine...")
try:
    engine = CacheQueryEngine(config.paths.db_file)

    # Count messages
    cursor = engine.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM headers")
    message_count = cursor.fetchone()[0]
    print(f"   Messages in cache: {message_count:,}")

    # Test From addresses
    from_addrs = engine.extract_unique_from_addresses(limit=5)
    print(f"   Top 5 From addresses:")
    for addr, count in from_addrs[:5]:
        print(f"     - {addr[:50]} ({count:,})")

    # Test Subject lines
    subjects = engine.extract_unique_subjects(limit=5)
    print(f"   Top 5 Subject lines:")
    for subj, count in subjects[:5]:
        print(f"     - {subj[:50]}... ({count:,})")

    engine.close()
    print("   ✓ Cache engine works correctly")
except Exception as e:
    print(f"   ✗ Cache engine failed: {e}")
    exit(1)

# Test 4: Test EmailPatternExtractor
print("\n4. Testing EmailPatternExtractor...")
try:
    engine = CacheQueryEngine(config.paths.db_file)
    extractor = EmailPatternExtractor()

    # Get a real email address from cache
    from_addrs = engine.extract_unique_from_addresses(limit=1)
    if from_addrs:
        test_email = from_addrs[0][0]
        print(f"   Testing with: {test_email}")

        patterns = extractor.suggest_patterns(test_email, engine)
        print(f"   Generated {len(patterns)} patterns:")
        for pattern, desc, count in patterns[:3]:
            print(f"     - {pattern} ({desc}, {count:,} messages)")

    engine.close()
    print("   ✓ Email pattern extractor works correctly")
except Exception as e:
    print(f"   ✗ Email pattern extractor failed: {e}")
    exit(1)

# Test 5: Test SubjectPatternExtractor
print("\n5. Testing SubjectPatternExtractor...")
try:
    engine = CacheQueryEngine(config.paths.db_file)
    extractor = SubjectPatternExtractor()

    # Get a real subject line from cache
    subjects = engine.extract_unique_subjects(limit=1)
    if subjects:
        test_subject = subjects[0][0]
        print(f"   Testing with: {test_subject[:60]}...")

        patterns = extractor.suggest_patterns(test_subject, engine)
        print(f"   Generated {len(patterns)} patterns:")
        for pattern, desc, count in patterns[:3]:
            display = pattern[:50] + "..." if len(pattern) > 50 else pattern
            print(f"     - {display} ({desc}, {count:,} messages)")

    engine.close()
    print("   ✓ Subject pattern extractor works correctly")
except Exception as e:
    print(f"   ✗ Subject pattern extractor failed: {e}")
    exit(1)

# Test 6: Test RuleBuilder
print("\n6. Testing RuleBuilder...")
try:
    builder = RuleBuilder()
    builder.set_name("Test Rule » Banking")
    builder.set_priority(150)
    builder.add_condition("from", "contains", "bank@example.com")
    builder.add_condition("from", "contains", "noreply@bank.com")
    builder.set_logic("any")
    builder.set_action("move", "Banking/Test")
    builder.add_comment("Test rule created by integration test")

    valid, error = builder.validate()
    if not valid:
        print(f"   ✗ Validation failed: {error}")
        exit(1)

    rule = builder.generate_rule()
    print(f"   Generated rule:")
    print(f"     Name: {rule['name']}")
    print(f"     Priority: {rule['priority']}")
    print(f"     Conditions: {len(rule['conditions']['any'])} (ANY)")
    print(f"     Action: {rule['action']['type']} to {rule['action']['target']}")

    print("   ✓ Rule builder works correctly")
except Exception as e:
    print(f"   ✗ Rule builder failed: {e}")
    exit(1)

# Test 7: Test rule preview
print("\n7. Testing rule preview (dry-run)...")
try:
    engine = CacheQueryEngine(config.paths.db_file)

    # Create a simple rule
    builder = RuleBuilder()
    builder.set_name("Preview Test")
    builder.set_priority(100)

    # Use a common pattern that should match something
    from_addrs = engine.extract_unique_from_addresses(limit=1)
    if from_addrs:
        test_email = from_addrs[0][0]
        builder.add_condition("from", "contains", test_email)
        builder.set_action("move", "Test")

        rule = builder.generate_rule()

        # Use wizard's preview method
        wizard = RuleWizard(config)
        wizard.cache_engine = engine
        match_count = wizard._preview_rule(rule)

        print(f"   Test rule would match: {match_count:,} messages")
        print("   ✓ Rule preview works correctly")
    else:
        print("   ⚠ No messages in cache to test preview")

    engine.close()
except Exception as e:
    print(f"   ✗ Rule preview failed: {e}")
    exit(1)

print("\n" + "=" * 70)
print("All integration tests passed! ✓")
print("=" * 70)
print("\nThe RuleWizard is ready to use. Run it with:")
print("  python3 -c 'from core.config import build_default_config; from core.tools.rule_wizard_core import RuleWizard; wizard = RuleWizard(build_default_config()); exit(wizard.run())'")
