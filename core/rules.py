"""Rule evaluation helpers."""
from __future__ import annotations

import email
import json
import re
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso


def load_rules(rule_dir: Path, logger: JsonLogger) -> list[dict]:
    rule_dir = Path(rule_dir)
    rule_dir.mkdir(exist_ok=True)
    rules: list[dict] = []
    for path in sorted(rule_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                rule = json.load(handle)
            rule["_file"] = path.name
            rule["priority"] = int(rule.get("priority", 100))
            rules.append(rule)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.log(
                "ERROR",
                "rule_load_failed",
                {"file": str(path), "error": str(exc)},
                console=f"❌ Failed to load {path.name}",
            )
    logger.log("INFO", "rules_loaded", {"count": len(rules)}, console=f"📜 Loaded {len(rules)} rules")
    return rules


def rule_match(header: dict, cond: dict) -> bool:
    value = header.get(cond.get("header", "").lower(), "") or ""
    pattern = cond.get("contains") or cond.get("regex")
    if not pattern:
        return False
    if "regex" in cond:
        return bool(re.search(pattern, value, re.I))
    return pattern.lower() in value.lower()


def evaluate_rules(db, rules: Sequence[dict], scope: str, cfg, logger: JsonLogger) -> tuple[PhaseTimer, int, int]:
    rule_list = list(rules)
    show = cfg.logging.show_progress
    timer = PhaseTimer("evaluate")

    cur = db.cursor()
    cur.execute("SELECT uid, folder, data FROM headers")
    rows = cur.fetchall()

    folder_groups: dict[str, list[tuple[str, str]]] = {}
    for uid, folder, data in rows:
        folder_groups.setdefault(folder, []).append((uid, data))

    folders_bar = tqdm(
        folder_groups.items(),
        desc="🧩 Evaluating folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show,
    )

    total_matches = 0
    for folder, msgs in folders_bar:
        folders_bar.set_postfix_str(folder)
        msgs_bar = tqdm(
            msgs,
            desc=f"   🎯 Checking {folder}",
            unit="msg",
            dynamic_ncols=True,
            leave=False,
            position=1,
            disable=not show,
        )

        for uid, data in msgs_bar:
            hdr = json.loads(data)["header"]
            header = {k.lower(): v for k, v in email.message_from_string(hdr).items()}

            for rule in rule_list:
                if scope == "inbox" and not folder.lower().endswith("inbox"):
                    continue

                conds = rule.get("conditions", [])
                if conds and all(rule_match(header, cond) for cond in conds):
                    action = rule.get("action", {})
                    db.execute(
                        "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            uid,
                            folder,
                            rule.get("name"),
                            action.get("target", ""),
                            int(rule.get("priority", 100)),
                            "pending" if not cfg.executor.dry_run else "simulated",
                            now_iso(),
                        ),
                    )
                    total_matches += 1
                    logger.log(
                        "INFO",
                        "rule_match",
                        {
                            "rule": rule.get("name"),
                            "priority": int(rule.get("priority", 100)),
                            "folder": folder,
                            "uid": uid,
                            "target": action.get("target"),
                            "dry_run": cfg.executor.dry_run,
                        },
                    )

        db.commit()

    timer.stop()
    timer.count = total_matches
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "evaluate",
            "rules": len(rule_list),
            "matches": total_matches,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Evaluate Rules\n"
            f"   🧩  Rules evaluated: {len(rule_list)}\n"
            f"   🎯  Matches found: {total_matches}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )
    return timer, len(rule_list), total_matches
