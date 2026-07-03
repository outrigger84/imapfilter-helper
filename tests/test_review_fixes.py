"""Tests for the 2026-07 code-review correctness fixes.

Covers:
- find_matching_rule threading flags/date through to condition evaluation
- coverage analyzer using executor-consistent priority order and metadata
- RuleValidator detection of partially-evaluated condition dicts
- prune_empty_folders dry-run listing candidates without deleting
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.imap_client import prune_empty_folders
from core.rule_engine import find_matching_rule
from core.rule_validator import RuleValidator
from core.stream_processor import StreamMessage


# ---------------------------------------------------------------------------
# find_matching_rule: flags and date must reach condition evaluation
# ---------------------------------------------------------------------------


def test_find_matching_rule_evaluates_flag_conditions():
    rule = {
        "name": "keyword rule",
        "priority": 10,
        "conditions": [{"has_keyword": "newsletter"}],
    }
    header = {"from": "sender@example.com"}

    assert find_matching_rule(header, [rule], flags=["newsletter"]) is rule
    assert find_matching_rule(header, [rule], flags=["other"]) is None
    # Without flags the condition cannot be evaluated and must not match.
    assert find_matching_rule(header, [rule]) is None


def test_find_matching_rule_evaluates_age_conditions():
    rule = {
        "name": "age rule",
        "priority": 10,
        "conditions": [{"age_days_gt": 30}],
    }
    header = {"from": "sender@example.com"}
    old_date = datetime.now(timezone.utc) - timedelta(days=90)
    new_date = datetime.now(timezone.utc) - timedelta(days=5)

    assert find_matching_rule(header, [rule], date=old_date) is rule
    assert find_matching_rule(header, [rule], date=new_date) is None
    assert find_matching_rule(header, [rule]) is None


def test_find_matching_rule_first_match_wins_on_sorted_rules():
    specific = {
        "name": "specific",
        "priority": 10,
        "conditions": [{"header": "from", "contains": "@example.com"}],
    }
    broad = {
        "name": "broad",
        "priority": 100,
        "conditions": [{"header": "from", "contains": "@"}],
    }
    header = {"from": "sender@example.com"}
    sorted_rules = sorted([broad, specific], key=lambda r: int(r.get("priority", 100)))

    assert find_matching_rule(header, sorted_rules) is specific


# ---------------------------------------------------------------------------
# Coverage analyzer: executor-consistent precedence and full metadata
# ---------------------------------------------------------------------------


def _write_rule(rules_dir: Path, name: str, priority: int, conditions) -> None:
    (rules_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "priority": priority,
                "conditions": conditions,
                "actions": [{"type": "move", "target": "Archive"}],
            }
        )
    )


def test_coverage_analyzer_matches_executor_precedence(tmp_path: Path):
    import sqlite3

    from core.tools.coverage_analyzer import RuleCoverageAnalyzer

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    # Overlapping rules: lower priority number must win, as in evaluate_rules.
    _write_rule(rules_dir, "specific", 10, [{"header": "from", "contains": "@example.com"}])
    _write_rule(rules_dir, "broad", 100, [{"header": "from", "contains": "@"}])
    # Rule that only matches via flags — regression check for metadata loss.
    _write_rule(rules_dir, "flagged", 5, [{"has_keyword": "special"}])

    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE headers (folder TEXT, uid TEXT, data TEXT, updated_at TEXT)")
    plain = json.dumps({"header": "From: sender@example.com\n\n", "flags": []})
    special = json.dumps({"header": "From: other@example.com\n\n", "flags": ["special"]})
    conn.execute("INSERT INTO headers VALUES ('INBOX', '1', ?, NULL)", (plain,))
    conn.execute("INSERT INTO headers VALUES ('INBOX', '2', ?, NULL)", (special,))
    conn.commit()
    conn.close()

    analyzer = RuleCoverageAnalyzer(db_path=db_path, rules_dir=rules_dir)
    stats = analyzer.analyze_coverage()

    assert stats.coverage_by_rule.get("specific") == 1, (
        "overlap must be attributed to the lower-priority-number rule, "
        f"got {dict(stats.coverage_by_rule)}"
    )
    assert stats.coverage_by_rule.get("flagged") == 1, (
        "has_keyword rules must be evaluated with message flags, "
        f"got {dict(stats.coverage_by_rule)}"
    )
    assert "broad" not in stats.coverage_by_rule


# ---------------------------------------------------------------------------
# RuleValidator: partially-evaluated condition dicts
# ---------------------------------------------------------------------------


def test_validator_flags_header_mixed_with_flag_condition():
    validator = RuleValidator()
    rule = {
        "name": "mixed",
        "conditions": [
            {"header": "from", "contains": "x@example.com", "has_keyword": "seen-it"}
        ],
        "actions": [{"type": "move", "target": "Archive"}],
    }
    is_valid, warnings = validator.validate_rule(rule)
    assert not is_valid
    assert any("IGNORED" in w for w in warnings)


def test_validator_flags_multiple_operators_in_one_block():
    validator = RuleValidator()
    rule = {
        "name": "double-op",
        "conditions": {
            "all": [{"header": "from", "contains": "a", "not_contains": "b"}]
        },
        "actions": [{"type": "move", "target": "Archive"}],
    }
    is_valid, warnings = validator.validate_rule(rule)
    assert not is_valid
    assert any("only the first is evaluated" in w for w in warnings)


def test_validator_accepts_well_formed_rules():
    validator = RuleValidator()
    rule = {
        "name": "clean",
        "conditions": {
            "all": [
                {"header": "from", "contains": "@example.com"},
                {"has_keyword": "newsletter"},
                {"age_days_gt": 30},
            ]
        },
        "actions": [{"type": "move", "target": "Archive"}],
    }
    is_valid, warnings = validator.validate_rule(rule)
    assert is_valid, warnings


def test_validator_accepts_flag_and_age_in_same_block():
    # The engine ANDs flag and age keys in one dict correctly; only header
    # operators are dropped. This combination must not warn.
    validator = RuleValidator()
    rule = {
        "name": "flag-age",
        "conditions": [{"has_keyword": "newsletter", "age_days_gt": 30}],
        "actions": [{"type": "move", "target": "Archive"}],
    }
    is_valid, warnings = validator.validate_rule(rule)
    assert is_valid, warnings


# ---------------------------------------------------------------------------
# prune_empty_folders: dry-run lists candidates, never deletes
# ---------------------------------------------------------------------------


class _FakePruneClient:
    """Just enough IMAP surface for find_empty_prunable_folders."""

    def __init__(self):
        self.deleted: list[str] = []

    def list(self, directory='""', pattern="*"):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren) "/" "Archive"',
            b'(\\HasNoChildren) "/" "Archive/Empty"',
            b'(\\HasNoChildren) "/" "Archive/Full"',
        ]

    def status(self, mailbox, items):
        counts = {'"INBOX"': 5, '"Archive"': 0, '"Archive/Empty"': 0, '"Archive/Full"': 3}
        name = mailbox.strip('"')
        return "OK", [f"{name} (MESSAGES {counts[mailbox]})".encode()]

    def delete(self, mailbox):
        self.deleted.append(mailbox)
        return "OK", [b"Delete completed"]


def test_prune_dry_run_lists_candidates_without_deleting(capsys):
    client = _FakePruneClient()
    prune_empty_folders(client, auto=True, dry_run=True)

    out = capsys.readouterr().out
    assert "Archive/Empty" in out, "dry-run must name the folders it would delete"
    assert "No folders were deleted" in out
    assert client.deleted == []


def test_prune_deletes_when_not_dry_run():
    client = _FakePruneClient()
    prune_empty_folders(client, auto=True, dry_run=False)
    assert '"Archive/Empty"' in client.deleted
    assert '"INBOX"' not in client.deleted
    assert '"Archive/Full"' not in client.deleted


# ---------------------------------------------------------------------------
# StreamMessage: extended shape stays backward compatible
# ---------------------------------------------------------------------------


def test_stream_message_defaults_for_legacy_constructors():
    msg = StreamMessage(folder="INBOX", uid="1", header_text="From: a@b\n\n")
    assert msg.flags == []
    assert msg.internaldate is None
