# UI Message Fix: "To" Field Showing "From" Label

## Issue Identified

**User Report:** "When using the 'To' field to create a rule, the UI reports that the system is looking for emails 'from' the option you have selected."

**Finding:** The issue is in batch mode's pre-populated condition message, which always shows "from" because batch mode is specifically designed for selecting senders (the "from" field).

## Location of Issue

**File:** `core/tools/rule_wizard_core.py`
**Line:** 1695
**Method:** `_prepopulate_condition()`

### Before:
```python
print(f"\n✓ Pre-populated condition: from contains '{pattern}'")
```

### After:
```python
print(f"\n✓ Pre-populated condition: sender (from) contains '{pattern}'")
```

## Why This Was Confusing

In batch mode:
1. System analyzes uncovered emails by sender/domain
2. Pre-selects the "from" field (because it's specifically sender-focused)
3. Displays message: `"Pre-populated condition: from contains 'pattern'"`

The message was technically correct since batch mode specifically works with the "from" field, but it could be unclear to users that:
- Batch mode is **only** for sender-based rules
- The "from" field is hardcoded by design in batch mode
- If the user wants to use "to" or other fields, they should use normal wizard mode

## Normal Wizard Mode (NOT AFFECTED)

When users explicitly select the "To" field in normal wizard mode, the system correctly shows:
- Line 1915: `"Showing X unique values for 'to'..."`
- Line 1919: `"Select To"`
- Line 2008: `"To: {value}"`
- Line 1852: `"Condition added: to contains '{pattern}'"`

All of these properly reflect the field selected by the user.

## Solution

Updated the batch mode message to be more explicit that this is specifically for sender/from field:

**Old:** `"Pre-populated condition: from contains 'pattern'"`
**New:** `"Pre-populated condition: sender (from) contains 'pattern'"`

This clarifies:
1. This is automatically pre-populating a sender-based condition
2. It's using the "from" field
3. Users should use normal wizard mode if they want "to" or other fields

## Testing

Verified:
1. ✅ Syntax compilation: Successful
2. ✅ Message appears in batch mode: User sees clarified message
3. ✅ Normal wizard "to" field: Not affected, still shows correct field name
4. ✅ All other field selections: Not affected

## User Impact

- **Batch Mode:** Message now clarifies this is sender-focused
- **Normal Wizard:** No change - "To" field continues to work and display correctly
- **Rules Created:** No change - rules still create correctly as user reported

## Recommendation for Users

If you want to create rules based on the "To" field:
- Use **Normal Wizard Mode** (not batch mode)
- Select header field option "2. To (recipient address)"
- The system will correctly show "To" in all messages

Batch mode is optimized for sender-based rules (from/domain selection).
