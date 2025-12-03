"""Shared utility functions for rule management.

This module provides common functions used by both rule_manager.py and
rule_wizard_core.py to avoid code duplication.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List


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


def load_all_rules(rules_dir: Path) -> List[dict]:
    """Load all rule JSON files from the specified directory.

    Args:
        rules_dir: Path to the rules directory

    Returns:
        List of rule dictionaries loaded from JSON files.
        Returns empty list if directory doesn't exist or has no JSON files.

    Raises:
        json.JSONDecodeError: If a rule file contains invalid JSON
    """
    if not rules_dir.exists():
        return []

    rules: List[dict] = []
    for rule_file in sorted(rules_dir.glob("*.json")):
        try:
            with rule_file.open("r", encoding="utf-8") as handle:
                rule_data = json.load(handle)
                # Add filename metadata for reference
                rule_data["_file"] = rule_file.name
                rules.append(rule_data)
        except json.JSONDecodeError as exc:
            # Re-raise with more context
            raise json.JSONDecodeError(
                f"Invalid JSON in {rule_file.name}: {exc.msg}",
                exc.doc,
                exc.pos
            ) from exc

    return rules


def save_rule_file(file_path: Path, rule: dict) -> None:
    """Save a rule dictionary to a JSON file with proper formatting.

    Args:
        file_path: Path where the rule file should be saved
        rule: Rule dictionary to save

    Notes:
        - Removes any "_file" metadata key before saving
        - Uses 2-space indentation
        - Ensures UTF-8 encoding
        - Adds trailing newline
    """
    # Create a clean copy without metadata
    clean_rule = {k: v for k, v in rule.items() if not k.startswith("_")}

    # Ensure parent directory exists
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Write with consistent formatting
    with file_path.open("w", encoding="utf-8") as handle:
        json.dump(clean_rule, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
