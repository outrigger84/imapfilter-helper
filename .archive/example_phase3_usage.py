#!/usr/bin/env python3
"""
Example: Phase 3 Rule Engine - Keyword and Age-Based Conditions

This script demonstrates how to use the new keyword (flag) and age-based
conditions in rules.
"""

import json
from datetime import datetime, timedelta, timezone

# ============================================================================
# Example 1: Flag-Based Conditions (Keywords)
# ============================================================================

print("=" * 70)
print("EXAMPLE 1: Flag-Based Conditions")
print("=" * 70)

# You can now check for IMAP flags/keywords in your rules:

rule_with_flags = {
    "name": "Archive Read Newsletters",
    "priority": 100,
    "conditions": {
        "all": [
            {"header": "from", "contains": "newsletter"},
            {"has_keyword": "\\Seen"},        # Message has been read
            {"has_keyword": "newsletter"}     # Custom keyword
        ]
    },
    "action": {
        "type": "move",
        "target": "Archive/Newsletters"
    }
}

print("\nRule definition:")
print(json.dumps(rule_with_flags, indent=2))

print("\n✅ This rule will match messages that:")
print("   - Are from a newsletter sender")
print("   - Have been read (\\Seen flag)")
print("   - Have the 'newsletter' custom keyword")

# ============================================================================
# Example 2: Negation with lacks_keyword
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 2: Negation with lacks_keyword")
print("=" * 70)

rule_with_negation = {
    "name": "Move Unread Important",
    "priority": 90,
    "conditions": {
        "all": [
            {"has_keyword": "\\Flagged"},     # Has important flag
            {"lacks_keyword": "\\Seen"}       # But NOT read yet
        ]
    },
    "action": {
        "type": "move",
        "target": "Priority/Unread"
    }
}

print("\nRule definition:")
print(json.dumps(rule_with_negation, indent=2))

print("\n✅ This rule will match messages that:")
print("   - Are flagged as important")
print("   - Have NOT been read yet")

# ============================================================================
# Example 3: Age-Based Conditions
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 3: Age-Based Conditions")
print("=" * 70)

rule_with_age = {
    "name": "Archive Old Emails",
    "priority": 80,
    "conditions": {
        "age_days_gt": 365  # Older than 1 year
    },
    "action": {
        "type": "move",
        "target": "Archive/OldMail"
    }
}

print("\nRule definition:")
print(json.dumps(rule_with_age, indent=2))

print("\n✅ This rule will match messages that:")
print("   - Are older than 365 days (1 year)")

print("\n📝 Available age operators:")
print("   - age_days_gt: Greater than N days (older)")
print("   - age_days_lt: Less than N days (newer)")
print("   - age_days_eq: Exactly N days old")

# ============================================================================
# Example 4: Combining Flags and Age
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 4: Combining Flags and Age")
print("=" * 70)

rule_combined = {
    "name": "Delete Old Spam",
    "priority": 70,
    "conditions": {
        "all": [
            {"has_keyword": "Junk"},      # Marked as spam/junk
            {"age_days_gt": 90},          # Older than 90 days
            {"has_keyword": "\\Seen"}     # Already read
        ]
    },
    "action": {
        "type": "move",
        "target": "[Gmail]/Trash"
    }
}

print("\nRule definition:")
print(json.dumps(rule_combined, indent=2))

print("\n✅ This rule will match messages that:")
print("   - Are marked as Junk")
print("   - Are older than 90 days")
print("   - Have been read")

# ============================================================================
# Example 5: Complex Logical Combinations
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 5: Complex Logical Combinations")
print("=" * 70)

rule_complex = {
    "name": "Archive Important Old Content",
    "priority": 60,
    "conditions": {
        "all": [
            # Must be old
            {"age_days_gt": 180},
            # Must be either newsletter OR important
            {
                "any": [
                    {"has_keyword": "newsletter"},
                    {"has_keyword": "important"},
                    {"header": "subject", "contains": "[Important]"}
                ]
            },
            # Must be read
            {"has_keyword": "\\Seen"}
        ]
    },
    "action": {
        "type": "move",
        "target": "Archive/Important"
    }
}

print("\nRule definition:")
print(json.dumps(rule_complex, indent=2))

print("\n✅ This rule will match messages that:")
print("   - Are older than 180 days")
print("   - AND (have 'newsletter' keyword OR 'important' keyword OR '[Important]' in subject)")
print("   - AND have been read")

# ============================================================================
# Example 6: Backward Compatibility
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 6: Backward Compatibility")
print("=" * 70)

rule_old_style = {
    "name": "Traditional Header Rule",
    "priority": 50,
    "conditions": {
        "header": "subject",
        "contains": "Newsletter"
    },
    "action": {
        "type": "move",
        "target": "Archive/Newsletters"
    }
}

print("\nRule definition:")
print(json.dumps(rule_old_style, indent=2))

print("\n✅ Old-style header-only rules still work perfectly!")
print("   - No need to update existing rules")
print("   - Flag and age conditions are optional")

# ============================================================================
# Example 7: Real-World Use Cases
# ============================================================================

print("\n" + "=" * 70)
print("EXAMPLE 7: Real-World Use Cases")
print("=" * 70)

use_cases = [
    {
        "name": "Clean Up Old Promotional Emails",
        "description": "Delete promotional emails older than 6 months",
        "rule": {
            "name": "Delete Old Promotions",
            "conditions": {
                "all": [
                    {"header": "from", "regex": ".*@.*\\.(marketing|promo)"},
                    {"age_days_gt": 180},
                    {"has_keyword": "\\Seen"}
                ]
            },
            "action": {"type": "move", "target": "[Gmail]/Trash"}
        }
    },
    {
        "name": "Archive Completed Project Emails",
        "description": "Archive old project emails with 'completed' keyword",
        "rule": {
            "name": "Archive Completed Projects",
            "conditions": {
                "all": [
                    {"header": "subject", "contains": "Project"},
                    {"has_keyword": "completed"},
                    {"age_days_gt": 30}
                ]
            },
            "action": {"type": "move", "target": "Archive/Projects/Completed"}
        }
    },
    {
        "name": "Prioritize Unread from VIPs",
        "description": "Move unread emails from important senders",
        "rule": {
            "name": "VIP Unread to Priority",
            "conditions": {
                "all": [
                    {"header": "from", "contains": "@company.com"},
                    {"lacks_keyword": "\\Seen"},
                    {"age_days_lt": 7}
                ]
            },
            "action": {"type": "move", "target": "Priority/Unread"}
        }
    }
]

for i, use_case in enumerate(use_cases, 1):
    print(f"\n{i}. {use_case['name']}")
    print(f"   {use_case['description']}")
    print(f"\n   Rule snippet:")
    print(f"   {json.dumps(use_case['rule']['conditions'], indent=6)}")

# ============================================================================
# Data Format Notes
# ============================================================================

print("\n" + "=" * 70)
print("DATA FORMAT NOTES")
print("=" * 70)

print("\n📦 Enhanced Cache Format:")
enhanced_format = {
    "header": "From: sender@example.com\\nSubject: Test\\n\\n",
    "flags": ["\\\\Seen", "newsletter", "important"],
    "internaldate": "28-Oct-2025 07:30:19 +0000"
}
print(json.dumps(enhanced_format, indent=2))

print("\n📦 Old Format (still supported):")
old_format = {
    "header": "From: sender@example.com\\nSubject: Test\\n\\n"
}
print(json.dumps(old_format, indent=2))

print("\n✅ Both formats work! The system automatically handles:")
print("   - Old format (header only) - flags/age conditions won't match")
print("   - Enhanced format (header + flags + date) - all features available")

# ============================================================================
# Common IMAP Flags
# ============================================================================

print("\n" + "=" * 70)
print("COMMON IMAP FLAGS")
print("=" * 70)

print("\n📌 Standard IMAP flags:")
print("   \\Seen       - Message has been read")
print("   \\Answered   - Message has been replied to")
print("   \\Flagged    - Message is marked as important")
print("   \\Deleted    - Message is marked for deletion")
print("   \\Draft      - Message is a draft")
print("   \\Recent     - Message is new (arrived recently)")

print("\n📌 Custom keywords (examples):")
print("   newsletter  - Custom tag for newsletters")
print("   important   - Custom importance tag")
print("   Junk        - Spam/junk marker")
print("   Work        - Work-related")
print("   Personal    - Personal emails")
print("   $Forwarded  - Message has been forwarded")

print("\n💡 Tip: You can use any custom keyword that your IMAP server supports!")

# ============================================================================
# Summary
# ============================================================================

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)

print("\n🎉 Phase 3 adds powerful new condition types:")
print("\n   1. Flag Conditions:")
print("      - has_keyword / has_flag")
print("      - lacks_keyword / lacks_flag")
print("\n   2. Age Conditions:")
print("      - age_days_gt (older than)")
print("      - age_days_lt (newer than)")
print("      - age_days_eq (exactly)")
print("\n   3. Full Backward Compatibility:")
print("      - Existing rules work unchanged")
print("      - Old cache format still supported")
print("      - Optional parameters (flags=None, date=None)")

print("\n📚 For more information:")
print("   - Run: python3 -m pytest test_phase3_rule_engine.py -v")
print("   - Run: python3 -m pytest test_phase3_integration.py -v")
print("   - Read: /root/imapfilter/core/rule_engine.py")

print("\n" + "=" * 70)
