"""Rule loading and evaluation helpers."""
from __future__ import annotations

import email
import email.header
import json
import re
from datetime import datetime, timezone
from email.parser import HeaderParser as _HeaderParser
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso

_HEADER_PARSER = _HeaderParser()


@lru_cache(maxsize=None)
def _get_compiled_regex(pattern: str) -> re.Pattern:
    """Get a compiled regex pattern, cached without eviction.

    Raises:
        re.error: If the pattern is invalid
    """
    return re.compile(pattern, re.IGNORECASE)


def load_rules(rule_dir: Path, logger: JsonLogger, *, skip_disabled: bool = True) -> list[dict]:
    rule_dir = Path(rule_dir)
    rule_dir.mkdir(exist_ok=True)
    rules: list[dict] = []
    skipped = 0
    for path in sorted(rule_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                rule = json.load(handle)
            rule["_file"] = path.name
            rule["priority"] = int(rule.get("priority", 100))
            if skip_disabled and not rule.get("enabled", True):
                skipped += 1
                continue
            rules.append(rule)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.log(
                "ERROR",
                "rule_load_failed",
                {"file": str(path), "error": str(exc)},
                console=f"❌ Failed to load {path.name}",
            )
    msg = f"📜 Loaded {len(rules)} rules"
    if skipped:
        msg += f" ({skipped} disabled)"
    logger.log("INFO", "rules_loaded", {"count": len(rules), "skipped_disabled": skipped}, console=msg)
    _prime_regex_cache(rules)
    return rules


def _prime_regex_cache(rules: list[dict]) -> None:
    """Pre-compile all regex patterns found in the rule set into the LRU cache."""
    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("regex", "not_regex"):
                if key in node and isinstance(node[key], str):
                    try:
                        _get_compiled_regex(node[key])
                    except re.error:
                        pass
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for rule in rules:
        _walk(rule.get("conditions"))


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
    """Parse the raw header string into a lowercase-keyed mapping, MIME-decoded."""

    message = _HEADER_PARSER.parsestr(raw_header or "", headersonly=True)
    result = {}
    for key, value in message.items():
        try:
            result[key.lower()] = str(email.header.make_header(email.header.decode_header(value)))
        except Exception:
            result[key.lower()] = value
    return result


def _parse_internaldate(date_str: Optional[str]) -> Optional[datetime]:
    """
    Parse IMAP INTERNALDATE format into a datetime object.

    Handles formats like:
    - "28-Oct-2025 07:30:19 +0000"
    - "28-Oct-2025 07:30:19" (without timezone)

    Returns:
        datetime object (timezone-aware if timezone present) or None if parsing fails
    """
    if not date_str:
        return None

    # Try with timezone first
    formats_to_try = [
        "%d-%b-%Y %H:%M:%S %z",  # With timezone: "28-Oct-2025 07:30:19 +0000"
        "%d-%b-%Y %H:%M:%S",     # Without timezone: "28-Oct-2025 07:30:19"
    ]

    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            # If no timezone, assume UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            continue

    return None


def _parse_header_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse an RFC 2822 ``Date:`` header into a timezone-aware datetime.

    Used as a fallback when the cached payload has no INTERNALDATE. Returns
    None if the value is missing or unparseable.
    """
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_message_metadata(data: str) -> Tuple[dict, List[str], Optional[datetime]]:
    """
    Extract message metadata from cached data string.

    Args:
        data: JSON string containing message data

    Returns:
        Tuple of (header_dict, flags_list, date_object)
        - header_dict: Parsed email headers with lowercase keys
        - flags_list: IMAP flags (empty list if not present)
        - date_object: Message date as datetime (None if not present)
    """
    # Default empty values
    header_dict: dict = {}
    flags_list: list = []
    date_object: Optional[datetime] = None

    # Parse JSON safely
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        # Old format: just raw header string
        header_dict = _parse_header_map(data)
        return (header_dict, flags_list, date_object)

    # Extract header
    if isinstance(payload, dict):
        raw_header = payload.get("header", "")
        if isinstance(raw_header, str):
            header_dict = _parse_header_map(raw_header)

        # Extract flags
        flags = payload.get("flags")
        if isinstance(flags, list):
            flags_list = flags

        # Extract and parse date
        date_str = payload.get("internaldate")
        if isinstance(date_str, str):
            date_object = _parse_internaldate(date_str)

        # Fallback: derive the date from the message's Date: header when the
        # cached payload has no internaldate (e.g. caches built before
        # internaldate capture, or when the FETCH response parser missed it).
        # Without this, every age-based rule silently fails to match.
        if date_object is None:
            date_object = _parse_header_date(header_dict.get("date"))
    else:
        # Fallback: treat entire payload as header
        header_dict = _parse_header_map(str(payload))

    return (header_dict, flags_list, date_object)


def _evaluate_flag_condition(flags: List[str], condition: dict) -> bool:
    """
    Evaluate flag-based conditions against message flags.

    Supports:
    - has_keyword/has_flag: True if keyword exists in flags
    - lacks_keyword/lacks_flag: True if keyword does NOT exist in flags

    Args:
        flags: List of IMAP flags/keywords for the message
        condition: Condition dict with flag-related keys

    Returns:
        True if condition matches, False otherwise
    """
    # Check for has_keyword or has_flag
    has_keyword = condition.get("has_keyword") or condition.get("has_flag")
    if has_keyword:
        return has_keyword in flags

    # Check for lacks_keyword or lacks_flag
    lacks_keyword = condition.get("lacks_keyword") or condition.get("lacks_flag")
    if lacks_keyword:
        return lacks_keyword not in flags

    return False


def _evaluate_age_condition(date: Optional[datetime], condition: dict) -> bool:
    """
    Evaluate age-based conditions against message date.

    Supports:
    - age_days_gt: True if message is older than N days
    - age_days_lt: True if message is younger than N days
    - age_days_eq: True if message is exactly N days old

    Args:
        date: Message date as datetime object (timezone-aware)
        condition: Condition dict with age-related keys

    Returns:
        True if condition matches, False if date is None or condition doesn't match
    """
    if date is None:
        return False

    # Ensure we're working with timezone-aware datetimes
    now = datetime.now(timezone.utc)
    msg_date = date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)

    # Calculate age in days
    age_days = (now - msg_date).days

    # Check age_days_gt
    if "age_days_gt" in condition:
        threshold = condition["age_days_gt"]
        if isinstance(threshold, (int, float)):
            return age_days > threshold

    # Check age_days_lt
    if "age_days_lt" in condition:
        threshold = condition["age_days_lt"]
        if isinstance(threshold, (int, float)):
            return age_days < threshold

    # Check age_days_eq
    if "age_days_eq" in condition:
        threshold = condition["age_days_eq"]
        if isinstance(threshold, (int, float)):
            return age_days == threshold

    return False


def rule_match(header: dict, cond: dict) -> bool:
    """Evaluate a header condition against the header map.

    Supports:
    - contains: substring match (case-insensitive)
    - not_contains: negated substring match
    - equals: exact match (case-insensitive)
    - not_equals: negated exact match
    - regex: regex match (case-insensitive)
    - not_regex: negated regex match
    """
    value = header.get(cond.get("header", "").lower(), "") or ""

    # Positive operators
    if "contains" in cond:
        pattern = cond["contains"]
        return pattern.lower() in value.lower()

    if "equals" in cond:
        pattern = cond["equals"]
        return pattern.lower() == value.lower()

    if "regex" in cond:
        pattern = cond["regex"]
        try:
            compiled = _get_compiled_regex(pattern)
            return bool(compiled.search(value))
        except re.error:
            return False

    # Negative operators
    if "not_contains" in cond:
        pattern = cond["not_contains"]
        return pattern.lower() not in value.lower()

    if "not_equals" in cond:
        pattern = cond["not_equals"]
        return pattern.lower() != value.lower()

    if "not_regex" in cond:
        pattern = cond["not_regex"]
        try:
            compiled = _get_compiled_regex(pattern)
            return not bool(compiled.search(value))
        except re.error:
            return True  # Invalid regex should not match

    # No valid operator found
    return False


def _evaluate_condition_node(
    header: dict,
    node: Any,
    flags: Optional[List[str]] = None,
    date: Optional[datetime] = None,
) -> bool:
    """
    Evaluate a condition or logical group against the header map.

    Args:
        header: Parsed email headers with lowercase keys
        node: Condition node (dict, list, or other)
        flags: Optional list of IMAP flags/keywords
        date: Optional message date as datetime

    Returns:
        True if condition matches, False otherwise
    """

    if isinstance(node, list):
        # Implicit AND for backward compatibility with legacy rule format.
        return all(_evaluate_condition_node(header, item, flags, date) for item in node)

    if isinstance(node, dict):
        matched = False

        # Check for flag conditions
        if flags is not None and any(
            key in node for key in ["has_keyword", "has_flag", "lacks_keyword", "lacks_flag"]
        ):
            matched = True
            if not _evaluate_flag_condition(flags, node):
                return False

        # Check for age conditions
        if date is not None and any(
            key in node for key in ["age_days_gt", "age_days_lt", "age_days_eq"]
        ):
            matched = True
            if not _evaluate_age_condition(date, node):
                return False

        # Check for NOT wrapper
        if "not" in node:
            matched = True
            negated_condition = node.get("not")
            # Evaluate the negated condition and invert the result
            if _evaluate_condition_node(header, negated_condition, flags, date):
                return False
            # If we get here, negated condition was False, so NOT result is True
            # Continue evaluating other conditions in this node

        if "all" in node:
            matched = True
            candidates = node.get("all")
            if not isinstance(candidates, list):
                candidates = [candidates]
            if not all(_evaluate_condition_node(header, item, flags, date) for item in candidates):
                return False

        if "any" in node:
            matched = True
            candidates = node.get("any")
            if not isinstance(candidates, list):
                candidates = [candidates]
            # Empty OR group should never match.
            if not candidates:
                return False
            if not any(_evaluate_condition_node(header, item, flags, date) for item in candidates):
                return False

        if matched:
            return True

        return rule_match(header, node)

    return False


def conditions_match(
    header: dict,
    conditions: Any,
    flags: Optional[List[str]] = None,
    date: Optional[datetime] = None,
) -> bool:
    """
    Return True if the header satisfies the supplied condition tree.

    Args:
        header: Parsed email headers with lowercase keys
        conditions: Condition tree to evaluate
        flags: Optional list of IMAP flags/keywords
        date: Optional message date as datetime

    Returns:
        True if conditions match, False otherwise
    """

    if not conditions:
        return False
    return _evaluate_condition_node(header, conditions, flags, date)


def find_matching_rule(
    header: dict,
    rules: Sequence[dict],
    flags: Optional[List[str]] = None,
    date: Optional[datetime] = None,
) -> dict | None:
    """
    Find the first matching rule for a message header.

    Rules are evaluated in order (should be sorted by priority ascending —
    lower priority number = higher precedence, matching evaluate_rules).
    Returns the first matching rule or None if no rules match.

    Args:
        header: Parsed header dictionary with lowercase keys
        rules: List of rule dictionaries, sorted by priority ascending
        flags: Optional list of IMAP flags/keywords for the message.
               Without this, has_keyword/lacks_keyword conditions never match.
        date: Optional message date. Without this, age_days_* conditions
              never match.

    Returns:
        The first matching rule dict, or None if no match
    """
    for rule in rules:
        conditions = rule.get("conditions")
        if conditions_match(header, conditions, flags=flags, date=date):
            return rule
    return None


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
    limit: int | None = None,
) -> tuple[PhaseTimer, int, int]:
    # Sort by priority ascending so lower numbers (higher precedence) are evaluated first.
    # evaluate_rules is first-match-wins: it breaks after the first rule that matches each email.
    rule_list = sorted(rules, key=lambda r: int(r.get("priority", 100)))
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
    matched_email_count = 0
    folder_match_counts: dict[str, int] = {}
    rule_match_counts: dict[str, int] = {}
    action_type_counts: dict[str, int] = {}
    # Header rows to drop (same-folder no-op moves). Deleting from `headers`
    # while `cur` is still iterating it is undefined behavior in SQLite, so
    # deletions are collected here and applied after the scan completes.
    stale_header_keys: list[tuple[str, str]] = []
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
    limit_reached = False
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

            header, flags, date = _extract_message_metadata(data)
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
                        "flags": flags,
                        "date": date.isoformat() if date else None,
                    },
                )

            email_matched = False
            for rule in rule_list:
                conds = rule.get("conditions")
                if conditions_match(header, conds, flags=flags, date=date):
                    email_matched = True
                    rule_name = rule.get("name") or "(unnamed)"
                    total_matches += 1
                    folder_match_counts[folder] = folder_match_counts.get(folder, 0) + 1
                    rule_match_counts[rule_name] = rule_match_counts.get(rule_name, 0) + 1

                    # Support both "action" (single) and "actions" (array)
                    actions = rule.get("actions", [])
                    if not actions and "action" in rule:
                        actions = [rule.get("action")]

                    base_priority = int(rule.get("priority", 100))

                    # Create action entries with effective priorities
                    for action_index, action in enumerate(actions):
                        action_type = action.get("type", "move")
                        target = action.get("target", "")

                        # Skip redundant same-folder move actions
                        if action_type == "move" and target and folder == target:
                            logger.log(
                                "INFO",
                                "skipped_same_folder_move",
                                {
                                    "rule": rule.get("name"),
                                    "folder": folder,
                                    "uid": uid,
                                    "target": target,
                                },
                                console=f"⊘ {rule_name}: {folder}/{uid} already in target folder {target}",
                            )
                            # Remove from cache since the email is already in the target location
                            # (deferred until after the scan — see stale_header_keys above)
                            stale_header_keys.append((folder, uid))
                            continue

                        # Calculate effective priority to ensure execution order:
                        # - Keywords (1000) execute before moves (500) within the same rule
                        # - Lower rule priority number wins (priority 10 beats priority 50)
                        # - Invert base_priority so lower numbers yield higher effective_priority
                        # - effective_priority = (1000 - rule_priority) * 10000 + type_priority - action_index
                        type_priority = 1000 if action_type in ("set_keywords", "remove_keywords") else 500
                        effective_priority = (1000 - base_priority) * 10000 + type_priority - action_index

                        # Serialize action data (keywords, etc.) as JSON if present
                        action_data = None
                        if action_type in ("set_keywords", "remove_keywords"):
                            keywords = action.get("keywords", [])
                            if keywords:
                                action_data = json.dumps({"keywords": keywords})

                        db.execute(
                            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, action_type, action_data) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                uid,
                                folder,
                                rule.get("name"),
                                target,
                                effective_priority,
                                "pending" if not dry_run else "simulated",
                                now_iso(),
                                action_type,
                                action_data,
                            ),
                        )
                        action_type_counts[action_type] = action_type_counts.get(action_type, 0) + 1

                        # Log verbose output for each action
                        console_msg: str | None = None
                        if verbose:
                            if action_type == "move":
                                console_msg = f"✅ {rule_name} matched {folder}/{uid} → {target}"
                            elif action_type in ("set_keywords", "remove_keywords"):
                                keywords = action.get("keywords", [])
                                console_msg = f"✅ {rule_name} matched {folder}/{uid} ({action_type}: {keywords})"
                            else:
                                console_msg = f"✅ {rule_name} matched {folder}/{uid} ({action_type})"
                        logger.log(
                            "INFO",
                            "rule_match",
                            {
                                "rule": rule.get("name"),
                                "priority": base_priority,
                                "effective_priority": effective_priority,
                                "folder": folder,
                                "uid": uid,
                                "action_type": action_type,
                                "target": target,
                                "dry_run": dry_run,
                            },
                            console=console_msg,
                        )

                    # First-match-wins: stop evaluating further rules for this email.
                    break

            if email_matched:
                matched_email_count += 1
                if limit is not None and matched_email_count >= limit:
                    limit_reached = True
                    break

        if limit_reached:
            break

    if limit_reached:
        logger.log(
            "INFO",
            "evaluate_limit_reached",
            {"limit": limit, "matched_emails": matched_email_count},
            console=f"🛑 Limit reached: stopped after {matched_email_count} matched email{'s' if matched_email_count != 1 else ''}",
        )

    _finalize_folder()
    folders_bar.close()
    if stale_header_keys:
        db.executemany(
            "DELETE FROM headers WHERE folder=? AND uid=?",
            stale_header_keys,
        )
        db.commit()
    timer.stop()
    timer.count = total_matches
    folder_summary = sorted(folder_match_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rule_summary = sorted(rule_match_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    total_actions = sum(action_type_counts.values())
    action_summary = sorted(action_type_counts.items(), key=lambda kv: (-kv[1], kv[0]))

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
        f"   ⚡  Actions generated: {total_actions}\n"
        f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        + _fmt_summary("📂  Matches by folder:", folder_summary)
        + _fmt_summary("🧠  Matches by rule:", rule_summary)
        + _fmt_summary("⚙️  Actions by type:", action_summary)
    )
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "evaluate",
            "rules": len(rule_list),
            "matches": total_matches,
            "actions": total_actions,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
            "matches_by_folder": folder_match_counts,
            "matches_by_rule": rule_match_counts,
            "actions_by_type": action_type_counts,
        },
        console=summary_console,
    )
    return timer, len(rule_list), total_matches
