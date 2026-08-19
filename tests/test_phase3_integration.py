"""
Integration test for age-based conditions and age-gated actions.
Tests the full rule evaluation pipeline (evaluate_rules) end to end,
including bracket expansion, first-bracket-wins, and the "coverage gap ->
fall through to the next rule" behavior.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    db = sqlite3.connect(str(db_path))
    db.execute(
        "CREATE TABLE headers ("
        "folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
        "PRIMARY KEY (folder, uid))"
    )
    db.execute(
        "CREATE TABLE actions ("
        "uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
        "priority INTEGER, status TEXT, created_at TEXT, "
        "action_type TEXT, action_data TEXT)"
    )
    return db


def test_age_gated_actions_end_to_end():
    """
    Full pipeline test: age-gated action brackets (the 2FA no-op pattern and
    the retention move-by-age pattern), a coverage-gap fall-through to a
    lower-priority rule, and plain (non-bracket) backward compatibility --
    all through evaluate_rules against a real actions table.
    """
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = _make_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)
        recent_date = now - timedelta(days=30)
        young_date = now - timedelta(hours=2)
        few_days_date = now - timedelta(days=3)

        messages = [
            # UID 1: young 2FA code -> bracket fires with do:[] (deliberate
            # no-op) -- must be consumed by this rule, not fall through to
            # the lower-priority catch-all.
            ("INBOX", "1", json.dumps({
                "header": "From: noreply@example.com\nSubject: Your verification code\n\n",
                "internaldate": young_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            # UID 2: old 2FA code -> second bracket fires -> real move action.
            ("INBOX", "2", json.dumps({
                "header": "From: noreply@example.com\nSubject: Your verification code\n\n",
                "internaldate": few_days_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            # UID 3: old newsletter -> single-bracket retention rule fires.
            ("INBOX", "3", json.dumps({
                "header": "From: news@example.com\nSubject: Monthly Newsletter\n\n",
                "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            # UID 4: recent newsletter -> the retention rule's only bracket
            # doesn't cover this age (coverage gap) -> falls through to the
            # lower-priority catch-all rule instead.
            ("INBOX", "4", json.dumps({
                "header": "From: news@example.com\nSubject: Monthly Newsletter\n\n",
                "internaldate": recent_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            # UID 5: legacy header-only format (no flags/date) -- plain,
            # non-bracket rule, backward compatibility.
            ("INBOX", "5", json.dumps({
                "header": "From: old@example.com\nSubject: Legacy Update\n\n",
            }), "2024-01-01T00:00:00Z"),
            # UID 6: plain old-format string, matches nothing.
            ("INBOX", "6", "From: ancient@example.com\nSubject: Ancient Email\n\n", "2024-01-01T00:00:00Z"),
        ]
        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            messages,
        )
        db.commit()

        rules = [
            {
                "name": "2FA Codes",
                "priority": 50,
                "conditions": {"header": "subject", "contains": "verification code"},
                "actions": [
                    {"age_days_lt": 1, "do": []},
                    {"age_days_gte": 1, "do": [{"type": "move", "target": "Deleted Messages"}]},
                ],
            },
            {
                "name": "Catch All 2FA",
                "priority": 60,
                "conditions": {"header": "subject", "contains": "verification code"},
                "action": {"type": "move", "target": "INBOX/Unsorted"},
            },
            {
                "name": "Archive Old Newsletters",
                "priority": 100,
                "conditions": {"header": "subject", "contains": "Newsletter"},
                "actions": [
                    {"age_days_gt": 365, "do": [{"type": "move", "target": "Archive/Newsletters"}]},
                ],
            },
            {
                "name": "Catch All Newsletters",
                "priority": 110,
                "conditions": {"header": "subject", "contains": "Newsletter"},
                "action": {"type": "move", "target": "INBOX/Recent"},
            },
            {
                "name": "Legacy Rule",
                "priority": 120,
                "conditions": {"header": "subject", "contains": "Legacy"},
                "action": {"type": "move", "target": "Archive"},
            },
        ]

        logger = JsonLogger(tmp_path / "test.log")
        timer, rule_count, match_count = evaluate_rules(
            db, rules, scope="all", dry_run=True, show_progress=False, logger=logger, verbose=False
        )

        cur = db.cursor()
        cur.execute("SELECT uid, rule_name, target FROM actions ORDER BY uid")
        actions = cur.fetchall()
        action_dict = {uid: (rule, target) for uid, rule, target in actions}

        # UID 1: matched (consumed by "2FA Codes"), but its do:[] bracket
        # produces zero action rows -- and critically, "Catch All 2FA" must
        # NOT have fired.
        assert "1" not in action_dict

        assert action_dict["2"] == ("2FA Codes", "Deleted Messages")
        assert action_dict["3"] == ("Archive Old Newsletters", "Archive/Newsletters")
        # UID 4 falls through the retention rule's coverage gap to the catch-all.
        assert action_dict["4"] == ("Catch All Newsletters", "INBOX/Recent")
        assert action_dict["5"] == ("Legacy Rule", "Archive")
        assert "6" not in action_dict

        assert len(actions) == 4, f"Expected 4 action rows, got {len(actions)}: {actions}"
        # 5 emails matched a rule (1-5); UID 1's match just produced no action.
        assert match_count == 5, f"Expected 5 matched emails, got {match_count}"

        # UID 1's no-op bracket match must still show up in the digest
        # breakdown (--notification-summary), not silently vanish just
        # because it produced zero action rows.
        phase_summary = None
        for line in (tmp_path / "test.log").read_text().splitlines():
            rec = json.loads(line)
            if rec.get("message") == "phase_summary":
                phase_summary = rec["context"]
        assert phase_summary is not None
        breakdown = phase_summary["matches_by_rule_and_target"]
        assert breakdown["2FA Codes"] == {"(no action)": 1, "Deleted Messages": 1}

        db.close()


def test_complex_logical_operators_with_age():
    """Nested ANY/ALL header conditions combined with age, unrelated to
    action-level bracketing (condition-level age gating is unchanged)."""
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = _make_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)

        messages = [
            ("INBOX", "1", json.dumps({
                "header": "Subject: Important Newsletter\n\n",
                "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            ("INBOX", "2", json.dumps({
                "header": "Subject: Critical Update\n\n",
                "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            ("INBOX", "3", json.dumps({
                "header": "Subject: Recent Newsletter\n\n",
                "internaldate": now.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
        ]
        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            messages,
        )
        db.commit()

        rules = [
            {
                "name": "Archive Old Important Content",
                "priority": 100,
                "conditions": {
                    "all": [
                        {
                            "any": [
                                {"header": "subject", "contains": "Newsletter"},
                                {"header": "subject", "contains": "Critical"},
                            ]
                        },
                        {"age_days_gt": 365},
                    ]
                },
                "action": {"type": "move", "target": "Archive/Important"},
            }
        ]

        logger = JsonLogger(tmp_path / "test.log")
        evaluate_rules(db, rules, scope="all", dry_run=True, show_progress=False, logger=logger, verbose=False)

        cur = db.cursor()
        cur.execute("SELECT uid FROM actions ORDER BY uid")
        matched_uids = [row[0] for row in cur.fetchall()]

        assert matched_uids == ["1", "2"]

        db.close()


def test_evaluate_logs_matches_by_rule_and_target():
    """The phase_summary event's matches_by_rule_and_target breakdown --
    consumed by --notification-summary's evaluate digest -- must reflect
    which bracket's target each message actually resolved to, not just a
    per-rule total."""
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db = _make_db(tmp_path)

        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)
        recent_date = now - timedelta(days=30)

        messages = [
            ("The Economist", "1", json.dumps({
                "header": "From: news@economist.com\n\n",
                "internaldate": old_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
            ("The Economist", "2", json.dumps({
                "header": "From: news@economist.com\n\n",
                "internaldate": recent_date.strftime("%d-%b-%Y %H:%M:%S +0000"),
            }), "2024-01-01T00:00:00Z"),
        ]
        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            messages,
        )
        db.commit()

        rules = [
            {
                "name": "The Economist",
                "priority": 90,
                "conditions": {"header": "from", "contains": "@economist.com"},
                "actions": [
                    {"age_days_lte": 365, "do": [{"type": "move", "target": "The Economist"}]},
                    {"age_days_gt": 365, "do": [{"type": "move", "target": "Deleted Messages"}]},
                ],
            }
        ]

        log_path = tmp_path / "test.log"
        logger = JsonLogger(log_path)
        evaluate_rules(db, rules, scope="all", dry_run=True, show_progress=False, logger=logger, verbose=False)

        phase_summary = None
        for line in log_path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("message") == "phase_summary":
                phase_summary = rec["context"]
        assert phase_summary is not None

        breakdown = phase_summary["matches_by_rule_and_target"]
        assert breakdown == {
            "The Economist": {
                "Deleted Messages": 1,  # uid 1, >365 days old
            }
        }

        # uid 2 (<=365 days old) is a same-folder no-op -- already filed in
        # "The Economist", so it's reported separately rather than mixed
        # into the real match/action breakdown above.
        noop_breakdown = phase_summary["noop_matches_by_rule_and_target"]
        assert noop_breakdown == {
            "The Economist": {
                "The Economist": 1,
            }
        }
        assert phase_summary["noop_matches"] == 1

        db.close()
