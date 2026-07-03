#!/usr/bin/env python3
"""
Analyse historical rules (rules.zip snapshot + Thunderbird export) against
unmatched emails in data/cache.db. Reports which historical rules would match
the most currently-unmatched emails, helping prioritise rule creation.

Usage:
    python analyze_historical_rules.py
    python analyze_historical_rules.py --min-matches 5
    python analyze_historical_rules.py --generate-rules --min-matches 10
    python analyze_historical_rules.py --generate-rules --dry-run
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

RULES_DIR = Path(__file__).parent / "rules"
RULES_ZIP = Path(__file__).parent / "rules.zip"
DB_PATH = Path(__file__).parent / "data" / "cache.db"
THUNDERBIRD_COMMIT = "ea19cd6"
ZIP_PREFIX = "rules-tmp/"
ACCOUNT_PREFIX_RE = re.compile(r'^[\w.@]+ » ')


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def load_current_rule_names() -> set[str]:
    names: set[str] = set()
    for path in RULES_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data.get("name", "")
            if name:
                names.add(name)
        except (json.JSONDecodeError, OSError):
            pass
    return names


def load_zip_rules() -> list[dict]:
    rules: list[dict] = []
    if not RULES_ZIP.exists():
        return rules
    with zipfile.ZipFile(RULES_ZIP) as z:
        for entry in z.namelist():
            if entry.startswith(ZIP_PREFIX) and entry.endswith(".json"):
                try:
                    rules.append(json.loads(z.read(entry)))
                except (json.JSONDecodeError, KeyError):
                    pass
    return rules


def _strip_account_prefix(name: str) -> str:
    return ACCOUNT_PREFIX_RE.sub("", name)


def parse_thunderbird_rules(content: str) -> list[dict]:
    """Parse Thunderbird msgFilterRules.dat into a list of raw rule dicts."""
    rules: list[dict] = []
    current: dict = {}
    for line in content.splitlines():
        line = line.strip()
        m = re.match(r'^(\w+)="(.*)"$', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "name":
            if current:
                rules.append(current)
            current = {"name": val, "action_list": [], "action_values": [], "conditions_raw": ""}
        elif key == "action":
            current.setdefault("action_list", []).append(val)
        elif key == "actionValue":
            current.setdefault("action_values", []).append(val)
        elif key == "condition":
            current["conditions_raw"] = val
        elif key == "enabled":
            current["enabled"] = (val == "yes")
    if current:
        rules.append(current)
    return rules


def thunderbird_to_json_rule(tb: dict) -> dict | None:
    """Convert a parsed Thunderbird rule to the JSON rule schema."""
    name = _strip_account_prefix(tb.get("name", ""))

    # Find first "Move to folder" action value
    target = ""
    for av in tb.get("action_values", []):
        m = re.search(r'imap://[^/]+/(.+)$', av)
        if m:
            target = m.group(1)
            break
    if not target:
        return None

    # Parse conditions
    cond_raw = tb.get("conditions_raw", "")
    tuples = re.findall(r'\(([^,]+),([^,)]+),?([^)]*)\)', cond_raw)

    conditions: list[dict] = []
    for field, op, value in tuples:
        field = field.strip().lower()
        op = op.strip().lower()
        value = value.strip()
        if field in ("from", "to", "cc", "subject"):
            if op in ("contains", "ends with"):
                if value:  # skip empty values
                    conditions.append({"header": field, "contains": value})
            elif op in ("doesn't contain", "does not contain"):
                if value:
                    conditions.append({"header": field, "not_contains": value})
        # age-based conditions skipped intentionally

    if not conditions:
        return None

    logic = "all" if cond_raw.startswith("AND") else "any"

    return {
        "name": name,
        "enabled": True,
        "priority": 100,
        "conditions": {logic: conditions},
        "actions": [{"type": "move", "target": target}],
        "comments": [f"Imported from Thunderbird msgFilterRules.dat (commit {THUNDERBIRD_COMMIT})"],
    }


# ---------------------------------------------------------------------------
# Cache DB helpers
# ---------------------------------------------------------------------------

def extract_from_header(raw_header: str) -> str:
    """Extract the From: value from raw IMAP header text (handles folded headers)."""
    from_val = ""
    in_from = False
    for line in raw_header.split("\n"):
        if re.match(r'^[Ff]rom\s*:', line):
            from_val = line.split(":", 1)[1].strip()
            in_from = True
        elif in_from and line and line[0] in (' ', '\t'):
            from_val += " " + line.strip()
        elif in_from:
            break
    return from_val.lower()


def load_from_counter(db_path: Path) -> Counter:
    """Return Counter of lowercased From: strings from all rows in headers."""
    counter: Counter = Counter()
    conn = sqlite3.connect(db_path)
    try:
        for (data_str,) in conn.execute("SELECT data FROM headers"):
            try:
                raw = json.loads(data_str).get("header", "")
                val = extract_from_header(raw)
                if val:
                    counter[val] += 1
            except (json.JSONDecodeError, KeyError):
                pass
    finally:
        conn.close()
    return counter


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _collect_from_patterns(rule: dict) -> tuple[str, list[str]]:
    """Return (logic, [pattern, ...]) for from-header conditions in a rule."""
    conditions = rule.get("conditions", {})
    logic = "any" if "any" in conditions else "all"
    patterns: list[str] = []
    for cond in conditions.get(logic, []):
        if cond.get("header", "").lower() == "from" and "contains" in cond:
            p = cond["contains"].strip().lower()
            if p:  # skip empty patterns — they would match every address
                patterns.append(p)
    return logic, patterns


def match_count(rule: dict, from_counter: Counter) -> tuple[int, list[str]]:
    """Return (total email count matched, top sample sender strings)."""
    logic, patterns = _collect_from_patterns(rule)
    if not patterns:
        return 0, []

    matched_count = 0
    sample_emails: Counter = Counter()

    for from_str, n in from_counter.items():
        if logic == "any":
            hit = any(p in from_str for p in patterns)
        else:
            hit = all(p in from_str for p in patterns)
        if hit:
            matched_count += n
            m = re.search(r'[\w.+%-]+@[\w.-]+', from_str)
            email_addr = m.group(0) if m else from_str[:60]
            sample_emails[email_addr] += n

    samples = [e for e, _ in sample_emails.most_common(5)]
    return matched_count, samples


# ---------------------------------------------------------------------------
# Rule file generation
# ---------------------------------------------------------------------------

def name_to_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r'\s*»\s*', '_', slug)
    slug = re.sub(r'[^a-z0-9_]', '_', slug)
    slug = re.sub(r'_+', '_', slug).strip('_')
    return slug[:80]


def generate_rule_file(rule: dict, dry_run: bool) -> Path:
    ts = int(time.time())
    path = RULES_DIR / f"{ts}_{name_to_slug(rule.get('name', 'rule'))}.json"
    if not dry_run:
        path.write_text(json.dumps(rule, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Find historical rules matching unmatched cache emails")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--min-matches", type=int, default=1, metavar="N",
                        help="Only include rules matching ≥N emails (default: 1)")
    parser.add_argument("--generate-rules", action="store_true",
                        help="Write new JSON rule files to rules/")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --generate-rules: preview without writing")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: {db_path} not found", file=sys.stderr)
        return 1

    # ── Current rules ──────────────────────────────────────────────────────
    print("Loading current rules...")
    current_names = load_current_rule_names()
    print(f"  {len(current_names):,} rules in rules/")

    # ── rules.zip gaps ─────────────────────────────────────────────────────
    print("\nLoading rules.zip...")
    zip_rules = load_zip_rules()
    zip_gaps = [r for r in zip_rules if r.get("name", "") not in current_names]
    print(f"  {len(zip_rules):,} rules in zip  →  {len(zip_gaps):,} not in current rules/")

    # ── Thunderbird gaps ────────────────────────────────────────────────────
    print("\nLoading Thunderbird rules from git...")
    tb_json_gaps: list[dict] = []
    try:
        result = subprocess.run(
            ["git", "show", f"{THUNDERBIRD_COMMIT}:msgFilterRules.dat"],
            capture_output=True, text=True,
            cwd=Path(__file__).parent, check=True,
        )
        tb_raw = parse_thunderbird_rules(result.stdout)
        print(f"  {len(tb_raw):,} Thunderbird rules parsed")
        zip_gap_names = {r.get("name", "") for r in zip_gaps}
        for tb in tb_raw:
            plain_name = _strip_account_prefix(tb.get("name", ""))
            if plain_name in current_names or plain_name in zip_gap_names:
                continue
            jr = thunderbird_to_json_rule(tb)
            if jr:
                tb_json_gaps.append(jr)
        print(f"  {len(tb_json_gaps):,} additional Thunderbird rules not covered elsewhere")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  Warning: could not load Thunderbird rules: {e}")

    all_gaps = zip_gaps + tb_json_gaps
    print(f"\nTotal gap rules to analyse: {len(all_gaps):,}")

    # ── Load From addresses ─────────────────────────────────────────────────
    print(f"\nReading emails from {db_path}...")
    from_counter = load_from_counter(db_path)
    total_emails = sum(from_counter.values())
    print(f"  {total_emails:,} emails, {len(from_counter):,} unique From addresses")

    # ── Match ───────────────────────────────────────────────────────────────
    print("\nMatching gap rules against emails...")
    results: list[tuple[int, dict, list[str]]] = []
    for rule in all_gaps:
        count, samples = match_count(rule, from_counter)
        if count >= args.min_matches:
            results.append((count, rule, samples))
    results.sort(key=lambda x: -x[0])
    print(f"  {len(results):,} rules with ≥{args.min_matches} match(es)")

    # ── Report ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"GAP RULES RANKED BY EMAIL MATCH COUNT  (min={args.min_matches})")
    print("=" * 72)

    for i, (count, rule, samples) in enumerate(results, 1):
        actions = rule.get("actions") or ([rule["action"]] if "action" in rule else [])
        target = next((a.get("target", "?") for a in actions if a.get("type") == "move"), "?")
        print(f"\n{i:4d}.  [{count:5,d}]  {rule.get('name', '?')}")
        print(f"        → {target}")
        if samples:
            print(f"        Senders: {', '.join(samples)}")

    total_matched = sum(c for c, _, _ in results)
    pct = 100 * total_matched / total_emails if total_emails else 0
    print(f"\nTotal emails that would be newly covered: {total_matched:,} / {total_emails:,} ({pct:.1f}%)")

    # ── Generate ─────────────────────────────────────────────────────────────
    if args.generate_rules:
        print("\n" + "=" * 72)
        label = "DRY RUN — " if args.dry_run else ""
        print(f"{label}GENERATING RULE FILES  ({len(results)} files)")
        print("=" * 72)
        for count, rule, _ in results:
            path = generate_rule_file(rule, dry_run=args.dry_run)
            verb = "would write" if args.dry_run else "wrote"
            print(f"  {verb}: {path.name}  [{count:,} matches]")
        if args.dry_run:
            print("\n(No files written — re-run without --dry-run to apply)")
        else:
            print(f"\n{len(results)} rule files written to {RULES_DIR}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
