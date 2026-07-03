#!/usr/bin/env python3
"""
Standardise finance-related rules to Personal/Finance/<grouping>/<organisation>.

Affected rule groups:
  Personal/Banking/*          → Personal/Finance/Banking/*
  Personal/Pensions/*         → Personal/Finance/Pensions/*
  Personal Finance/*          → Personal/Finance/*  (space → slash)
  Finance/Comparison/*        → Personal/Finance/Comparison/*

Updates both the 'name' field (» separated) and move action 'target' (/ separated).
Also handles older rules that carry a singular 'action' dict alongside 'actions'.

Usage:
    python migrate_finance.py --dry-run   # preview changes
    python migrate_finance.py             # apply changes
"""

import argparse
import json
import sys
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"

SEP = " » "  # " » "

# (name_prefix, new_name_prefix, target_prefix, new_target_prefix)
# More-specific entries must come before their prefixes.
MAPPINGS = [
    # Wizard-created banking rules: "Personal » Banking » X"
    ("Personal » Banking",
     "Personal » Finance » Banking",
     "Personal/Banking",
     "Personal/Finance/Banking"),

    # Thunderbird-imported banking rules: "Banking » X" (no Personal prefix)
    ("Banking",
     "Personal » Finance » Banking",
     "Personal/Banking",
     "Personal/Finance/Banking"),

    # Thunderbird-imported pensions rules: "Pensions" / "Pensions » X"
    ("Pensions",
     "Personal » Finance » Pensions",
     "Personal/Pensions",
     "Personal/Finance/Pensions"),

    # Old-style "Personal Finance » …" rules (space, not slash, after Personal)
    ("Personal Finance",
     "Personal » Finance",
     "Personal Finance",
     "Personal/Finance"),

    # Root-level Finance/Comparison rules
    ("Finance » Comparison",
     "Personal » Finance » Comparison",
     "Finance/Comparison",
     "Personal/Finance/Comparison"),
]


def find_mapping(name: str):
    for entry in MAPPINGS:
        name_prefix = entry[0]
        if name == name_prefix or name.startswith(name_prefix + " »"):
            return entry
    return None


def migrate_rule(data: dict, mapping) -> bool:
    name_prefix, new_name_prefix, target_prefix, new_target_prefix = mapping
    changed = False

    old_name = data.get("name", "")
    if old_name.startswith(name_prefix):
        new_name = new_name_prefix + old_name[len(name_prefix):]
        if new_name != old_name:
            data["name"] = new_name
            changed = True

    # Older rules have a singular 'action' dict as well as 'actions' list.
    # Both reference distinct dicts so each must be updated.
    all_actions = (
        [data["action"]] if isinstance(data.get("action"), dict) else []
    ) + data.get("actions", [])

    for action in all_actions:
        if action.get("type") == "move":
            old_target = action.get("target", "")
            if old_target.startswith(target_prefix):
                new_target = new_target_prefix + old_target[len(target_prefix):]
                if new_target != old_target:
                    action["target"] = new_target
                    changed = True

    return changed


def main():
    parser = argparse.ArgumentParser(
        description="Standardise finance rules to Personal/Finance/<grouping>/<org>"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    args = parser.parse_args()

    rule_files = sorted(RULES_DIR.glob("*.json"))
    changed_files = []
    skipped = 0
    folder_renames: dict[str, str] = {}

    for path in rule_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  WARN: could not read {path.name}: {e}", file=sys.stderr)
            continue

        name = data.get("name", "")
        mapping = find_mapping(name)
        if not mapping:
            skipped += 1
            continue

        _, _, target_prefix, new_target_prefix = mapping
        all_actions = (
            [data["action"]] if isinstance(data.get("action"), dict) else []
        ) + data.get("actions", [])
        for action in all_actions:
            if action.get("type") == "move":
                old_t = action.get("target", "")
                if old_t.startswith(target_prefix) and old_t not in folder_renames:
                    new_t = new_target_prefix + old_t[len(target_prefix):]
                    if new_t != old_t:
                        folder_renames[old_t] = new_t

        old_name = data.get("name", "")
        changed = migrate_rule(data, mapping)
        if changed:
            changed_files.append((path, old_name, data.get("name", "")))
            if not args.dry_run:
                path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Results:")
    print(f"  Rules updated : {len(changed_files)}")
    print(f"  Rules skipped : {skipped}")

    if changed_files:
        print("\nChanged rules:")
        for path, old_name, new_name in changed_files:
            print(f"  {path.name}")
            print(f"    {old_name}")
            print(f"    → {new_name}")

    if folder_renames:
        print(f"\nIMAP folders to rename on the server ({len(folder_renames)}):")
        for old, new in sorted(folder_renames.items()):
            print(f"  {old}")
            print(f"  → {new}")

    if args.dry_run:
        print("\n(No files written — re-run without --dry-run to apply)")


if __name__ == "__main__":
    main()
