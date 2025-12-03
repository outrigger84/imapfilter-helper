# Batch Mode Flow Comparison

## Before the Fix

```
┌─────────────────────────────────┐
│  Select Domain/Sender           │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Pre-populate Condition         │
│  (field="from", hardcoded)      │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Add Additional Conditions?     │
│  (with full field/match menus)  │ ◄── INCONSISTENT!
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Configure Logic, Action, etc.  │
└─────────────────────────────────┘
```

**Problem:** First condition is hardcoded to "from/contains", but additional conditions allow full field/match type selection. This creates an asymmetric and confusing UX.

---

## After the Fix

```
┌─────────────────────────────────┐
│  Select Domain/Sender           │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Pre-populate Condition         │
│  (field="from", match="contains")│
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│  Edit this condition?           │
│  (yes/no prompt)                │
└────┬────────────────────┬───────┘
     │                    │
 YES │                    │ NO
     │                    │
     ▼                    ▼
┌─────────────────┐  ┌──────────────────┐
│ Full Field Menu │  │ Keep Pre-populated│
│ Full Match Menu │  │ Condition        │
│ Pattern Selection│  └────────┬─────────┘
└────────┬────────┘           │
         │                    │
         │  Cancelled?        │
         │  ┌─────────────┐   │
         └─►│Restore Orig │   │
            └──────┬──────┘   │
                   │          │
                   └──────────┤
                              │
                              ▼
          ┌─────────────────────────────────┐
          │  Add Additional Conditions?     │
          │  (with full field/match menus)  │ ◄── NOW CONSISTENT!
          └────────────┬────────────────────┘
                       │
                       ▼
          ┌─────────────────────────────────┐
          │  Configure Logic, Action, etc.  │
          └─────────────────────────────────┘
```

**Solution:** After pre-population, user is given the option to edit the first condition with full field/match type selection, making it consistent with additional conditions.

---

## User Experience Flow

### Scenario 1: Keep Pre-populated (Fastest Path)
```
User selects: sender@example.com
System: "✓ Pre-populated condition: sender (from) contains 'sender@example.com'"
System: "Do you want to edit this pre-populated condition? (yes/no)"
User: "no"
System: "Add more conditions? (yes/no)"
...continues with rest of workflow
```

### Scenario 2: Edit with Full Options
```
User selects: sender@example.com
System: "✓ Pre-populated condition: sender (from) contains 'sender@example.com'"
System: "Do you want to edit this pre-populated condition? (yes/no)"
User: "yes"
System: "
============================================================
EDIT FIRST CONDITION
============================================================
Build your condition with full field and match type options...

Select header field:
  1. From (sender address)
  2. To (recipient address)
  3. Subject
  4. List-ID
  5. Reply-To
  6. Other (enter custom header)"
User: "3" (Subject)
...continues with full condition builder
```

### Scenario 3: Edit but Cancel (Restore Original)
```
User selects: sender@example.com
System: "✓ Pre-populated condition: sender (from) contains 'sender@example.com'"
System: "Do you want to edit this pre-populated condition? (yes/no)"
User: "yes"
System: Opens field menu
User: [ESC or invalid input to cancel]
System: "⚠️ Edit cancelled - keeping original pre-populated condition"
System: "Add more conditions? (yes/no)"
...continues with original pre-populated condition
```

---

## Code Changes Summary

### Location
File: `core/tools/rule_wizard_core.py`
Method: `run_batch_mode()`
Lines: 1570-1597

### Key Implementation Points

1. **Prompt User After Pre-population**
   ```python
   edit_choice = self._prompt_yes_no(
       "Do you want to edit this pre-populated condition? "
       "(You can change the field or match type)"
   )
   ```

2. **Handle Edit Choice**
   - `True` (yes): Clear conditions, call `_add_single_condition()`
   - `False` (no): Continue with pre-populated condition
   - `None` (cancelled): Continue with pre-populated condition

3. **Save/Restore Logic**
   ```python
   saved_conditions = self.rule_builder.conditions.copy()
   self.rule_builder.conditions.clear()
   success = self._add_single_condition()
   if not success:
       self.rule_builder.conditions = saved_conditions
   ```

4. **User Feedback**
   - Clear messages at each decision point
   - Visual separators for edit mode
   - Warning message if edit is cancelled

---

## Benefits

1. **Consistency:** First condition now has same flexibility as additional conditions
2. **Flexibility:** Users can match on any field, not just "from"
3. **Usability:** Clear prompts guide the user through the process
4. **Safety:** Cancellation at any point restores original state
5. **Backward Compatible:** Users can still use the fast path by saying "no"

## Testing

- ✅ Code compiles without errors
- ✅ Logic tests pass (4/4 scenarios)
- ✅ Rule engine tests pass (no regression)
- ⏳ Manual testing pending (requires user interaction)
