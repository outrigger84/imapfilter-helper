from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import init_db
from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules


@pytest.fixture()
def rule_test_env(tmp_path: Path):
    db_path = tmp_path / "rules.db"
    log_path = tmp_path / "log.jsonl"

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    header = "From: newsletter@example.com\nSubject: Urgent update\n\n"
    db.execute(
        "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
        ("1", "INBOX", json.dumps({"header": header}), None),
    )
    db.commit()

    try:
        yield db, logger
    finally:
        db.close()


def _run_rule(db, logger, rule):
    return evaluate_rules(
        db,
        [rule],
        scope="all",
        dry_run=True,
        show_progress=False,
        logger=logger,
    )


def test_evaluate_rules_with_all_conditions(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "AND rule",
        "conditions": [
            {"header": "from", "contains": "newsletter@example.com"},
            {"header": "subject", "contains": "Urgent"},
        ],
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_with_any_conditions(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "OR rule",
        "conditions": {
            "any": [
                {"header": "subject", "contains": "Digest"},
                {"header": "from", "contains": "newsletter@example.com"},
            ]
        },
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_with_mixed_logic(rule_test_env):
    db, logger = rule_test_env
    rule = {
        "name": "Mixed logic rule",
        "conditions": {
            "all": [
                {
                    "any": [
                        {"header": "from", "contains": "alerts@example.com"},
                        {"header": "from", "contains": "newsletter@example.com"},
                    ]
                },
                {"header": "subject", "contains": "update"},
            ]
        },
        "action": {"type": "move", "target": "Newsletters"},
    }

    _timer, count, matches = _run_rule(db, logger, rule)

    assert count == 1
    assert matches == 1


def test_evaluate_rules_streaming_large_dataset(tmp_path: Path):
    db_path = tmp_path / "large_rules.db"
    log_path = tmp_path / "large_log.jsonl"

    logger = JsonLogger(log_path)
    db = init_db(db_path, logger=logger)

    try:
        total_folders = 5
        messages_per_folder = 300
        expected_matches = 0

        for folder_idx in range(total_folders):
            folder = f"Folder-{folder_idx}"
            for msg_idx in range(messages_per_folder):
                subject = "Match me" if msg_idx % 10 == 0 else "Other"
                header = f"From: sender{folder_idx}@example.com\nSubject: {subject}\n\n"
                db.execute(
                    "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
                    (
                        f"{folder_idx}-{msg_idx}",
                        folder,
                        json.dumps({"header": header}),
                        None,
                    ),
                )
                if subject == "Match me":
                    expected_matches += 1

        db.commit()

        rule = {
            "name": "Subject contains match",
            "conditions": {"header": "subject", "contains": "Match me"},
            "action": {"type": "move", "target": "Matches"},
        }

        _timer, count, matches = evaluate_rules(
            db,
            [rule],
            scope="all",
            dry_run=True,
            show_progress=False,
            logger=logger,
        )

        assert count == 1
        assert matches == expected_matches

        cursor = db.cursor()
        cursor.execute("SELECT COUNT(*) FROM actions")
        (action_count,) = cursor.fetchone()
        assert action_count == expected_matches
    finally:
        db.close()
