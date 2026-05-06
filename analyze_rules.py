#!/usr/bin/env python3
"""
Read-only analysis of imapfilter-helper rules.
Detects shadowing conflicts and structural anomalies.
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict
from typing import Any

RULES_DIR = pathlib.Path("/Users/stephenjgibson/imapfilter-helper/rules")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_rules_raw() -> list[dict]:
    """Load every .json rule file. Return ALL rules (enabled and disabled)."""
    rules = []
    for path in sorted(RULES_DIR.glob("*.json")):
        try:
            rule = json.loads(path.read_text(encoding="utf-8"))
            rule["_file"] = path.name
            rule["_path"] = str(path)
            rule["priority"] = int(rule.get("priority", 100))
            rules.append(rule)
        except Exception as exc:
            print(f"  [LOAD ERROR] {path.name}: {exc}")
    return rules


def get_targets(rule: dict) -> list[str]:
    """Extract all move-target strings from a rule's actions."""
    targets = []
    # "actions" array (canonical)
    for act in rule.get("actions", []):
        if isinstance(act, dict) and act.get("type", "move") == "move":
            t = act.get("target", "")
            if t:
                targets.append(t)
    # "action" singular (legacy)
    act = rule.get("action")
    if isinstance(act, dict) and act.get("type", "move") == "move":
        t = act.get("target", "")
        if t:
            targets.append(t)
    return targets


def extract_from_contains(node: Any) -> list[str]:
    """
    Recursively walk a condition tree and collect every
    `{"header": "from", "contains": <value>}` string.
    """
    results: list[str] = []
    if isinstance(node, list):
        for item in node:
            results.extend(extract_from_contains(item))
    elif isinstance(node, dict):
        # Direct leaf condition?
        hdr = node.get("header", "").lower()
        if hdr == "from" and "contains" in node:
            results.append(node["contains"])
        # Walk logical wrappers
        for key in ("all", "any", "not"):
            child = node.get(key)
            if child is not None:
                results.extend(extract_from_contains(child))
    return results


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def main() -> None:
    all_rules = load_rules_raw()
    total = len(all_rules)

    # -- Structural anomalies (check ALL rules, including disabled) ----------
    rules_with_singular_action = []
    rules_missing_enabled = []

    for r in all_rules:
        fname = r["_file"]
        name = r.get("name", "(unnamed)")
        if "action" in r and "actions" not in r:
            rules_with_singular_action.append((fname, name))
        if "enabled" not in r:
            rules_missing_enabled.append((fname, name))

    # -- Filter to enabled-only for conflict analysis -------------------------
    enabled = [r for r in all_rules if r.get("enabled", True)]
    disabled_count = total - len(enabled)

    # Sort by (priority, filename) exactly as load_rules does
    enabled_sorted = sorted(enabled, key=lambda r: (r["priority"], r["_file"]))

    # Pre-compute per-rule data
    # rule_data[i] = {"file", "name", "priority", "patterns", "targets"}
    rule_data = []
    for r in enabled_sorted:
        patterns = extract_from_contains(r.get("conditions"))
        targets = get_targets(r)
        rule_data.append({
            "file": r["_file"],
            "name": r.get("name", "(unnamed)"),
            "priority": r["priority"],
            "patterns": patterns,
            "targets": targets,
            "rule": r,
        })

    # -- Shadowing conflict detection -----------------------------------------
    # Group rules by priority
    by_priority: dict[int, list[dict]] = defaultdict(list)
    for rd in rule_data:
        by_priority[rd["priority"]].append(rd)

    shadowing_conflicts = []

    for priority, group in sorted(by_priority.items()):
        # Within this priority group the engine sees them in filename order
        # (already sorted). For every pair (i < j) check if rule_i shadows rule_j.
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a = group[i]  # earlier filename → fires first
                b = group[j]  # later filename  → potentially shadowed

                # Only interesting when they route to different places
                targets_a = set(a["targets"])
                targets_b = set(b["targets"])
                if not targets_a or not targets_b:
                    continue
                if targets_a == targets_b:
                    continue  # same target – shadow doesn't matter

                # Check if any pattern in A is a substring of any pattern in B
                for pa in a["patterns"]:
                    for pb in b["patterns"]:
                        # A's pattern is a non-empty suffix-match substring of B's pattern
                        # meaning: pa in pb AND pa != pb (A is broader)
                        if pa and pb and pa.lower() in pb.lower() and pa.lower() != pb.lower():
                            shadowing_conflicts.append({
                                "priority": priority,
                                "shadow_file": a["file"],
                                "shadow_name": a["name"],
                                "shadow_pattern": pa,
                                "shadow_targets": sorted(targets_a),
                                "victim_file": b["file"],
                                "victim_name": b["name"],
                                "victim_pattern": pb,
                                "victim_targets": sorted(targets_b),
                            })

    # -- Cross-priority ambiguities -------------------------------------------
    # Same pattern appears in rules at different priorities targeting different folders.
    # These aren't bugs per se (higher-priority rule intentionally wins) but worth noting
    # if the lower-priority rule would never fire for that pattern.
    pattern_appearances: dict[str, list[dict]] = defaultdict(list)
    for rd in rule_data:
        for pat in rd["patterns"]:
            pattern_appearances[pat.lower()].append(rd)

    cross_priority_ambiguities = []
    for pat, appearances in sorted(pattern_appearances.items()):
        if len(appearances) < 2:
            continue
        # Only flag if they differ in both priority AND target
        all_priorities = {a["priority"] for a in appearances}
        all_targets = {t for a in appearances for t in a["targets"]}
        if len(all_priorities) > 1 and len(all_targets) > 1:
            cross_priority_ambiguities.append((pat, appearances))

    # -------------------------------------------------------------------------
    # REPORT
    # -------------------------------------------------------------------------
    sep = "=" * 72

    print(sep)
    print("  IMAPFILTER-HELPER  —  RULE ANALYSIS REPORT")
    print(sep)
    print(f"  Rules directory : {RULES_DIR}")
    print(f"  Total .json files: {total}  |  Enabled: {len(enabled)}  |  Disabled: {disabled_count}")
    print()

    # ── Section 1: Shadowing conflicts ────────────────────────────────────────
    print(sep)
    print("  SECTION 1: SHADOWING CONFLICTS  (same priority, A fires before B)")
    print(sep)
    if not shadowing_conflicts:
        print("  No shadowing conflicts detected.\n")
    else:
        print(f"  {len(shadowing_conflicts)} conflict(s) found:\n")
        for idx, c in enumerate(shadowing_conflicts, 1):
            print(f"  [{idx}] Priority {c['priority']}")
            print(f"      SHADOW  : {c['shadow_file']}")
            print(f"        Name  : {c['shadow_name']}")
            print(f"        Pattern: \"{c['shadow_pattern']}\"")
            print(f"        Target : {', '.join(c['shadow_targets'])}")
            print(f"      VICTIM  : {c['victim_file']}")
            print(f"        Name  : {c['victim_name']}")
            print(f"        Pattern: \"{c['victim_pattern']}\"")
            print(f"        Target : {', '.join(c['victim_targets'])}")
            print(f"      → Any email matching \"{c['victim_pattern']}\" also matches")
            print(f"        \"{c['shadow_pattern']}\" — the shadow rule fires first.")
            print()

    # ── Section 2: Singular "action" key ─────────────────────────────────────
    print(sep)
    print("  SECTION 2: RULES USING \"action\" (singular) INSTEAD OF \"actions\"")
    print(sep)
    if not rules_with_singular_action:
        print("  None found.\n")
    else:
        print(f"  {len(rules_with_singular_action)} rule(s) use the singular form:\n")
        for fname, name in rules_with_singular_action:
            print(f"    • {fname}")
            print(f"      Name: {name}")
        print()

    # ── Section 3: Missing "enabled" field ───────────────────────────────────
    print(sep)
    print("  SECTION 3: RULES MISSING \"enabled\" FIELD")
    print(sep)
    if not rules_missing_enabled:
        print("  None found.\n")
    else:
        print(f"  {len(rules_missing_enabled)} rule(s) lack an explicit \"enabled\" key")
        print("  (treated as enabled=true by the engine):\n")
        for fname, name in rules_missing_enabled:
            print(f"    • {fname}")
            print(f"      Name: {name}")
        print()

    # ── Section 4: Cross-priority ambiguities ─────────────────────────────────
    print(sep)
    print("  SECTION 4: CROSS-PRIORITY AMBIGUITIES")
    print("  (same pattern in multiple rules at different priorities → different targets)")
    print(sep)
    if not cross_priority_ambiguities:
        print("  None found.\n")
    else:
        print(f"  {len(cross_priority_ambiguities)} pattern(s) appear in multiple rules at different priorities:\n")
        for pat, appearances in cross_priority_ambiguities:
            print(f"  Pattern: \"{pat}\"")
            for a in sorted(appearances, key=lambda x: (x["priority"], x["file"])):
                targets_str = ", ".join(a["targets"]) if a["targets"] else "(no move target)"
                print(f"    priority={a['priority']}  {a['file']}")
                print(f"      Name: {a['name']}")
                print(f"      Target: {targets_str}")
            print()

    print(sep)
    print("  END OF REPORT")
    print(sep)


if __name__ == "__main__":
    main()
