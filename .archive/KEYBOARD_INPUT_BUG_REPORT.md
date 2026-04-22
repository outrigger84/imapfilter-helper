# Keyboard Input Bug Report & Fix

## Issue Identified

**Problem:** Uppercase 'K' and 'J' key presses were not recognized for navigation in the folder selection screen (and other FilterableListSelector screens).

**Root Cause:** The keyboard handler only checked for lowercase 'k' and 'j' characters, but when users pressed uppercase 'K' or 'J', these fell through to the "printable characters" handler which added them to the filter text instead of navigating.

**Location:** `core/tools/rule_wizard_core.py`, lines 182 and 186 in the `_handle_key()` method

## The Bug

### Before (Lines 182 & 186):
```python
if key in (curses.KEY_UP, ord("k")):           # Only lowercase 'k'
    if self.selected_index > 0:
        self.selected_index -= 1

elif key in (curses.KEY_DOWN, ord("j")):       # Only lowercase 'j'
    if self.selected_index < len(self.filtered_items) - 1:
        self.selected_index += 1
```

When user presses uppercase 'K' or 'J':
1. Line 182 check fails (only looks for lowercase 'k')
2. Line 186 check fails (only looks for lowercase 'j')
3. Falls through to line 224 (printable character handler):
   ```python
   elif 32 <= key <= 126:
       char = chr(key)
       self.filter_text += char        # K or J added to filter!
   ```
4. Result: Uppercase 'K'/'J' gets added to filter text instead of navigating

### After (Lines 182 & 186):
```python
if key in (curses.KEY_UP, ord("k"), ord("K")):     # Now handles both cases
    if self.selected_index > 0:
        self.selected_index -= 1

elif key in (curses.KEY_DOWN, ord("j"), ord("J")): # Now handles both cases
    if self.selected_index < len(self.filtered_items) - 1:
        self.selected_index += 1
```

## Affected Screens

This bug affected ALL screens using `FilterableListSelector`:

1. **Folder Selection** - when choosing target folder for move actions
2. **Header Value Selection** - when selecting from/to/subject values
3. **Keyword Selection** - when selecting keywords from predefined/cached lists
4. **Any other list selection screen**

## Related Code Review

### Similar Issues Found: NONE

**Good News:** Other keyboard handling code properly handles case-insensitivity:

1. ✅ `_prompt_yes_no()` method (line 2658):
   ```python
   response = input("  (yes/no) > ").strip().lower()  # Converts to lowercase
   ```

2. ✅ Yes/No prompts throughout (lines 1431, 1438, 1528, etc.):
   ```python
   choice = input("\n...? (y/n): ").strip().lower()  # All call .lower()
   ```

3. ✅ Numeric choice inputs - not affected by case

### FilterableListSelector - The ONLY Affected Component

The `FilterableListSelector` class was the only place in the codebase with this specific bug because:
- It's the only component using low-level `curses.getch()` for direct key handling
- It's the only place implementing vim-like 'j'/'k' navigation
- It was checking characters by `ord()` without accounting for uppercase variants

## Testing

To verify the fix works:

1. Run the rule wizard and get to folder selection screen
2. Try navigating with:
   - Arrow keys (↑/↓) - should work
   - Lowercase 'j'/'k' - should work ✅
   - Uppercase 'J'/'K' - should now work ✅ (previously added to filter)

## Summary

| Aspect | Details |
|--------|---------|
| **Bug Type** | Case-sensitivity issue in keyboard input handler |
| **Location** | `core/tools/rule_wizard_core.py:182,186` |
| **Component** | `FilterableListSelector._handle_key()` |
| **Affected Screens** | All list selection screens (folders, values, keywords) |
| **Other Similar Bugs** | None found in codebase |
| **Fix Applied** | Added uppercase 'K' and 'J' to navigation key checks |
| **Status** | ✅ FIXED |
| **Syntax Verified** | ✅ YES |

## Commit Message

```
Fix: Handle uppercase K/J keys in FilterableListSelector navigation

Previously, uppercase K and J keypresses were not recognized as navigation
commands and were instead added to the filter text. This affected all
list selection screens (folders, values, keywords, etc.).

Now both lowercase (k/j) and uppercase (K/J) vim-like navigation keys
work correctly. Other keyboard handling throughout the codebase already
properly handles case-insensitivity.

- Modified: core/tools/rule_wizard_core.py lines 182, 186
- Added: ord("K") and ord("J") to navigation key checks
- Verified: No other similar keyboard input bugs in codebase
```
