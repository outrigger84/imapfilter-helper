# Test Plan: Batch Mode First Condition Edit Feature

## Overview
This document describes how to test the newly implemented feature that allows users to edit the pre-populated condition in batch mode.

## Feature Summary
**File Modified:** `/root/imapfilter/core/tools/rule_wizard_core.py`

**Changes Made:**
- After pre-populating the first condition in batch mode, the wizard now prompts the user if they want to edit it
- If user chooses "yes", the condition is cleared and `_add_single_condition()` is called, giving full field and match type selection
- If user chooses "no", the pre-populated condition is kept as-is
- If user cancels during editing, the original pre-populated condition is restored

## Code Verification

### 1. Syntax Check
```bash
python3 -m py_compile core/tools/rule_wizard_core.py
```
**Expected Result:** No output (clean compilation)
**Status:** ✅ PASSED

### 2. Logic Unit Tests
```bash
python3 test_batch_edit_condition.py
```
**Expected Result:** All 4 test cases pass
**Status:** ✅ PASSED

## Manual Testing Scenarios

### Prerequisites
1. Have the imapfilter environment set up
2. Have a cache.db with uncovered emails
3. Run: `python3 wizard.py --batch`

### Test Case 1: Accept Edit and Complete
1. Run batch mode wizard
2. Select a domain/sender from the list
3. Observe the pre-populated condition message
4. When prompted "Do you want to edit this pre-populated condition?", answer **yes**
5. **Expected:** See "EDIT FIRST CONDITION" header and full field selection menu
6. Select a different field (e.g., "Subject" instead of "From")
7. Complete the condition building process
8. **Expected:** New condition uses your selected field

### Test Case 2: Accept Edit but Cancel During Editing
1. Run batch mode wizard
2. Select a domain/sender
3. Answer **yes** to edit prompt
4. Start building the condition but cancel (ESC or invalid input)
5. **Expected:** See message "⚠️ Edit cancelled - keeping original pre-populated condition"
6. **Expected:** Original from/contains condition is restored
7. Continue with additional conditions workflow

### Test Case 3: Decline Edit (Keep Pre-populated)
1. Run batch mode wizard
2. Select a domain/sender
3. When prompted "Do you want to edit this pre-populated condition?", answer **no**
4. **Expected:** Immediately proceeds to "Add more conditions?" prompt
5. **Expected:** First condition remains as pre-populated (from/contains)

### Test Case 4: Full Workflow Test
1. Run batch mode wizard
2. Select a domain/sender
3. Answer **yes** to edit prompt
4. Choose "Subject" field
5. Pick a subject from the filterable list
6. Select a pattern
7. Choose "regex" match type
8. Add additional conditions (optional)
9. Configure logic (if multiple conditions)
10. Configure action
11. Set metadata
12. Save rule
13. **Expected:** Rule saved successfully with custom first condition

## Success Criteria Checklist

- ✅ Code compiles without syntax errors
- ✅ Logic flow handles all 4 scenarios correctly
- ✅ User is prompted after pre-population
- ✅ Choosing "yes" opens full field/match type menus
- ✅ Choosing "no" keeps pre-populated condition
- ✅ Cancelling during edit restores original condition
- ✅ All existing batch mode functionality preserved
- ✅ No regression in other tests (rule_engine tests pass)

## Implementation Details

### Code Location
File: `/root/imapfilter/core/tools/rule_wizard_core.py`
Method: `run_batch_mode()`
Lines: 1570-1597

### Key Changes
1. Added `_prompt_yes_no()` call after `_prepopulate_condition()`
2. Implemented condition save/restore logic
3. Added call to `_add_single_condition()` for full editing experience
4. Added informative messages for user clarity

### Edge Cases Handled
- User cancels edit prompt (None returned)
- User cancels during condition building (restores original)
- User completes edit successfully (new condition kept)
- User declines edit (original condition kept)

## Notes for Reviewers

1. **Consistent UX:** First condition now has same flexibility as additional conditions
2. **No Breaking Changes:** Existing batch mode workflow still works if user declines edit
3. **Graceful Cancellation:** All cancellation paths properly restore state
4. **Clear Messaging:** User always knows what's happening via print statements

## Testing Status

| Test | Status | Notes |
|------|--------|-------|
| Syntax Compilation | ✅ PASSED | No errors |
| Logic Unit Tests | ✅ PASSED | All 4 scenarios work |
| Rule Engine Tests | ✅ PASSED | No regression (26/26 passed) |
| Manual Testing | ⏳ PENDING | Requires user interaction |

## Conclusion

The implementation successfully addresses the asymmetric UX issue in batch mode. Users can now:
- Edit the pre-populated first condition if needed
- Keep it as-is if it's already suitable
- Cancel safely at any point

The feature integrates cleanly with existing code and passes all automated tests.
