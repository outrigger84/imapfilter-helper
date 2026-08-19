"""Shared utility functions for rule management.

This module provides common functions used by both rule_manager.py and
rule_wizard_core.py to avoid code duplication.
"""
from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
from typing import List


def encode_mailbox_utf7(mailbox: str) -> str:
    """Encode a mailbox name to IMAP modified UTF-7 (mUTF-7, RFC 3501).

    '&' must become '&-'; non-printable-ASCII characters use &<modified-base64>-.
    Rule "target" fields are written in plain text, but cached/IMAP folder
    names are always mUTF-7 -- comparing the two directly without this
    encoding silently fails for any folder containing '&' or other special
    characters.
    """
    result = []
    i = 0
    while i < len(mailbox):
        ch = mailbox[i]
        if ch == '&':
            result.append('&-')
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            run = []
            while i < len(mailbox) and (ord(mailbox[i]) < 0x20 or ord(mailbox[i]) > 0x7e):
                run.append(mailbox[i])
                i += 1
            encoded = ''.join(run).encode('utf-16-be')
            b64 = base64.b64encode(encoded).decode('ascii').rstrip('=').replace('/', ',')
            result.append(f'&{b64}-')
            continue
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def slugify(name: str) -> str:
    """Convert a rule name to a filesystem-safe slug.

    This function creates URL/filename-safe versions of rule names by:
    - Converting to lowercase
    - Keeping alphanumeric characters
    - Replacing spaces, hyphens, dots, slashes, and underscores with underscores
    - Removing consecutive underscores
    - Stripping leading/trailing underscores

    Args:
        name: The rule name to slugify (e.g., "Banking » NatWest")

    Returns:
        Slugified string (e.g., "banking_natwest")
        Returns "rule" if the result would be empty

    Examples:
        >>> slugify("Banking » NatWest")
        'banking_natwest'
        >>> slugify("Newsletters/Reddit")
        'newsletters_reddit'
        >>> slugify("Events » SoulCycle [Cancelled]")
        'events_soulcycle_cancelled'
    """
    safe: List[str] = []
    for ch in name.lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {" ", "-", ".", "/", "_", "»", "[", "]", "(", ")", ",", ":"}:
            safe.append("_")

    slug = "".join(safe).strip("_")

    # Collapse multiple consecutive underscores
    while "__" in slug:
        slug = slug.replace("__", "_")

    return slug or "rule"


def generate_filename(name: str, rules_dir: Path) -> Path:
    """Generate the next available filename for a new rule.

    This function generates filenames in the format: {5-digit-id}_{slug}.json
    The ID is determined by finding the maximum numeric prefix in existing
    rule files and incrementing it by 1. If no rules exist, it generates
    an ID from the current timestamp.

    Args:
        name: The rule name to generate a filename for
        rules_dir: Path to the rules directory

    Returns:
        Full path to the new rule file

    Examples:
        >>> # If max existing ID is 99012
        >>> generate_filename("Banking » NatWest", Path("/rules"))
        Path('/rules/99013_banking_natwest.json')
    """
    slug = slugify(name)
    numeric_prefixes: List[int] = []

    # Scan existing rule files for numeric prefixes
    if rules_dir.exists():
        for rule_file in rules_dir.glob("*.json"):
            stem = rule_file.stem
            prefix = ""
            for ch in stem:
                if ch.isdigit():
                    prefix += ch
                else:
                    break
            if prefix:
                try:
                    numeric_prefixes.append(int(prefix))
                except ValueError:
                    continue

    # Generate next ID (max + 1, or timestamp-based if no existing rules)
    if numeric_prefixes:
        next_id = max(numeric_prefixes) + 1
    else:
        # Generate ID from current timestamp: YYJJJHHMM (year, julian day, hour, minute)
        next_id = int(datetime.now().strftime("%y%j%H%M"))

    filename = f"{next_id:05d}_{slug}.json"
    return rules_dir / filename
