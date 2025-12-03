# Phase 2 Implementation Summary: Cache Builder Enhancement

## Overview
Successfully implemented FLAGS and INTERNALDATE fetching in the cache builder (`/root/imapfilter/core/cache_builder.py`).

## Changes Made

### 1. Added Required Import
**File:** `/root/imapfilter/core/cache_builder.py` (Line 5)

```python
import re  # Added for regex pattern matching
```

### 2. Created `_parse_fetch_response()` Function
**File:** `/root/imapfilter/core/cache_builder.py` (Lines 52-125)

**Function Signature:**
```python
def _parse_fetch_response(msg_data) -> tuple[bytes, list[str], str | None]:
```

**Purpose:**
Parse IMAP FETCH response to extract BODY[HEADER], FLAGS, and INTERNALDATE.

**Key Features:**
- Extracts header bytes from IMAP response
- Uses regex to parse FLAGS: `rb'FLAGS \(([^)]*)\)'`
- Uses regex to parse INTERNALDATE: `rb'INTERNALDATE "([^"]*)"'`
- Handles both tuple-based and bytes-based IMAP response formats
- Graceful error handling (returns empty lists/None if parsing fails)
- No exceptions raised - ensures backward compatibility

**Regex Patterns Used:**
- `rb'FLAGS \(([^)]*)\)'` - Captures flag names within parentheses
- `rb'INTERNALDATE "([^"]*)"'` - Captures date string within quotes

**Returns:**
- `header_bytes: bytes` - Raw email headers
- `flags: List[str]` - List of flag strings (e.g., `["\\Seen", "\\Flagged", "custom"]`)
- `internaldate: str | None` - Date string or None if not found

### 3. Updated FETCH Command
**File:** `/root/imapfilter/core/cache_builder.py` (Line 222)

**Before:**
```python
typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER])")
```

**After:**
```python
typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)")
```

### 4. Updated Response Parsing
**File:** `/root/imapfilter/core/cache_builder.py` (Lines 231-239)

**Before:**
```python
raw_hdr = _coalesce_fetch_payload(msg_data)
if not raw_hdr:
    continue
hdr_str = raw_hdr.decode(errors="ignore")
```

**After:**
```python
# Parse the FETCH response
raw_hdr, flags, internaldate = _parse_fetch_response(msg_data)
if not raw_hdr:
    logger.log(
        "WARNING",
        "cache_parse_failed",
        {"folder": folder, "uid": uid_value},
    )
    continue

hdr_str = raw_hdr.decode(errors="ignore")
```

### 5. Updated Storage Format
**File:** `/root/imapfilter/core/cache_builder.py` (Lines 243-248)

**Before:**
```python
db.execute(
    "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
    "VALUES(?,?,?,?)",
    (
        folder,
        uid_value,
        json.dumps({"header": hdr_str}),
        now_iso(),
    ),
)
```

**After:**
```python
# Build cache entry with FLAGS and INTERNALDATE
cache_entry = {"header": hdr_str}
if flags:
    cache_entry["flags"] = flags
if internaldate:
    cache_entry["internaldate"] = internaldate

db.execute(
    "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
    "VALUES(?,?,?,?)",
    (
        folder,
        uid_value,
        json.dumps(cache_entry),
        now_iso(),
    ),
)
```

### 6. Enhanced Error Handling
**File:** `/root/imapfilter/core/cache_builder.py` (Lines 223-229)

Added logging for FETCH failures:
```python
if typ != "OK":
    logger.log(
        "WARNING",
        "cache_fetch_failed",
        {"folder": folder, "uid": uid_value},
    )
    continue
```

## Sample Output

### Example 1: Complete Response
```python
# Input msg_data:
[(b'123 (FLAGS (\\Seen \\Flagged custom) INTERNALDATE "28-Oct-2025 07:30:19 +0000" BODY[HEADER] {500}',
  b'From: test@example.com\r\nSubject: Test Email\r\n')]

# Output:
header_bytes = b'From: test@example.com\r\nSubject: Test Email\r\n'
flags = ['\\Seen', '\\Flagged', 'custom']
internaldate = '28-Oct-2025 07:30:19 +0000'

# Stored in database as:
{
  "header": "From: test@example.com\r\nSubject: Test Email\r\n",
  "flags": ["\\Seen", "\\Flagged", "custom"],
  "internaldate": "28-Oct-2025 07:30:19 +0000"
}
```

### Example 2: Response with Only \Seen Flag
```python
# Input msg_data:
[(b'456 (FLAGS (\\Seen) INTERNALDATE "15-Nov-2025 12:45:30 +0000" BODY[HEADER] {300}',
  b'From: sender@test.com\r\nSubject: Another Test\r\n')]

# Output:
flags = ['\\Seen']
internaldate = '15-Nov-2025 12:45:30 +0000'

# Stored in database as:
{
  "header": "From: sender@test.com\r\nSubject: Another Test\r\n",
  "flags": ["\\Seen"],
  "internaldate": "15-Nov-2025 12:45:30 +0000"
}
```

### Example 3: Multiple Custom Flags
```python
# Input msg_data:
[(b'999 (FLAGS (\\Seen \\Draft $Important $Work) INTERNALDATE "20-Nov-2025 16:20:45 -0500" BODY[HEADER] {400}',
  b'From: multi@example.com\r\nSubject: Multiple Flags\r\n')]

# Output:
flags = ['\\Seen', '\\Draft', '$Important', '$Work']
internaldate = '20-Nov-2025 16:20:45 -0500'
```

### Example 4: Empty FLAGS
```python
# Input msg_data:
[(b'789 (FLAGS () INTERNALDATE "01-Dec-2025 09:15:00 +0000" BODY[HEADER] {200}',
  b'From: noflags@example.com\r\n')]

# Output:
flags = []
internaldate = '01-Dec-2025 09:15:00 +0000'

# Stored in database as (flags omitted since empty):
{
  "header": "From: noflags@example.com\r\n",
  "internaldate": "01-Dec-2025 09:15:00 +0000"
}
```

## Backward Compatibility

### Old Cache Format (Still Valid)
```json
{
  "header": "From: old@example.com\r\nSubject: Old Format\r\n"
}
```

### Reading Old Cache Entries
```python
cache_data = json.loads(old_cache_entry)
header = cache_data.get("header", "")
flags = cache_data.get("flags", [])  # Defaults to []
internaldate = cache_data.get("internaldate", None)  # Defaults to None
```

**Key Points:**
- Old cache entries (header-only JSON) continue to work
- Missing fields default to `[]` and `None` using `.get()` method
- No migration required for existing cache
- New entries automatically include FLAGS and INTERNALDATE when available

## Testing

### Unit Tests Created

1. **test_cache_parser.py** - Tests `_parse_fetch_response()` function
   - Complete responses with FLAGS and INTERNALDATE
   - Responses with only single flag
   - Responses with empty FLAGS
   - Multiple custom flags
   - Empty response edge cases
   - JSON storage format validation

2. **test_backward_compat.py** - Validates backward compatibility
   - Reading old cache format (header-only)
   - Reading new cache format (with flags and internaldate)
   - Reading partial new format (flags only, no internaldate)
   - Demonstrates graceful handling with `.get()` defaults

### Test Results
```
All tests passed successfully!
✓ _parse_fetch_response() extracts FLAGS and INTERNALDATE correctly
✓ Multiple flag types handled (system flags, custom flags, keywords)
✓ Empty FLAGS handled gracefully
✓ INTERNALDATE with various timezones parsed correctly
✓ Backward compatibility maintained
✓ No syntax errors in cache_builder.py
```

## Error Handling

### Graceful Degradation
1. **FETCH Failure:** Logged as WARNING, processing continues with next message
2. **Parse Failure:** Logged as WARNING, processing continues with next message
3. **Missing FLAGS/INTERNALDATE:** Handled gracefully, fields omitted from cache entry
4. **Malformed Response:** Exception caught in try-except, returns partial data

### Logging Events
- `cache_fetch_failed`: FETCH command returned non-OK status
- `cache_parse_failed`: Unable to extract header from response

## Integration Points

### Dependencies
- No new external dependencies added
- Uses standard library `re` module for regex
- Compatible with existing IMAP client implementation

### Database Schema
- No changes required to database schema
- Uses existing `headers` table with JSON `data` column
- Flexible JSON structure supports both old and new formats

## Performance Impact

### Minimal Overhead
- Single FETCH command retrieves all data (HEADER, FLAGS, INTERNALDATE)
- Regex parsing is fast for small IMAP responses
- No additional round trips to IMAP server
- Storage increase: ~50-100 bytes per message (for flags and date)

### Benefits
- Reduces need for future FETCH operations
- Enables flag-based filtering without additional queries
- Supports date-based operations using cached INTERNALDATE

## Issues Encountered

**None.** Implementation completed without issues.

- IMAP FETCH syntax validated
- Regex patterns tested with various flag combinations
- Backward compatibility verified
- Error handling tested with edge cases

## Next Steps for Integration

1. **Rule Engine:** Can now access `flags` and `internaldate` from cache
2. **Executor:** Can use cached FLAGS for filtering decisions
3. **Performance:** Monitor cache size growth in production
4. **Documentation:** Update user docs to explain new cache capabilities

## Verification Checklist

- [x] FETCH command updated to include FLAGS and INTERNALDATE
- [x] `_parse_fetch_response()` function created (~75 lines)
- [x] FLAGS extracted using regex `rb'FLAGS \(([^)]*)\)'`
- [x] INTERNALDATE extracted using regex `rb'INTERNALDATE "([^"]*)"'`
- [x] Storage format updated to include flags and internaldate
- [x] Backward compatibility maintained
- [x] Error handling implemented (FETCH and parse failures)
- [x] Unit tests created and passing
- [x] Syntax validation passed
- [x] No issues encountered

## Files Modified

1. `/root/imapfilter/core/cache_builder.py` - Main implementation

## Files Created (Testing)

1. `/root/imapfilter/test_cache_parser.py` - Unit tests for _parse_fetch_response()
2. `/root/imapfilter/test_backward_compat.py` - Backward compatibility tests

---

**Implementation Status:** COMPLETE ✓

**Date:** 2025-12-01

**Phase:** 2 of Multi-Phase Implementation
