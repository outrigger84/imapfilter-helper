"""Rule loading and evaluation helpers."""
from __future__ import annotations

import email
import json
import re
from pathlib import Path
from typing import Any, Sequence

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


def _extract_raw_header(data: str) -> str:
    """Return the raw header string from a row of header data."""

    if not data:
        return ""

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data

    if isinstance(payload, dict):
        header = payload.get("header")
        if isinstance(header, str):
            return header

    return data


def _parse_header_map(raw_header: str) -> dict[str, str]:
    """Parse the raw header string into a lowercase-keyed mapping."""

    message = email.message_from_string(raw_header or "")
    return {key.lower(): value for key, value in message.items()}


def rule_match(header: dict, cond: dict) -> bool:
    value = header.get(cond.get("header", "").lower(), "") or ""
    pattern = cond.get("contains") or cond.get("regex")
    if not pattern:
        return False
    if "regex" in cond:
        return bool(re.search(pattern, value, re.I))
    return pattern.lower() in value.lower()


def _evaluate_condition_node(header: dict, node: Any) -> bool:
    """Evaluate a condition or logical group against the header map."""

    if isinstance(node, list):
        # Implicit AND for backward compatibility with legacy rule format.
        return all(_evaluate_condition_node(header, item) for item in node)

    if isinstance(node, dict):
        matched = False

        if "all" in node:
            matched = True
            candidates = node.get("all")
            if not isinstance(candidates, list):
                candidates = [candidates]
            if not all(_evaluate_condition_node(header, item) for item in candidates):
                return False

        if "any" in node:
            matched = True
            candidates = node.get("any")
            if not isinstance(candidates, list):
                candidates = [candidates]
            # Empty OR group should never match.
            if not candidates:
                return False
            if not any(_evaluate_condition_node(header, item) for item in candidates):
                return False

        if matched:
            return True

        return rule_match(header, node)

    return False


def conditions_match(header: dict, conditions: Any) -> bool:
    """Return True if the header satisfies the supplied condition tree."""

    if not conditions:
        return False
    return _evaluate_condition_node(header, conditions)


def evaluate_rules(
    db,
    rules: Sequence[dict],
    *,
    scope: str,
    dry_run: bool,
    show_progress: bool,
    logger: JsonLogger,
    verbose: bool = False,
    debug_headers: bool = False,
    folders: Sequence[str] | None = None,
) -> tuple[PhaseTimer, int, int]:
    rule_list = list(rules)
    timer = PhaseTimer("evaluate")

    cur = db.cursor()

    normalized_scope = (scope or "all").lower()
    folder_filter = {folder: None for folder in folders} if folders else None

    def folder_allowed(folder_name: str) -> bool:
        if folder_filter is not None:
            return folder_name in folder_filter
        if normalized_scope == "inbox":
            return folder_name.lower().endswith("inbox")
        return True

    folder_totals: dict[str, int] = {}
    totals_cursor = db.cursor()
    totals_cursor.execute(
        "SELECT folder, COUNT(*) FROM headers GROUP BY folder ORDER BY folder"
    )
    for folder, count in totals_cursor.fetchall():
        if folder_allowed(folder):
            folder_totals[folder] = count

    cur.execute("SELECT uid, folder, data FROM headers ORDER BY folder, uid")

    logger.log(
        "INFO",
        "evaluate_log_hint",
        {"log_file": str(logger.log_file)},
        console=f"📝 Detailed logs: {logger.log_file}",
    )

    if verbose and folder_totals:
        overview = dict(sorted(folder_totals.items(), key=lambda kv: kv[0]))
        lines = "\n".join(f"      • {folder}: {count}" for folder, count in overview.items())
        logger.log(
            "INFO",
            "evaluate_overview",
            {"folders": overview, "total_messages": sum(folder_totals.values())},
            console=(
                "📂 Folders queued for evaluation:" + (f"\n{lines}" if lines else "")
            ),
        )

    total_matches = 0
    folder_match_counts: dict[str, int] = {}
    rule_match_counts: dict[str, int] = {}
    folders_bar = tqdm(
        total=len(folder_totals) if folder_totals else None,
        desc="🧩 Evaluating folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    current_folder: str | None = None
    current_folder_total = 0
    current_folder_count = 0
    msgs_bar: tqdm | None = None

    def _finalize_folder() -> None:
        nonlocal current_folder, current_folder_total, current_folder_count, msgs_bar
        if current_folder is None:
            return
        if msgs_bar is not None:
            msgs_bar.close()
            msgs_bar = None
        folders_bar.update(1)
        db.commit()
        if verbose:
            logger.log(
                "INFO",
                "evaluate_folder_complete",
                {
                    "folder": current_folder,
                    "messages": max(current_folder_total, current_folder_count),
                    "processed": current_folder_count,
                    "matches": folder_match_counts.get(current_folder, 0),
                },
                console=(
                    f"📦 Completed {current_folder}: "
                    f"{folder_match_counts.get(current_folder, 0)} matches"
                ),
            )
        current_folder = None
        current_folder_total = 0
        current_folder_count = 0

    chunk_size = 512
    while True:
        rows = cur.fetchmany(chunk_size)
        if not rows:
            break
        for uid, folder, data in rows:
            if folder != current_folder:
                _finalize_folder()
                if not folder_allowed(folder):
                    current_folder = None
                    current_folder_total = 0
                    current_folder_count = 0
                    continue
                current_folder = folder
                current_folder_total = folder_totals.get(folder, 0)
                current_folder_count = 0
                folders_bar.set_postfix_str(folder)
                msgs_bar = tqdm(
                    total=current_folder_total or None,
                    desc=f"   🎯 Checking {folder}",
                    unit="msg",
                    dynamic_ncols=True,
                    leave=False,
                    position=1,
                    disable=not show_progress,
                )
                if verbose:
                    logger.log(
                        "INFO",
                        "evaluate_folder_start",
                        {"folder": folder, "messages": current_folder_total},
                        console=(
                            f"🔍 Evaluating {folder} "
                            f"({current_folder_total} messages)"
                        ),
                    )

            if current_folder is None:
                continue

            raw_header = _extract_raw_header(data)
            header = _parse_header_map(raw_header)
            current_folder_count += 1
            if msgs_bar is not None:
                msgs_bar.update(1)

            if debug_headers:
                logger.log(
                    "DEBUG",
                    "header_debug",
                    {
                        "uid": uid,
                        "folder": folder,
                        "subject": header.get("subject"),
                        "from": header.get("from"),
                    },
                )

            for rule in rule_list:
                conds = rule.get("conditions")
                if conditions_match(header, conds):
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
                            "pending" if not dry_run else "simulated",
                            now_iso(),
                        ),
                    )
                    total_matches += 1
                    folder_match_counts[folder] = folder_match_counts.get(folder, 0) + 1
                    rule_name = rule.get("name") or "(unnamed)"
                    rule_match_counts[rule_name] = rule_match_counts.get(rule_name, 0) + 1
                    console_msg: str | None = None
                    if verbose:
                        target = action.get("target") or "(no target)"
                        console_msg = f"✅ {rule_name} matched {folder}/{uid} → {target}"
                    logger.log(
                        "INFO",
                        "rule_match",
                        {
                            "rule": rule.get("name"),
                            "priority": int(rule.get("priority", 100)),
                            "folder": folder,
                            "uid": uid,
                            "target": action.get("target"),
                            "dry_run": dry_run,
                        },
                        console=console_msg,
                    )

    _finalize_folder()
    folders_bar.close()
    timer.stop()
    timer.count = total_matches
    folder_summary = sorted(folder_match_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rule_summary = sorted(rule_match_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def _fmt_summary(title: str, entries: list[tuple[str, int]], limit: int = 5) -> str:
        if not entries:
            return ""
        shown = entries[:limit]
        lines = "\n".join(f"      • {name}: {count}" for name, count in shown)
        extra = ""
        remaining = len(entries) - len(shown)
        if remaining > 0:
            extra = f"\n      • … and {remaining} more"
        return f"   {title}\n{lines}{extra}\n"

    summary_console = (
        "\n📊 Summary — Evaluate Rules\n"
        f"   🧩  Rules evaluated: {len(rule_list)}\n"
        f"   🎯  Matches found: {total_matches}\n"
        f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        + _fmt_summary("📂  Matches by folder:", folder_summary)
        + _fmt_summary("🧠  Matches by rule:", rule_summary)
    )
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "evaluate",
            "rules": len(rule_list),
            "matches": total_matches,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
            "matches_by_folder": folder_match_counts,
            "matches_by_rule": rule_match_counts,
        },
        console=summary_console,
    )
    return timer, len(rule_list), total_matches
