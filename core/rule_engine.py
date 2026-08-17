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
from typing import Any, List, Optional, Sequence, Tuple

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


def decode_header_value(value: Any) -> str:
    """Coerce a header value (str or email.header.Header) into a decoded str.

    ``email`` policies (e.g. compat32) return an ``email.header.Header``
    instead of ``str`` when a header contains raw 8-bit bytes that aren't
    valid encoded-words. Callers that expect plain strings (e.g. rule
    matching) must normalize through this first.
    """
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return str(value)


def _parse_header_map(raw_header: str) -> dict[str, str]:
    """Parse the raw header string into a lowercase-keyed mapping, MIME-decoded."""

    message = _HEADER_PARSER.parsestr(raw_header or "", headersonly=True)
    result = {}
    for key, value in message.items():
        result[key.lower()] = decode_header_value(value)
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


def _evaluate_age_condition(
    date: Optional[datetime], condition: dict, now: Optional[datetime] = None
) -> bool:
    """
    Evaluate age-based conditions against message date.

    Supports:
    - age_days_gt: True if message is older than N days
    - age_days_lt: True if message is younger than N days
    - age_days_eq: True if message is exactly N days old

    Args:
        date: Message date as datetime object (timezone-aware)
        condition: Condition dict with age-related keys
        now: Reference "current time" to measure age against. Defaults to
            datetime.now(timezone.utc) if not supplied -- callers evaluating
            many conditions for the same message should compute this once
            and pass it in, so condition-level and action-level age checks
            can't disagree by a few microseconds right at a day boundary.

    Returns:
        True if condition matches, False if date is None or condition doesn't match
    """
    if date is None:
        return False

    # Ensure we're working with timezone-aware datetimes
    now = now if now is not None else datetime.now(timezone.utc)
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
    now: Optional[datetime] = None,
) -> bool:
    """
    Evaluate a condition or logical group against the header map.

    Args:
        header: Parsed email headers with lowercase keys
        node: Condition node (dict, list, or other)
        flags: Optional list of IMAP flags/keywords
        date: Optional message date as datetime
        now: Reference "current time" for age comparisons (see
            _evaluate_age_condition)

    Returns:
        True if condition matches, False otherwise
    """

    if isinstance(node, list):
        # Implicit AND for backward compatibility with legacy rule format.
        return all(_evaluate_condition_node(header, item, flags, date, now) for item in node)

    if isinstance(node, dict):
        matched = False

        # Check for age conditions
        if date is not None and any(
            key in node for key in ["age_days_gt", "age_days_lt", "age_days_eq"]
        ):
            matched = True
            if not _evaluate_age_condition(date, node, now):
                return False

        # Check for NOT wrapper
        if "not" in node:
            matched = True
            negated_condition = node.get("not")
            # Evaluate the negated condition and invert the result
            if _evaluate_condition_node(header, negated_condition, flags, date, now):
                return False
            # If we get here, negated condition was False, so NOT result is True
            # Continue evaluating other conditions in this node

        if "all" in node:
            matched = True
            candidates = node.get("all")
            if not isinstance(candidates, list):
                candidates = [candidates]
            if not all(_evaluate_condition_node(header, item, flags, date, now) for item in candidates):
                return False

        if "any" in node:
            matched = True
            candidates = node.get("any")
            if not isinstance(candidates, list):
                candidates = [candidates]
            # Empty OR group should never match.
            if not candidates:
                return False
            if not any(_evaluate_condition_node(header, item, flags, date, now) for item in candidates):
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
    now: Optional[datetime] = None,
) -> bool:
    """
    Return True if the header satisfies the supplied condition tree.

    Args:
        header: Parsed email headers with lowercase keys
        conditions: Condition tree to evaluate
        flags: Optional list of IMAP flags/keywords
        date: Optional message date as datetime
        now: Reference "current time" for age comparisons

    Returns:
        True if conditions match, False otherwise
    """

    if not conditions:
        return False
    return _evaluate_condition_node(header, conditions, flags, date, now)


def find_matching_rule(
    header: dict,
    rules: Sequence[dict],
    flags: Optional[List[str]] = None,
    date: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict | None:
    """
    Find the first matching rule for a message header.

    Rules are evaluated in order (should be sorted by priority ascending —
    lower priority number = higher precedence, matching evaluate_rules).
    Returns the first matching rule or None if no rules match.

    Note: this only evaluates a rule's `conditions`. It does not know about
    age-gated action brackets (see expand_actions_for_age) -- a caller that
    cares whether a matched rule's actions actually produce anything for a
    given message's age must additionally call expand_actions_for_age on the
    returned rule's actions and handle a `bracket_only_no_match` result by
    retrying against the remaining lower-priority rules.

    Args:
        header: Parsed header dictionary with lowercase keys
        rules: List of rule dictionaries, sorted by priority ascending
        flags: Optional list of IMAP flags/keywords for the message.
        date: Optional message date. Without this, age_days_* conditions
              never match.
        now: Reference "current time" for age comparisons

    Returns:
        The first matching rule dict, or None if no match
    """
    for rule in rules:
        conditions = rule.get("conditions")
        if conditions_match(header, conditions, flags=flags, date=date, now=now):
            return rule
    return None


_ACTION_AGE_LOWER_KEYS = ("age_days_gt", "age_days_gte")
_ACTION_AGE_UPPER_KEYS = ("age_days_lt", "age_days_lte")
_ACTION_AGE_KEYS = _ACTION_AGE_LOWER_KEYS + _ACTION_AGE_UPPER_KEYS + ("age_days_eq",)


def _is_age_bracket(item: Any) -> bool:
    """True if an actions-list item is an age-gated bracket rather than a
    plain action -- i.e. it has a "do" key plus at least one age key."""
    return isinstance(item, dict) and "do" in item and any(key in item for key in _ACTION_AGE_KEYS)


def _bracket_gate_matches(age_days: Optional[int], bracket: dict) -> bool:
    """Evaluate an age-gated action bracket's condition.

    Unlike _evaluate_age_condition (which only ever checks the first age key
    present on a dict, by design/legacy behavior), this ANDs together every
    age key present, so a bracket can combine one lower-bound
    (age_days_gt/age_days_gte) and one upper-bound (age_days_lt/age_days_lte)
    key to express a bounded band. age_days_eq is exclusive of the others
    (the rule validator rejects combining it with a bound).
    """
    if age_days is None:
        return False

    if "age_days_eq" in bracket:
        threshold = bracket["age_days_eq"]
        return isinstance(threshold, (int, float)) and age_days == threshold

    matched_any = False
    if "age_days_gt" in bracket:
        threshold = bracket["age_days_gt"]
        if not (isinstance(threshold, (int, float)) and age_days > threshold):
            return False
        matched_any = True
    if "age_days_gte" in bracket:
        threshold = bracket["age_days_gte"]
        if not (isinstance(threshold, (int, float)) and age_days >= threshold):
            return False
        matched_any = True
    if "age_days_lt" in bracket:
        threshold = bracket["age_days_lt"]
        if not (isinstance(threshold, (int, float)) and age_days < threshold):
            return False
        matched_any = True
    if "age_days_lte" in bracket:
        threshold = bracket["age_days_lte"]
        if not (isinstance(threshold, (int, float)) and age_days <= threshold):
            return False
        matched_any = True

    return matched_any


def expand_actions_for_age(
    actions: Sequence[dict],
    msg_date: Optional[datetime],
    now: datetime,
) -> tuple[list[dict], bool]:
    """Flatten a rule's actions list, resolving age-gated brackets.

    A plain action item (no age key) always fires unconditionally. A bracket
    item (age key + "do") only fires if no earlier bracket in this same list
    already fired (first-bracket-wins among brackets, mirroring the outer
    rule list's first-match-wins) and its age gate is satisfied; its "do"
    list's items are then spliced into the result in place of the bracket.

    Returns (expanded_actions, bracket_only_no_match). bracket_only_no_match
    is True iff the actions list contains at least one bracket, no bracket's
    gate was satisfied, and no plain (unconditional) action was present
    either -- the caller should then treat the rule as not having matched
    this email at all (fall through to the next rule), exactly as if
    `conditions` hadn't matched.
    """
    if not any(_is_age_bracket(item) for item in actions if isinstance(item, dict)):
        # No brackets at all -- preserve today's behavior unconditionally,
        # including a genuinely empty/absent actions list (a silent no-op
        # match, not a fall-through).
        return list(actions), False

    age_days: Optional[int] = None
    if msg_date is not None:
        aware_date = msg_date if msg_date.tzinfo is not None else msg_date.replace(tzinfo=timezone.utc)
        age_days = (now - aware_date).days

    expanded: list[dict] = []
    bracket_fired = False
    has_plain = False
    for item in actions:
        if not isinstance(item, dict):
            continue
        if _is_age_bracket(item):
            if bracket_fired:
                continue
            if _bracket_gate_matches(age_days, item):
                bracket_fired = True
                do_items = item.get("do") or []
                expanded.extend(a for a in do_items if isinstance(a, dict))
        else:
            has_plain = True
            expanded.append(item)

    bracket_only_no_match = not bracket_fired and not has_plain
    return expanded, bracket_only_no_match


def describe_age_bracket(bracket: dict) -> str:
    """Human-readable description of an age-gated bracket's age condition,
    e.g. "age > 30 days" or "age >= 30 days and age < 90 days"."""
    labels = {
        "age_days_gt": "age > {:g} days",
        "age_days_gte": "age >= {:g} days",
        "age_days_lt": "age < {:g} days",
        "age_days_lte": "age <= {:g} days",
        "age_days_eq": "age = {:g} days",
    }
    parts = [labels[k].format(bracket[k]) for k in labels if k in bracket]
    return " and ".join(parts)


def find_fired_bracket(
    actions: Sequence[dict], msg_date: Optional[datetime], now: datetime
) -> Optional[dict]:
    """Return the age-gated bracket (raw dict) that fired for this actions
    list and message age, or None if no bracket fired -- e.g. a plain-only
    rule, or (if called without first checking expand_actions_for_age's
    bracket_only_no_match) a coverage gap. Mirrors the first-bracket-wins
    iteration in expand_actions_for_age; intended for describing *which*
    bracket was responsible for a match already known to have succeeded
    (e.g. for notifications/logging), not for deciding whether it matched.
    """
    age_days: Optional[int] = None
    if msg_date is not None:
        aware_date = msg_date if msg_date.tzinfo is not None else msg_date.replace(tzinfo=timezone.utc)
        age_days = (now - aware_date).days

    for item in actions:
        if isinstance(item, dict) and _is_age_bracket(item):
            if _bracket_gate_matches(age_days, item):
                return item
    return None


def all_possible_move_targets(actions: Sequence[dict]) -> set[str]:
    """Every folder a rule's actions could ever move a message to, across
    all possible age outcomes -- ignores age gating entirely. For reporting/
    conflict-analysis consumers ("what folder(s) can rule X move mail to")
    rather than per-message evaluation; use expand_actions_for_age for that.
    """
    targets: set[str] = set()

    def _collect(items: Sequence[dict]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if _is_age_bracket(item):
                _collect(item.get("do") or [])
            elif item.get("type", "move") == "move" and item.get("target"):
                targets.add(item["target"])

    _collect(actions)
    return targets


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
    show_folder_bar: bool = True,
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
    matches_by_rule_and_target: dict[str, dict[str, int]] = {}
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
        disable=not (show_progress and show_folder_bar),
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
            now = datetime.now(timezone.utc)
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
                if not conditions_match(header, conds, flags=flags, date=date, now=now):
                    continue

                rule_name = rule.get("name") or "(unnamed)"

                # Support both "action" (single) and "actions" (array)
                actions = rule.get("actions", [])
                if not actions and "action" in rule:
                    actions = [rule.get("action")]

                expanded_actions, bracket_only_no_match = expand_actions_for_age(actions, date, now)
                if bracket_only_no_match:
                    # Conditions matched, but none of this rule's age
                    # brackets covered this message's age, and it has no
                    # unconditional actions either -- treat this rule as not
                    # having matched at all and fall through to the next
                    # (lower-priority) rule.
                    continue

                email_matched = True
                total_matches += 1
                folder_match_counts[folder] = folder_match_counts.get(folder, 0) + 1
                rule_match_counts[rule_name] = rule_match_counts.get(rule_name, 0) + 1

                base_priority = int(rule.get("priority", 100))

                # Which age bracket (if any) actually fired, so notifications
                # can say e.g. "age >= 30 days" instead of just "moved".
                fired_bracket = find_fired_bracket(actions, date, now)
                age_gate_desc = describe_age_bracket(fired_bracket) if fired_bracket else None

                if not expanded_actions:
                    # A bracket fired with an empty "do" (deliberate no-op,
                    # e.g. "stays in INBOX today") -- still a real decision,
                    # so it belongs in the digest even though the action loop
                    # below never runs for it.
                    matches_by_rule_and_target.setdefault(rule_name, {})
                    matches_by_rule_and_target[rule_name]["(no action)"] = (
                        matches_by_rule_and_target[rule_name].get("(no action)", 0) + 1
                    )

                # Create action entries with effective priorities
                for action_index, action in enumerate(expanded_actions):
                    action_type = action.get("type", "move")
                    target = action.get("target", "")

                    matches_by_rule_and_target.setdefault(rule_name, {})
                    matches_by_rule_and_target[rule_name][target] = (
                        matches_by_rule_and_target[rule_name].get(target, 0) + 1
                    )

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
                    # - Lower rule priority number wins (priority 10 beats priority 50)
                    # - Invert base_priority so lower numbers yield higher effective_priority
                    # - effective_priority = (1000 - rule_priority) * 10000 - action_index
                    effective_priority = (1000 - base_priority) * 10000 - action_index

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
                            None,
                        ),
                    )
                    action_type_counts[action_type] = action_type_counts.get(action_type, 0) + 1

                    # Log verbose output for each action
                    console_msg: str | None = None
                    if verbose:
                        if action_type == "move":
                            console_msg = f"✅ {rule_name} matched {folder}/{uid} → {target}"
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
                            "age_gate": age_gate_desc,
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
            "matches_by_rule_and_target": matches_by_rule_and_target,
        },
        console=summary_console,
    )
    return timer, len(rule_list), total_matches
