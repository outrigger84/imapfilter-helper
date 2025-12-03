# Phase 2 - Quick Reference

## What Changed in cache_builder.py

### Import Added (Line 5)
```python
import re  # For FLAGS and INTERNALDATE regex parsing
```

### New Function: `_parse_fetch_response()` (Lines 52-125)
```python
def _parse_fetch_response(msg_data) -> tuple[bytes, list[str], str | None]:
    """
    Parse IMAP FETCH response to extract BODY[HEADER], FLAGS, and INTERNALDATE.

    Returns: (header_bytes, flags_list, internaldate_string)
    """
    # ... implementation using regex patterns:
    # - rb'FLAGS \(([^)]*)\)' for FLAGS
    # - rb'INTERNALDATE "([^"]*)"' for INTERNALDATE
```

### FETCH Command Updated (Line 222)
```python
# OLD:
typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER])")

# NEW:
typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)")
```

### Parsing Updated (Lines 231-241)
```python
# OLD:
raw_hdr = _coalesce_fetch_payload(msg_data)
if not raw_hdr:
    continue
hdr_str = raw_hdr.decode(errors="ignore")

# NEW:
raw_hdr, flags, internaldate = _parse_fetch_response(msg_data)
if not raw_hdr:
    logger.log("WARNING", "cache_parse_failed", {"folder": folder, "uid": uid_value})
    continue
hdr_str = raw_hdr.decode(errors="ignore")
```

### Storage Format Updated (Lines 243-248)
```python
# OLD:
json.dumps({"header": hdr_str})

# NEW:
cache_entry = {"header": hdr_str}
if flags:
    cache_entry["flags"] = flags
if internaldate:
    cache_entry["internaldate"] = internaldate
json.dumps(cache_entry)
```

## Sample Cache Entries

### Before (Old Format)
```json
{
  "header": "From: test@example.com\r\nSubject: Test\r\n"
}
```

### After (New Format - Complete)
```json
{
  "header": "From: test@example.com\r\nSubject: Test\r\n",
  "flags": ["\\Seen", "\\Flagged"],
  "internaldate": "28-Oct-2025 07:30:19 +0000"
}
```

### After (New Format - Partial)
```json
{
  "header": "From: test@example.com\r\nSubject: Test\r\n",
  "internaldate": "28-Oct-2025 07:30:19 +0000"
}
```

## How to Read Cache Entries (Backward Compatible)

```python
import json

# Load from database
cache_data = json.loads(db_row["data"])

# Extract fields (works with both old and new format)
header = cache_data.get("header", "")           # Always present
flags = cache_data.get("flags", [])             # Defaults to []
internaldate = cache_data.get("internaldate")   # Defaults to None

# Use in filtering logic
if "\\Seen" in flags:
    print("Message has been read")

if internaldate:
    print(f"Message received on: {internaldate}")
```

## Regex Patterns

### FLAGS Pattern
```python
rb'FLAGS \(([^)]*)\)'
```
- Matches: `FLAGS (\\Seen \\Flagged custom)`
- Captures: `\\Seen \\Flagged custom`
- Result: `['\\Seen', '\\Flagged', 'custom']`

### INTERNALDATE Pattern
```python
rb'INTERNALDATE "([^"]*)"'
```
- Matches: `INTERNALDATE "28-Oct-2025 07:30:19 +0000"`
- Captures: `28-Oct-2025 07:30:19 +0000`
- Result: `'28-Oct-2025 07:30:19 +0000'`

## Testing Commands

```bash
# Test the parser function
python3 test_cache_parser.py

# Test backward compatibility
python3 test_backward_compat.py

# Validate syntax
python3 -m py_compile /root/imapfilter/core/cache_builder.py
```

## Common FLAG Values

- `\\Seen` - Message has been read
- `\\Answered` - Message has been replied to
- `\\Flagged` - Message is marked as important
- `\\Deleted` - Message is marked for deletion
- `\\Draft` - Message is a draft
- `\\Recent` - Message is recent
- `$Custom` - Custom flags (user-defined)

## INTERNALDATE Format

Standard IMAP date format: `"DD-Mon-YYYY HH:MM:SS +ZZZZ"`

Examples:
- `"28-Oct-2025 07:30:19 +0000"` (UTC)
- `"15-Nov-2025 12:45:30 -0500"` (EST)
- `"01-Dec-2025 09:15:00 +0100"` (CET)

---

**Quick Status:** Phase 2 COMPLETE ✓
