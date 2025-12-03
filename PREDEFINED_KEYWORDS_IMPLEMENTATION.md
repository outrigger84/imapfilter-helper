# Predefined Keywords Implementation Summary

## Overview

Successfully implemented a comprehensive predefined keywords management system that allows users to standardize on specific keywords that may not yet exist in the message cache. Keywords can be managed through three different approaches, with visual separation in the UI when selecting keywords.

## What Was Implemented

### 1. Core Keyword Management (`core/keywords.py`)

**KeywordManager Class**
- Manages predefined keywords stored in JSON configuration file
- Automatic creation of config file with default keywords on first use
- Methods: `get_keywords()`, `add_keyword()`, `remove_keyword()`, `keyword_exists()`
- Default keywords provided: Important, Review Needed, Action Item, Archive, Hold, Personal

**Configuration Storage**
- Location: `data/keywords.json` (relative to project base)
- Format: JSON with structure `{"predefined_keywords": ["keyword1", "keyword2", ...]}`
- Persists across sessions automatically

### 2. Rule Wizard Integration (`core/tools/rule_wizard_core.py`)

**KeywordManager Integration**
- KeywordManager initialized in `RuleWizard.__init__` with `config.paths.data_dir`
- Seamlessly integrated with existing keyword selection workflow

**Updated Methods**
- `_get_keywords()`: Enhanced to check for predefined keywords and offer management option
- `_select_keywords_from_list()`: Shows segmented list with emoji indicators:
  - 📌 PREDEFINED KEYWORDS (shown first)
  - ─────────────────────── (visual separator)
  - 📊 CACHED KEYWORDS (from messages, duplicates filtered)

**New Methods**
- `_manage_keywords()`: Interactive menu for add/remove/list/return options
- `_list_keywords()`: Display all predefined keywords with numbering
- `_add_keyword_interactive()`: Prompt user to add a keyword
- `_remove_keyword_interactive()`: Show list and remove selected keyword

### 3. CLI Management Commands (`core/cli.py`)

**Keywords Command**
```bash
imapfilter keywords list              # List all predefined keywords
imapfilter keywords add KEYWORD       # Add a new predefined keyword
imapfilter keywords remove KEYWORD    # Remove a predefined keyword
imapfilter keywords edit              # Open keywords config in editor
```

**Implementation**
- Added `keywords` subparser with `list`, `add`, `remove`, `edit` subcommands
- `handle_keywords()` function processes all keyword management operations
- Supports environment variable editors (EDITOR, VISUAL)
- Falls back to common editors: nano, vi, vim, gedit, code

### 4. Wizard CLI Flags (`wizard.py`)

**Keyword Management Flags**
```bash
python3 wizard.py --list-keywords              # List keywords and exit
python3 wizard.py --add-keyword KEYWORD        # Add keyword and exit
python3 wizard.py --remove-keyword KEYWORD     # Remove keyword and exit
python3 wizard.py                              # Start normal wizard
```

**Implementation**
- Argument parser with mutually exclusive keyword management options
- `handle_keyword_operations()` processes flags before wizard starts
- Returns to system after operation or continues to wizard if no flags

### 5. User Interface Improvements

**Keyword Selection List**
- Predefined keywords appear first with 📌 indicator
- Visual separator line between sections
- Cached keywords appear below with 📊 indicator
- Duplicates automatically filtered (keywords in both lists appear only in predefined)
- Message counts shown for cached keywords

**Interactive Menu**
- Users can manage keywords from within batch mode
- Option 3 in keyword selection menu: "Manage predefined keywords"
- Submenu allows: View, Add, Remove, Return to selection

## Three Management Approaches

### Approach 1: Direct File Editing
- Edit `data/keywords.json` directly with any text editor
- Format: `{"predefined_keywords": ["keyword1", "keyword2", ...]}`
- Changes take effect immediately when wizard restarts

### Approach 2: CLI Management Command
```bash
# List keywords
imapfilter keywords list

# Add keyword
imapfilter keywords add Important

# Remove keyword
imapfilter keywords remove Old

# Edit in default editor
imapfilter keywords edit
```

### Approach 3: Wizard CLI Flags
```bash
# Before starting wizard
python3 wizard.py --add-keyword NewKeyword
python3 wizard.py --remove-keyword OldKeyword
python3 wizard.py --list-keywords
```

### Approach 4: In-Wizard Management (Bonus)
```
Choose how to select keywords:
  1. Select from list (predefined + cached)
  2. Enter manually (comma-separated)
  3. Manage predefined keywords

  > 3

Manage predefined keywords:
  1. View all keywords
  2. Add new keyword
  3. Remove keyword
  4. Return to keyword selection
```

## Key Features

✅ **Predefined keywords take precedence** - shown first in UI
✅ **Automatic deduplication** - prevents keywords appearing twice in list
✅ **Visual segmentation** - emoji indicators distinguish keyword sources
✅ **Persistent storage** - JSON file automatically created and maintained
✅ **Multiple management methods** - three different approaches (file, CLI, wizard flags)
✅ **In-wizard management** - add/remove keywords without leaving wizard
✅ **Default keywords provided** - system works immediately without user config
✅ **Backward compatible** - existing code continues to work unchanged

## Configuration File

**Location:** `data/keywords.json`

**Format:**
```json
{
  "predefined_keywords": [
    "Important",
    "Review Needed",
    "Action Item",
    "Archive",
    "Hold",
    "Personal"
  ]
}
```

**Automatic Creation:**
- Created on first KeywordManager initialization if it doesn't exist
- Populated with default keywords automatically

## Testing

Created comprehensive test suite (`test_predefined_keywords.py`) verifying:
1. ✅ Direct file editing approach
2. ✅ CLI management commands
3. ✅ Wizard CLI flags
4. ✅ UI segmentation and deduplication
5. ✅ Data persistence across instances

**Test Results:** ALL TESTS PASSED

## Files Modified/Created

### Created:
- `core/keywords.py` - KeywordManager class (116 lines)
- `test_predefined_keywords.py` - Comprehensive test suite (270 lines)

### Modified:
- `core/tools/rule_wizard_core.py` - Added KeywordManager integration and management methods
  - Import: Added `from core.keywords import KeywordManager`
  - `__init__`: Added `self.keyword_manager = KeywordManager(config.paths.data_dir)`
  - `_get_keywords()`: Enhanced to offer predefined keywords and management option
  - `_select_keywords_from_list()`: Implemented emoji-based segmentation
  - New: `_manage_keywords()`, `_list_keywords()`, `_add_keyword_interactive()`, `_remove_keyword_interactive()`

- `core/cli.py` - Added keywords management CLI command
  - Import: Added `from core.keywords import KeywordManager`
  - `build_parser()`: Added keywords subparser with list/add/remove/edit subcommands
  - New: `handle_keywords()` function (80+ lines)
  - `COMMAND_HANDLERS`: Added `"keywords": handle_keywords`
  - `__all__`: Added `"handle_keywords"`

- `wizard.py` - Added CLI flags for keyword management
  - Added: `build_parser()` function with keyword management flags
  - Added: `handle_keyword_operations()` function
  - Modified: `main()` to parse args and handle keyword operations

## Usage Examples

### Using CLI Commands
```bash
# Add a new predefined keyword
$ imapfilter keywords add Budget
✓ Added keyword: Budget

# List all keywords
$ imapfilter keywords list
Predefined Keywords:
  1. Important
  2. Review Needed
  3. Action Item
  4. Archive
  5. Hold
  6. Personal
  7. Budget

# Remove a keyword
$ imapfilter keywords remove Archive
✓ Removed keyword: Archive

# Edit in default editor
$ imapfilter keywords edit
```

### Using Wizard CLI Flags
```bash
# Add keyword before starting wizard
$ python3 wizard.py --add-keyword Urgent
✓ Added keyword: Urgent

# Start wizard normally
$ python3 wizard.py
[wizard starts...]

# When asked to select keywords:
📌 4 predefined keyword(s) available

Choose how to select keywords:
  1. Select from list (predefined + cached)
  2. Enter manually (comma-separated)
  3. Manage predefined keywords
```

### In Wizard - Selecting Keywords
```
Select keywords to add/remove:
  📌 PREDEFINED KEYWORDS - always available
  📊 CACHED KEYWORDS - from your messages

Press Enter to open keyword selector...

Select Keywords (6 items)
Filter:
[all 6 items]

  1. 📌 Important (0 messages)
  2. 📌 Review Needed (0 messages)
  3. 📌 Action Item (0 messages)
  4. 📌 Archive (0 messages)
  5. 📌 Hold (0 messages)
  6. 📌 Personal (0 messages)
  ───────────────────────────────────
  7. 📊 Work (234 messages)
  8. 📊 Personal (156 messages)
```

### In Wizard - Managing Keywords
```
Choose how to select keywords:
  1. Select from list (predefined + cached)
  2. Enter manually (comma-separated)
  3. Manage predefined keywords
  > 3

Manage predefined keywords:
  1. View all keywords
  2. Add new keyword
  3. Remove keyword
  4. Return to keyword selection
  > 2

Enter new keyword: ClientReview
✓ Added: ClientReview

Manage predefined keywords:
  1. View all keywords
  2. Add new keyword
  3. Remove keyword
  4. Return to keyword selection
  > 1

Predefined Keywords:
  1. Important
  2. Review Needed
  3. Action Item
  4. Archive
  5. Hold
  6. Personal
  7. ClientReview
```

## Success Criteria Met

✅ Predefined keywords stored in configuration file
✅ Predefined keywords shown first in selection list with visual separator
✅ CLI commands for managing keywords (add/remove/list/edit)
✅ CLI flags for wizard (--add-keyword, --remove-keyword, --list-keywords)
✅ In-wizard management of predefined keywords
✅ Default keywords provided if config doesn't exist
✅ No duplication of keywords in UI
✅ All three management approaches working
✅ Consistent UI with emoji indicators
✅ Backward compatible with existing code

## Future Enhancements

Potential future improvements (not in scope of this implementation):
- Multiple keyword selection in one action
- Import/export keyword lists
- Keyword categories/grouping
- Keyword usage statistics
- Automatic keyword suggestion based on message patterns
