#!/usr/bin/env python3
"""Interactive rule management console for IMAPFilter Helper.

This module provides a small text UI for inspecting, creating and editing
rule definition files stored in the ``rules/`` directory.  Whenever the
terminal supports :mod:`curses` we display scrollable menus, otherwise we
fall back to simple numbered prompts.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - capability detection depends on the host terminal
    import curses
except Exception:  # pragma: no cover - gracefully degrade when curses is missing
    curses = None  # type: ignore[assignment]

from core.config import build_default_config
from core.database import init_db
from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules, load_rules
from core.rule_utils import slugify
from core.rule_validator import RuleValidator


# ---------------------------------------------------------------------------
# Generic input helpers

def prompt(message: str, *, allow_empty: bool = False) -> str:
    """Prompt the user for text, repeating until a value is provided."""

    while True:
        value = input(message).strip()
        if value or allow_empty:
            return value
        print("⚠️  Please enter a value or press CTRL+C to cancel.")


def prompt_int(message: str, *, default: int | None = None) -> int:
    """Prompt for an integer, repeating until a valid number is entered."""

    while True:
        raw = input(message).strip()
        if not raw and default is not None:
            return default
        try:
            return int(raw)
        except ValueError:
            print("⚠️  Please enter a whole number (e.g. 100).")


def confirm(message: str, *, default: bool = False) -> bool:
    """Prompt the user for a yes/no confirmation."""

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        choice = input(f"{message} {suffix} ").strip().lower()
        if not choice:
            return default
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print("⚠️  Please respond with 'y' or 'n'.")



# ---------------------------------------------------------------------------
# Menu helpers

def _supports_curses() -> bool:
    return bool(
        curses
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def interactive_menu(title: str, options: Sequence[str]) -> int | None:
    """Display ``options`` using an interactive, scrollable curses menu.

    ``None`` is returned when the user aborts with ESC or ``q``.  A
    ``RuntimeError`` is raised when curses support is unavailable.
    """

    if not _supports_curses():
        raise RuntimeError("curses menu unavailable")

    selected = 0
    top = 0
    aborted = False

    def _run(stdscr: Any) -> None:
        nonlocal selected, top, aborted
        curses.curs_set(0)
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:  # pragma: no cover - not all terminals support this
            pass

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            body_top = 2
            body_height = max(1, height - body_top - 2)

            stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD)

            for offset in range(body_height):
                index = top + offset
                if index >= len(options):
                    break
                label = f"{index + 1:>3}. {options[index]}"
                attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
                stdscr.addnstr(body_top + offset, 0, label, width - 1, attr)

            help_text = "↑/↓ move  PgUp/PgDn jump  Enter select  ESC cancel"
            stdscr.addnstr(height - 1, 0, help_text, width - 1, curses.A_DIM)
            stdscr.refresh()

            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(options) - 1, selected + 1)
            elif key in (curses.KEY_PPAGE,):
                selected = max(0, selected - body_height)
            elif key in (curses.KEY_NPAGE,):
                selected = min(len(options) - 1, selected + body_height)
            elif key in (curses.KEY_HOME,):
                selected = 0
            elif key in (curses.KEY_END,):
                selected = len(options) - 1
            elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
                return
            elif key in (27, ord("q"), ord("Q")):
                aborted = True
                return
            elif 32 <= key <= 126:
                curses.beep()

            if selected < top:
                top = selected
            elif selected >= top + body_height:
                top = selected - body_height + 1

    try:
        curses.wrapper(_run)
    except curses.error as exc:  # pragma: no cover - terminal limitations
        raise RuntimeError("curses menu unavailable") from exc

    if aborted:
        return None
    return selected


def _text_menu(
    title: str,
    options: Sequence[str],
    *,
    prompt_text: str = "Enter number: ",
    allow_cancel: bool = True,
) -> int | None:
    while True:
        print(f"\n{title}")
        for idx, label in enumerate(options, start=1):
            print(f"  {idx:>3}. {label}")
        raw = input(prompt_text).strip()
        if not raw:
            if allow_cancel:
                return None
            continue
        try:
            index = int(raw) - 1
        except ValueError:
            print("⚠️  Please enter a number.")
            continue
        if 0 <= index < len(options):
            return index
        print("⚠️  Number out of range.")


def choose_menu_option(
    title: str,
    options: Sequence[tuple[str, str]],
    *,
    prompt_text: str = "Select action: ",
) -> str | None:
    """Return the hotkey for the selected option or ``None`` if cancelled."""

    labels = [f"[{key.upper()}] {label}" for key, label in options]
    try:
        choice = interactive_menu(title, labels)
    except RuntimeError:
        while True:
            print(f"\n{title}")
            for key, label in options:
                print(f"  [{key.upper()}] {label}")
            raw = input(prompt_text).strip().lower()
            if not raw:
                continue
            hotkey = raw[0]
            for key, _ in options:
                if hotkey == key:
                    return key
            print("⚠️  Unknown option. Please choose one of the highlighted letters.")
        return None  # pragma: no cover - loop above always returns
    if choice is None:
        return None
    return options[choice][0]


def choose_from_list(
    title: str,
    entries: Sequence[str],
    *,
    prompt_text: str = "Enter number (blank to cancel): ",
    allow_cancel: bool = True,
) -> int | None:
    """Return the index of the selected entry or ``None`` if cancelled."""

    if not entries:
        return None
    try:
        choice = interactive_menu(title, list(entries))
    except RuntimeError:
        return _text_menu(title, entries, prompt_text=prompt_text, allow_cancel=allow_cancel)
    return choice


# ---------------------------------------------------------------------------
# Rule editing helpers

def summarise_condition(node: Any) -> str:
    """Return a compact, human-friendly description of a condition node."""

    if not node:
        return "(empty)"
    if isinstance(node, dict):
        if "all" in node or "any" in node:
            key = "all" if "all" in node else "any"
            children = node.get(key) or []
            label = "ALL" if key == "all" else "ANY"
            return f"Group {label} ({len(children)} entries)"

        # Handle keyword/flag conditions
        if "has_keyword" in node or "has_flag" in node:
            keyword = node.get("has_keyword") or node.get("has_flag")
            return f"has keyword: {keyword}"
        if "lacks_keyword" in node or "lacks_flag" in node:
            keyword = node.get("lacks_keyword") or node.get("lacks_flag")
            return f"lacks keyword: {keyword}"

        # Handle age conditions
        if "age_days_gt" in node:
            return f"age > {node['age_days_gt']} days"
        if "age_days_lt" in node:
            return f"age < {node['age_days_lt']} days"
        if "age_days_eq" in node:
            return f"age = {node['age_days_eq']} days"

        # Handle header conditions
        header = node.get("header", "<header>")
        # Check all 6 match types
        if "equals" in node:
            return f"{header} == {node['equals']}"
        if "not_equals" in node:
            return f"{header} != {node['not_equals']}"
        if "contains" in node:
            return f"{header} ⊃ {node['contains']}"
        if "not_contains" in node:
            return f"{header} ⊅ {node['not_contains']}"
        if "regex" in node:
            return f"{header} ~= {node['regex']}"
        if "not_regex" in node:
            return f"{header} !~ {node['not_regex']}"
        return f"{header} (custom)"
    if isinstance(node, list):
        return f"Implicit ALL ({len(node)})"
    return str(node)


def summarise_action(actions: Any) -> str:
    """Return a concise description of an action block or list of actions."""

    # Handle list of actions (multiple actions)
    if isinstance(actions, list):
        if not actions:
            return "(no actions)"
        if len(actions) == 1:
            return summarise_action(actions[0])
        # Multiple actions: summarize each and combine
        summaries = []
        for action in actions:
            if isinstance(action, dict):
                act_type = action.get("type", "move")
                target = action.get("target")
                summary = f"{act_type} → {target}" if target else act_type
                summaries.append(summary)
        if summaries:
            return f"{len(actions)} actions: {', '.join(summaries)}"
        return "(no actions)"

    # Handle single action (dict)
    if not isinstance(actions, dict):
        return "(no action)"
    act_type = actions.get("type", "move")
    target = actions.get("target")
    summary = f"{act_type} → {target}" if target else act_type
    extras = [key for key in actions.keys() if key not in {"type", "target"}]
    if extras:
        summary += f" (+{len(extras)} extra field{'s' if len(extras) != 1 else ''})"
    return summary


def _format_condition_tree(
    node: Any, indent: int = 0, max_depth: int = 5, current_depth: int = 0
) -> str:
    """Format a condition node as a hierarchical tree with indentation and operators.

    Args:
        node: Condition node (dict, list, or single condition)
        indent: Starting indentation level (spaces)
        max_depth: Maximum recursion depth before truncating
        current_depth: Current recursion depth

    Returns:
        Formatted tree string with AND/OR operators and indentation

    Example output:
        → [Group: ANY of 2 conditions]
            • from contains '@e.hollisterco.com'
            • from contains '@em.hollisterco.com'
        ↓ AND ↓
        → from not_contains 'orders@e.hollisterco.com'
    """
    if current_depth >= max_depth:
        return " " * indent + "→ ..."

    if not node:
        return " " * indent + "→ (empty)"

    # Handle group (all/any)
    if isinstance(node, dict) and ("all" in node or "any" in node):
        key = "all" if "all" in node else "any"
        children = node.get(key) or []
        logic_label = "ALL" if key == "all" else "ANY"

        if not children:
            return " " * indent + f"→ [Group: {logic_label} of 0 conditions]"

        lines = []
        lines.append(" " * indent + f"→ [Group: {logic_label} of {len(children)} conditions]")

        for i, child in enumerate(children):
            # Format child condition
            if isinstance(child, dict) and ("all" in child or "any" in child):
                # Nested group
                child_str = _format_condition_tree(
                    child, indent + 4, max_depth, current_depth + 1
                )
                lines.append(child_str)
            else:
                # Leaf condition
                summary = summarise_condition(child)
                lines.append(" " * (indent + 4) + f"• {summary}")

            # Add operator between items (except after last item)
            if i < len(children) - 1:
                lines.append(" " * indent + f"↓ {logic_label} ↓")

        return "\n".join(lines)

    # Handle single condition
    summary = summarise_condition(node)
    return " " * indent + f"→ {summary}"


def ensure_group(node: Any) -> dict[str, Any]:
    """Normalise ``node`` into the internal group representation."""

    if not node:
        return {"all": []}
    if isinstance(node, list):
        return {"all": list(node)}
    if isinstance(node, dict) and ("all" in node or "any" in node):
        key = "all" if "all" in node else "any"
        items = node.get(key)
        if not isinstance(items, list):
            items = [items]
        return {key: [normalise_condition(item) for item in items]}
    return {"all": [normalise_condition(node)]}


def normalise_condition(node: Any) -> Any:
    """Recursively ensure nested groups use the internal structure."""

    if isinstance(node, dict) and ("all" in node or "any" in node):
        key = "all" if "all" in node else "any"
        items = node.get(key)
        if not isinstance(items, list):
            items = [items]
        return {key: [normalise_condition(item) for item in items]}
    if isinstance(node, list):
        return [normalise_condition(item) for item in node]
    return node


def _detect_condition_type(node: dict[str, Any]) -> str:
    """Detect the type of a condition node.

    Returns:
        "group" | "keyword" | "age" | "header" | "unknown"
    """
    if "all" in node or "any" in node:
        return "group"

    # Check for keyword conditions
    if any(key in node for key in ["has_keyword", "has_flag", "lacks_keyword", "lacks_flag"]):
        return "keyword"

    # Check for age conditions
    if any(key in node for key in ["age_days_gt", "age_days_lt", "age_days_eq"]):
        return "age"

    # Check for header conditions
    if "header" in node:
        return "header"

    return "unknown"


def edit_generic_dict(data: dict[str, Any], *, protected: Iterable[str] = ()) -> None:
    """Generic key/value editor for dictionaries."""

    protected_keys = set(protected)
    while True:
        items = list(data.items())
        labels = [
            f"{'*' if key in protected_keys else ' '} {key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in items
        ]
        add_index = len(labels)
        back_index = add_index + 1
        labels.append("➕ Add new entry")
        labels.append("⬅ Back")

        selection = choose_from_list(
            "Additional fields",
            labels,
        )
        if selection is None or selection == back_index:
            return
        if selection == add_index:
            key = prompt("  Key: ")
            if key in protected_keys:
                print("⚠️  That key is managed elsewhere.")
                continue
            raw = prompt("  Value (JSON encoded): ")
            try:
                data[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"⚠️  Invalid JSON: {exc}")
            continue

        key, value = items[selection]
        if key in protected_keys:
            print("⚠️  That key is managed elsewhere.")
            continue
        action = choose_menu_option(
            f"Entry {key} = {json.dumps(value, ensure_ascii=False)}",
            [("e", "Edit value"), ("r", "Remove entry"), ("b", "Back")],
        )
        if action in {None, "b"}:
            continue
        if action == "e":
            raw = prompt("  New value (JSON encoded): ")
            try:
                data[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"⚠️  Invalid JSON: {exc}")
        elif action == "r":
            del data[key]


def edit_simple_condition(node: dict[str, Any]) -> dict[str, Any]:
    """Interactively edit a single header match condition."""

    while True:
        header = node.get("header", "")
        # Detect which match type is being used
        match_field = None
        for mtype in ["equals", "not_equals", "contains", "not_contains", "regex", "not_regex"]:
            if mtype in node:
                match_field = mtype
                break
        if not match_field:
            match_field = "contains"  # default
        match_value = node.get(match_field, "") if match_field in node else ""
        # Exclude all 6 match type keys from extras
        extras = {k: v for k, v in node.items() if k not in {"header", "equals", "not_equals", "contains", "not_contains", "regex", "not_regex"}}
        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in extras.items()
        ) or "(none)"

        action = choose_menu_option(
            "Condition editor",
            [
                ("h", f"Header     : {header or '<unset>'}"),
                ("m", f"Match type : {match_field}"),
                ("v", f"Value      : {match_value}"),
                ("x", f"Extras     : {extras_summary}"),
                ("b", "Back"),
            ],
        )
        if action in {None, "b"}:
            return node
        if action == "h":
            node["header"] = prompt("  Header name: ")
        elif action == "m":
            new_type = _get_match_type_menu(header=node.get("header"))
            # Remove all existing match type keys
            for mtype in ["equals", "not_equals", "contains", "not_contains", "regex", "not_regex"]:
                node.pop(mtype, None)
            node[new_type] = prompt("  Match value: ")
        elif action == "v":
            if match_field not in {"equals", "not_equals", "contains", "not_contains", "regex", "not_regex"}:
                print("⚠️  Set the match type first.")
                continue
            node[match_field] = prompt("  Match value: ", allow_empty=True)
        elif action == "x":
            edit_generic_dict(node, protected={"header", "equals", "not_equals", "contains", "not_contains", "regex", "not_regex"})


def edit_keyword_condition(node: dict[str, Any]) -> dict[str, Any]:
    """Interactively edit a keyword-based condition."""

    while True:
        # Detect which operator is being used
        operator = None
        keyword_value = ""
        for op in ["has_keyword", "has_flag", "lacks_keyword", "lacks_flag"]:
            if op in node:
                operator = op
                keyword_value = node.get(op, "")
                break

        if not operator:
            operator = "has_keyword"

        # Build extras (non-protected fields)
        extras = {k: v for k, v in node.items()
                  if k not in {"has_keyword", "has_flag", "lacks_keyword", "lacks_flag"}}
        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in extras.items()
        ) or "(none)"

        # Friendly operator display
        operator_display = {
            "has_keyword": "has keyword",
            "has_flag": "has keyword",
            "lacks_keyword": "lacks keyword",
            "lacks_flag": "lacks keyword"
        }[operator]

        action = choose_menu_option(
            "Keyword condition editor",
            [
                ("o", f"Operator : {operator_display}"),
                ("v", f"Keyword  : {keyword_value or '<unset>'}"),
                ("x", f"Extras   : {extras_summary}"),
                ("b", "Back"),
            ],
        )

        if action in {None, "b"}:
            return node

        if action == "o":
            print("\n  1. Has keyword")
            print("  2. Lacks keyword")
            choice = input("  > ").strip()

            # Determine new operator
            if choice == "1":
                new_op = "has_keyword"
            elif choice == "2":
                new_op = "lacks_keyword"
            else:
                print("⚠️  Please enter 1 or 2.")
                continue

            # Remove all existing keyword operator keys
            for op in ["has_keyword", "has_flag", "lacks_keyword", "lacks_flag"]:
                node.pop(op, None)
            node[new_op] = keyword_value or ""

        elif action == "v":
            node[operator] = prompt("  Keyword/Flag: ", allow_empty=True)

        elif action == "x":
            edit_generic_dict(node, protected={"has_keyword", "has_flag", "lacks_keyword", "lacks_flag"})


def edit_age_condition(node: dict[str, Any]) -> dict[str, Any]:
    """Interactively edit an age-based condition."""

    while True:
        # Detect which operator is being used
        operator = None
        days_value = 0
        for op in ["age_days_gt", "age_days_lt", "age_days_eq"]:
            if op in node:
                operator = op
                days_value = node.get(op, 0)
                break

        if not operator:
            operator = "age_days_gt"

        # Build extras (non-protected fields)
        extras = {k: v for k, v in node.items()
                  if k not in {"age_days_gt", "age_days_lt", "age_days_eq"}}
        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in extras.items()
        ) or "(none)"

        # Friendly operator display
        operator_display = {
            "age_days_gt": "older than (>)",
            "age_days_lt": "younger than (<)",
            "age_days_eq": "exactly (=)"
        }[operator]

        action = choose_menu_option(
            "Age condition editor",
            [
                ("o", f"Operator : {operator_display}"),
                ("d", f"Days     : {days_value}"),
                ("x", f"Extras   : {extras_summary}"),
                ("b", "Back"),
            ],
        )

        if action in {None, "b"}:
            return node

        if action == "o":
            print("\n  1. Older than (>)")
            print("  2. Younger than (<)")
            print("  3. Exactly (=)")
            choice = input("  > ").strip()

            # Determine new operator
            if choice == "1":
                new_op = "age_days_gt"
            elif choice == "2":
                new_op = "age_days_lt"
            elif choice == "3":
                new_op = "age_days_eq"
            else:
                print("⚠️  Please enter 1, 2, or 3.")
                continue

            # Remove all existing age operator keys
            for op in ["age_days_gt", "age_days_lt", "age_days_eq"]:
                node.pop(op, None)
            node[new_op] = days_value

        elif action == "d":
            days = prompt_int("  Number of days: ", default=days_value)
            node[operator] = days

        elif action == "x":
            edit_generic_dict(node, protected={"age_days_gt", "age_days_lt", "age_days_eq"})


def _get_field_guidance(header: str | None) -> str:
    """Return contextual guidance for a header field.

    Args:
        header: Header field name (from, to, subject, etc.)

    Returns:
        Guidance text for the field
    """
    if not header:
        return ""

    guidance = {
        "from": "Sender address. Often includes display names like 'Name <email@domain.com>'. "
        "Use 'contains' to match any display name format.",
        "to": "Recipient address. May have multiple recipients. "
        "Use 'contains' to match email parts.",
        "cc": "Carbon copy address. Similar to 'to' field. "
        "Use 'contains' to match email patterns.",
        "bcc": "Blind carbon copy address. "
        "Use 'contains' to match email patterns.",
        "reply-to": "Reply address (may differ from From). "
        "Use 'contains' like the From field.",
        "subject": "Email subject line. "
        "Use 'contains' for keywords or 'regex' for patterns.",
        "list-id": "Mailing list identifier. "
        "Use 'contains' to match domain or list name.",
    }

    return guidance.get(header.lower(), "")


def _get_match_type_menu(header: str | None = None) -> str:
    """Display numbered menu for match types with field-specific recommendations.

    Args:
        header: Optional header field name for context-aware recommendations

    Returns:
        Selected match type key
    """
    # Show field guidance if available
    if header:
        guidance = _get_field_guidance(header)
        if guidance:
            print(f"\n📝 {guidance}")

    # For email fields, recommend contains/not_contains
    is_email_field = header and header.lower() in {
        "from", "to", "cc", "bcc", "reply-to"
    }

    if is_email_field:
        print("\n⚠️ Email address hint: Email fields often include display names.")
        print("   Recommend using 'Contains' or 'Not Contains' for flexibility.\n")
        match_types = [
            ("contains", "Contains substring [RECOMMENDED]"),
            ("not_contains", "Does not contain substring [RECOMMENDED]"),
            ("regex", "Regular expression"),
            ("not_regex", "Does not match regex"),
            ("equals", "Exact match (rarely needed for email)"),
            ("not_equals", "Not exact match (rarely needed)"),
        ]
    else:
        match_types = [
            ("contains", "Contains substring"),
            ("not_contains", "Does not contain substring"),
            ("equals", "Exact match"),
            ("not_equals", "Does not match exactly"),
            ("regex", "Regular expression"),
            ("not_regex", "Does not match regex"),
        ]

    while True:
        print("Match type:")
        for idx, (type_key, description) in enumerate(match_types, start=1):
            print(f"  {idx}. {description}")

        raw = input("Select match type (1-6): ").strip()
        if not raw:
            print("⚠️  Please enter a number between 1 and 6.")
            continue

        try:
            choice = int(raw)
            if 1 <= choice <= len(match_types):
                return match_types[choice - 1][0]
            print(f"⚠️  Please enter a number between 1 and {len(match_types)}.")
        except ValueError:
            print("⚠️  Please enter a valid number.")


def _get_action_type_menu() -> str:
    """Display numbered menu for action types and return the selected type."""

    action_types = [
        ("move", "Move to folder"),
        ("set_keywords", "Set keywords (add labels)"),
        ("remove_keywords", "Remove keywords (remove labels)"),
    ]

    while True:
        print("\nAction type:")
        for idx, (type_key, description) in enumerate(action_types, start=1):
            print(f"  {idx}. {description}")

        raw = input("Select action type (1-3): ").strip()
        if not raw:
            print("⚠️  Please enter a number between 1 and 3.")
            continue

        try:
            choice = int(raw)
            if 1 <= choice <= 3:
                return action_types[choice - 1][0]
            print("⚠️  Please enter a number between 1 and 3.")
        except ValueError:
            print("⚠️  Please enter a valid number.")


def select_condition_field() -> tuple[str, str]:
    """Let user choose a condition field type.

    Returns:
        Tuple of (field_type, field_name) where:
        - field_type: "header" | "keyword" | "age"
        - field_name: field name for headers, or "" for keyword/age
    """
    print("\nSelect condition field:")
    print("  1. From (sender address)")
    print("  2. To (recipient address)")
    print("  3. Subject")
    print("  4. List-ID")
    print("  5. Reply-To")
    print("  6. Keyword/Flag (IMAP keywords)")
    print("  7. Age (message age in days)")
    print("  8. Other (enter custom header)")

    while True:
        choice = input("  > ").strip()
        field_map = {
            "1": ("header", "from"),
            "2": ("header", "to"),
            "3": ("header", "subject"),
            "4": ("header", "list-id"),
            "5": ("header", "reply-to"),
            "6": ("keyword", ""),
            "7": ("age", ""),
        }
        if choice in field_map:
            return field_map[choice]
        elif choice == "8":
            custom = prompt("  Enter header name: ")
            return ("header", custom.lower())
        print("⚠️  Please enter a number 1-8.")


def select_header_field() -> str:
    """Backward compatibility wrapper for select_condition_field()."""
    field_type, field_name = select_condition_field()
    if field_type == "header":
        return field_name
    # Fallback if user selects non-header (shouldn't happen in normal usage)
    return "from"


def make_condition() -> dict[str, Any]:
    """Create a new match condition using prompts."""

    field_type, field_name = select_condition_field()

    if field_type == "header":
        # Create header condition (existing logic)
        match_field = _get_match_type_menu(header=field_name)
        value = prompt("  Match value: ")
        return {"header": field_name, match_field: value}

    elif field_type == "keyword":
        # Create keyword condition
        print("\nKeyword operator:")
        print("  1. Has keyword (message must have this keyword/flag)")
        print("  2. Lacks keyword (message must NOT have this keyword/flag)")

        while True:
            choice = input("  > ").strip()
            if choice == "1":
                keyword = prompt("  Keyword/Flag: ")
                return {"has_keyword": keyword}
            elif choice == "2":
                keyword = prompt("  Keyword/Flag: ")
                return {"lacks_keyword": keyword}
            else:
                print("⚠️  Please enter 1 or 2.")

    elif field_type == "age":
        # Create age condition
        print("\nAge operator:")
        print("  1. Older than N days (age > N)")
        print("  2. Younger than N days (age < N)")
        print("  3. Exactly N days old (age = N)")

        while True:
            choice = input("  > ").strip()
            if choice == "1":
                days = prompt_int("  Number of days: ", default=0)
                return {"age_days_gt": days}
            elif choice == "2":
                days = prompt_int("  Number of days: ", default=0)
                return {"age_days_lt": days}
            elif choice == "3":
                days = prompt_int("  Number of days: ", default=0)
                return {"age_days_eq": days}
            else:
                print("⚠️  Please enter 1, 2, or 3.")

    # Fallback (shouldn't reach here)
    return {"header": "from", "contains": ""}


def _parse_number_selection(text: str, max_num: int) -> list[int] | None:
    """Parse number selection like '1,3,5' or '1-3' and return list of indices.

    Args:
        text: User input (e.g., '1,3,5' or '1-3')
        max_num: Maximum valid number

    Returns:
        List of valid 0-based indices, or None if invalid
    """
    if not text.strip():
        return None

    indices = set()
    try:
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                # Handle range (e.g., '2-5')
                range_parts = part.split("-")
                if len(range_parts) != 2:
                    return None
                start = int(range_parts[0].strip())
                end = int(range_parts[1].strip())
                if start < 1 or end < 1 or start > max_num or end > max_num or start > end:
                    return None
                indices.update(range(start - 1, end))  # Convert to 0-based
            else:
                # Handle single number
                num = int(part)
                if num < 1 or num > max_num:
                    return None
                indices.add(num - 1)  # Convert to 0-based

        return sorted(list(indices))
    except ValueError:
        return None


def _quick_group_conditions(
    children: list[Any], node: dict[str, Any]
) -> tuple[bool, Any | None]:
    """Interactive quick grouping of multiple conditions.

    Args:
        children: List of current conditions
        node: The parent group node

    Returns:
        Tuple of (success, new_group_to_add)
    """
    if len(children) < 2:
        print("⚠️  Need at least 2 conditions to group.")
        return False, None

    print("\nAvailable conditions to group:")
    for i, child in enumerate(children, 1):
        summary = summarise_condition(child)
        print(f"  {i}. {summary}")

    text = input("\nEnter numbers to group (e.g., 1,3,5 or 1-3): ").strip()
    indices = _parse_number_selection(text, len(children))

    if not indices or len(indices) < 2:
        print("⚠️  Please select at least 2 conditions.")
        return False, None

    print("\nGroup these conditions with:")
    print("  1. ALL (AND) - all must match")
    print("  2. ANY (OR) - any can match")
    logic_choice = input("  > ").strip()

    if logic_choice == "1":
        logic = "all"
    elif logic_choice == "2":
        logic = "any"
    else:
        print("⚠️  Please enter 1 or 2.")
        return False, None

    # Create new group with selected conditions
    selected_children = [children[i] for i in indices]
    new_group = {logic: selected_children}

    # Show preview
    print("\n" + "=" * 60)
    print("Group preview:")
    print("=" * 60)
    print(_format_condition_tree(new_group))
    print("=" * 60)

    if not confirm("Create this group?", default=True):
        return False, None

    # Remove selected conditions from parent (in reverse order to avoid index shifting)
    for i in sorted(indices, reverse=True):
        children.pop(i)

    return True, new_group


def edit_condition_group(node: dict[str, Any]) -> dict[str, Any]:
    """Interactive editor for logical condition groups."""

    node = ensure_group(node)
    while True:
        key = "all" if "all" in node else "any"
        children = node.setdefault(key, [])
        mode_label = "ALL (AND)" if key == "all" else "ANY (OR)"

        # Display current tree structure
        if children:
            print("\n" + "=" * 60)
            print("Current conditions:")
            print("=" * 60)
            tree_display = _format_condition_tree(node)
            print(tree_display)
            print("=" * 60 + "\n")

        options: list[tuple[str, str]] = [
            ("a", "Add condition"),
            ("g", "Add group"),
        ]
        if children:
            options.extend([
                ("q", "Quick group (select multiple)"),
                ("e", "Edit existing entry"),
                ("r", "Remove entry"),
            ])
        options.extend([("t", f"Toggle mode (currently {mode_label})"), ("b", "Back")])

        action = choose_menu_option("Condition group editor", options)
        if action in {None, "b"}:
            return node
        if action == "t":
            other_key = "any" if key == "all" else "all"
            node = {other_key: [normalise_condition(child) for child in children]}
            continue
        if action == "a":
            children.append(make_condition())
            continue
        if action == "g":
            new_group = edit_condition_group({"all": []})
            # Show preview of the created group before adding it
            if new_group.get("all") or new_group.get("any"):
                print("\n" + "=" * 60)
                print("Group preview:")
                print("=" * 60)
                print(_format_condition_tree(new_group))
                print("=" * 60)
                if confirm("Add this group?", default=True):
                    children.append(new_group)
            continue
        if action == "q":
            success, new_group = _quick_group_conditions(children, node)
            if success and new_group:
                children.append(new_group)
            continue
        if not children:
            print("⚠️  No entries to operate on.")
            continue
        summaries = [summarise_condition(child) for child in children]
        index = choose_from_list("Select condition entry", summaries)
        if index is None:
            continue
        if action == "r":
            children.pop(index)
            continue
        child = children[index]
        if isinstance(child, dict) and ("all" in child or "any" in child):
            children[index] = edit_condition_group(child)
        elif isinstance(child, list):
            children[index] = edit_condition_group({"all": child})
        else:
            # Route to appropriate editor based on condition type
            child_dict = dict(child)
            cond_type = _detect_condition_type(child_dict)

            if cond_type == "keyword":
                children[index] = edit_keyword_condition(child_dict)
            elif cond_type == "age":
                children[index] = edit_age_condition(child_dict)
            else:  # header or unknown
                children[index] = edit_simple_condition(child_dict)


def edit_action_block(actions: Any) -> list[dict[str, Any]]:
    """Interactive editor for the rule's action block (list of actions)."""

    # Normalize to list
    if isinstance(actions, dict):
        actions = [actions]
    elif not isinstance(actions, list):
        actions = []

    if not actions:
        actions = [{"type": "move", "target": ""}]

    while True:
        labels = []
        for i, action in enumerate(actions, 1):
            summary = summarise_action(action)
            labels.append(f"{i}. {summary}")

        add_index = len(labels)
        back_index = add_index + 1
        labels.append("➕ Add action")
        labels.append("⬅ Back")

        choice = choose_from_list("Actions editor", labels)
        if choice is None or choice == back_index:
            return actions
        if choice == add_index:
            # Add new action
            new_action = {"type": "move", "target": ""}
            actions.append(new_action)
            continue

        # Edit existing action
        action = actions[choice]
        edit_menu_options = [("e", "Edit"), ("r", "Remove"), ("b", "Back")]
        action_choice = choose_menu_option(f"Action {choice + 1}: {summarise_action(action)}", edit_menu_options)

        if action_choice in {None, "b"}:
            continue
        if action_choice == "r":
            actions.pop(choice)
            continue
        if action_choice == "e":
            actions[choice] = _edit_single_action(dict(action))

    return actions


def _edit_single_action(action: dict[str, Any]) -> dict[str, Any]:
    """Interactive editor for a single action within an action list."""

    if not action:
        action = {"type": "move", "target": ""}

    while True:
        act_type = action.get("type", "move")
        target = action.get("target", "")
        keywords = action.get("keywords", [])

        # Build menu options based on action type
        options: list[tuple[str, str]] = [("t", f"Type: {act_type}")]

        if act_type == "move":
            options.append(("r", f"Target folder: {target or '<unset>'}"))
        elif act_type in ("set_keywords", "remove_keywords"):
            keywords_summary = ", ".join(keywords) if keywords else "<unset>"
            options.append(("k", f"Keywords: {keywords_summary}"))

        # Add extras and back options
        extras = {k: v for k, v in action.items() if k not in {"type", "target", "keywords"}}
        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in extras.items()
        ) or "(none)"
        options.append(("x", f"Extras: {extras_summary}"))
        options.append(("b", "Back"))

        choice = choose_menu_option("Action editor", options)

        if choice in {None, "b"}:
            return action

        if choice == "t":
            new_type = _get_action_type_menu()
            # Clean up old fields when changing type
            if new_type != act_type:
                action["type"] = new_type
                action.pop("target", None)
                action.pop("keywords", None)
                if new_type == "move":
                    action["target"] = ""
                else:
                    action["keywords"] = []

        elif choice == "r" and act_type == "move":
            action["target"] = prompt("  Target folder: ", allow_empty=True)

        elif choice == "k" and act_type in ("set_keywords", "remove_keywords"):
            # Edit keywords array
            keywords = action.get("keywords", [])
            while True:
                print("\nCurrent keywords:")
                if keywords:
                    for i, kw in enumerate(keywords, 1):
                        print(f"  {i}. {kw}")
                else:
                    print("  (none)")

                print("\nOptions:")
                print("  a. Add keyword")
                print("  r. Remove keyword")
                print("  c. Clear all")
                print("  d. Done")

                kw_choice = input("  > ").strip().lower()

                if kw_choice == "a":
                    kw = prompt("Enter keyword: ")
                    if kw and kw not in keywords:
                        keywords.append(kw)
                        print(f"✓ Added: {kw}")

                elif kw_choice == "r":
                    if keywords:
                        idx_str = input("Enter number to remove: ").strip()
                        try:
                            idx = int(idx_str) - 1
                            if 0 <= idx < len(keywords):
                                removed = keywords.pop(idx)
                                print(f"✓ Removed: {removed}")
                        except ValueError:
                            print("⚠️  Invalid number")

                elif kw_choice == "c":
                    if confirm("Clear all keywords?", default=False):
                        keywords.clear()
                        print("✓ Cleared all keywords")

                elif kw_choice == "d":
                    action["keywords"] = keywords
                    break

        elif choice == "x":
            extras = {k: v for k, v in action.items() if k not in {"type", "target", "keywords"}}
            edit_generic_dict(extras)
            for key in list(action):
                if key not in {"type", "target", "keywords"}:
                    action.pop(key)
            action.update(extras)


def edit_comments_list(items: list[str]) -> list[str]:
    """Interactive list editor for comment strings."""

    while True:
        labels = list(items)
        add_index = len(labels)
        back_index = add_index + 1
        labels.append("➕ Add comment")
        labels.append("⬅ Back")

        choice = choose_from_list("Comments", labels)
        if choice is None or choice == back_index:
            return items
        if choice == add_index:
            items.append(prompt("  Comment: "))
            continue
        action = choose_menu_option(
            f"Comment: {items[choice]}",
            [("e", "Edit"), ("r", "Remove"), ("b", "Back")],
        )
        if action in {None, "b"}:
            continue
        if action == "e":
            items[choice] = prompt("  New comment: ")
        elif action == "r":
            items.pop(choice)


# ---------------------------------------------------------------------------
# High level rule manager

@dataclass
class RuleRecord:
    """Lightweight container for a rule loaded from disk."""

    file: Path
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.data.get("name", "<unnamed>"))

    @property
    def priority(self) -> int:
        try:
            return int(self.data.get("priority", 100))
        except (TypeError, ValueError):
            return 100

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", True))


class RuleManager:
    """Interactive console orchestrator."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.config = build_default_config(base_dir)
        self.rules_dir = self.config.paths.rules_dir
        self.logger = JsonLogger(self.config.paths.log_file)
        self.refresh_rules()

    # ------------------------------------------------------------------ utils
    def refresh_rules(self) -> None:
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        rules = load_rules(self.rules_dir, self.logger, skip_disabled=False)
        self.rules: list[RuleRecord] = []
        for raw in rules:
            file_name = raw.pop("_file", None)
            path = self.rules_dir / (file_name or f"{slugify(raw.get('name', 'rule'))}.json")
            self.rules.append(RuleRecord(path, raw))
        self.rules.sort(key=lambda rule: (rule.priority, rule.name.lower()))

    def save_rule(self, rule: RuleRecord) -> None:
        data = dict(rule.data)
        data.pop("_file", None)

        # Validate rule before saving
        validator = RuleValidator()
        is_valid, warnings = validator.validate_rule(data)

        if warnings:
            print("\n" + "=" * 60)
            print("⚠️ RULE VALIDATION WARNINGS")
            print("=" * 60)
            for warning in warnings:
                print(f"  • {warning}")
            print("=" * 60)

            if not confirm("\nSave rule anyway?", default=False):
                print("❌ Rule not saved.")
                return

            # Check for Hollister pattern and offer suggestion
            suggestion = validator.suggest_fix_for_rule(data)
            if suggestion:
                print("\n" + "=" * 60)
                print(f"💡 SUGGESTION FOR: {suggestion['name']}")
                print("=" * 60)
                print(f"Issue: {suggestion['issue']}")
                print(f"\nDescription: {suggestion['description']}")
                print(f"\nSuggestion: {suggestion['suggestion']}")
                print("=" * 60)

        with rule.file.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print("✓ Rule saved.")


    def select_rule(self) -> RuleRecord | None:
        if not self.rules:
            print("No rules are available yet.")
            return None
        rule_labels = [
            f"[{rule.priority:>4}] {rule.name} — {summarise_action(rule.data.get('actions', rule.data.get('action')))}"
            + ("  [DISABLED]" if not rule.enabled else "")
            for rule in self.rules
        ]
        selection = choose_from_list("Available rules (sorted by priority):", rule_labels)
        if selection is None:
            return None
        return self.rules[selection]

    def extract_folders(self) -> dict[str, list[RuleRecord]]:
        """Extract unique folders and map them to rules.

        Scans all rules for move actions and builds a mapping of destination
        folders to the rules that move emails to those folders.

        Returns:
            Dictionary mapping folder paths to lists of RuleRecords that move
            to that folder, with rules sorted by priority within each folder.
        """
        folders: dict[str, list[RuleRecord]] = {}

        for rule in self.rules:
            # Support both 'actions' (new) and 'action' (legacy) fields
            actions = rule.data.get('actions', rule.data.get('action', []))
            if isinstance(actions, dict):
                actions = [actions]

            for action in actions:
                if isinstance(action, dict) and action.get('type') == 'move':
                    target = action.get('target', '')
                    if target:  # Skip empty targets
                        if target not in folders:
                            folders[target] = []
                        folders[target].append(rule)

        # Sort rules within each folder by priority
        for folder in folders:
            folders[folder].sort(key=lambda r: (r.priority, r.name.lower()))

        return folders

    def view_rules_by_folder(self) -> None:
        """Display rules organized by target folder for troubleshooting.

        Allows users to browse rules grouped by their destination folder
        (from move actions), making it easier to find and troubleshoot
        rules affecting a specific folder.
        """
        folders = self.extract_folders()

        if not folders:
            print("No folders found. Rules may not have move actions.")
            return

        while True:
            # Display folder list with rule counts
            folder_list = sorted(folders.keys())
            folder_labels = [
                f"{folder} ({len(folders[folder])} rule{'s' if len(folders[folder]) != 1 else ''})"
                for folder in folder_list
            ]

            selection = choose_from_list(
                "📁 View rules by folder — select a folder:",
                folder_labels,
            )

            if selection is None:
                return

            selected_folder = folder_list[selection]
            rules_for_folder = folders[selected_folder]

            # Display rules for selected folder
            while True:
                rule_labels = [
                    f"[{rule.priority:>4}] {rule.name} — {summarise_action(rule.data.get('actions', rule.data.get('action')))}"
                    for rule in rules_for_folder
                ]

                rule_selection = choose_from_list(
                    f"Rules moving to: {selected_folder}",
                    rule_labels,
                )

                if rule_selection is None:
                    break

                selected_rule = rules_for_folder[rule_selection]
                self.edit_rule(selected_rule)

    def generate_filename(self, name: str) -> Path:
        slug = slugify(name)
        numeric_prefixes: list[int] = []
        for record in self.rules:
            stem = record.file.stem
            prefix = ""
            for ch in stem:
                if ch.isdigit():
                    prefix += ch
                else:
                    break
            if prefix:
                try:
                    numeric_prefixes.append(int(prefix))
                except ValueError:
                    continue
        next_id = max(
            numeric_prefixes,
            default=int(datetime.now().strftime("%y%j%H%M")),
        ) + 1
        filename = f"{next_id:05d}_{slug}.json"
        return self.rules_dir / filename

    # --------------------------------------------------------------- operations
    def create_rule(self) -> None:
        print("\n➕ Create a new rule")
        name = prompt("  Rule name: ")
        priority = prompt_int("  Priority [100]: ", default=100)
        filename = self.generate_filename(name)
        if not confirm(f"Save rule as {filename.name}?", default=True):
            print("❌ Creation cancelled.")
            return
        record = RuleRecord(
            file=filename,
            data={
                "name": name,
                "priority": priority,
                "conditions": {"all": []},
                "actions": [{"type": "move", "target": ""}],
            },
        )
        self.rules.append(record)
        self.edit_rule(record, new_rule=True)
        self.rules.sort(key=lambda rule: (rule.priority, rule.name.lower()))

    def edit_rule(self, rule: RuleRecord, *, new_rule: bool = False) -> None:
        while True:
            comments = rule.data.get("comments") or []
            # Support both 'actions' (new) and 'action' (legacy) fields
            actions_field = rule.data.get('actions', rule.data.get('action'))
            enabled_label = "Yes" if rule.enabled else "No"
            options = [
                ("e", f"Enabled   : {enabled_label}"),
                ("n", f"Name      : {rule.data.get('name', '<unnamed>')}"),
                ("p", f"Priority  : {rule.priority}"),
                ("c", f"Conditions: {summarise_condition(rule.data.get('conditions'))}"),
                ("a", f"Actions   : {summarise_action(actions_field)}"),
                ("o", f"Comments ({len(comments)})" if comments else "Comments (none)"),
                ("x", "Edit extra fields"),
                ("t", "Dry-run test"),
                ("s", "Save & exit"),
                ("b", "Back"),
            ]
            title = f"🛠️  Editing rule — {rule.file.name}"
            if not rule.enabled:
                title += "  [DISABLED]"
            action = choose_menu_option(title, options,)
            if action in {None, "b"}:
                if new_rule and confirm("Discard this new rule?", default=False):
                    self.rules.remove(rule)
                    if rule.file.exists():
                        rule.file.unlink()
                    print("🗑️  Rule discarded.")
                return
            if action == "s":
                self.save_rule(rule)
                print("✅ Rule saved.")
                if new_rule:
                    print("🎉 New rule created. Returning to main menu.")
                return
            if action == "e":
                if rule.enabled:
                    rule.data["enabled"] = False
                    print("  Rule disabled.")
                else:
                    rule.data.pop("enabled", None)
                    print("  Rule enabled.")
            elif action == "n":
                rule.data["name"] = prompt("  Rule name: ")
            elif action == "p":
                rule.data["priority"] = prompt_int("  Priority: ", default=rule.priority)
            elif action == "c":
                existing = rule.data.get("conditions")
                rule.data["conditions"] = edit_condition_group(existing or {"all": []})
            elif action == "a":
                rule.data["actions"] = edit_action_block(rule.data.get("actions", rule.data.get("action") or []))
                # Remove legacy 'action' field if present
                rule.data.pop("action", None)
            elif action == "o":
                items = list(rule.data.get("comments") or [])
                rule.data["comments"] = edit_comments_list(items)
            elif action == "x":
                _managed = {"name", "priority", "conditions", "actions", "action", "comments", "enabled"}
                extras = {key: value for key, value in rule.data.items() if key not in _managed}
                edit_generic_dict(extras)
                for key in list(rule.data):
                    if key not in _managed:
                        rule.data.pop(key)
                rule.data.update(extras)
            elif action == "t":
                self.test_rule(rule)

    def delete_rule(self) -> None:
        rule = self.select_rule()
        if not rule:
            return
        if not confirm(f"Delete {rule.file.name}?", default=False):
            return
        if rule.file.exists():
            backup = rule.file.with_suffix(".bak")
            shutil.copy2(rule.file, backup)
            rule.file.unlink()
            print(f"🗑️  Rule deleted. Backup saved to {backup.name}.")
        else:
            print("🗑️  Rule removed (file did not exist on disk).")
        self.rules.remove(rule)

    def batch_delete_rules(self) -> None:
        """Delete multiple rules at once with confirmation."""
        if not self.rules:
            print("No rules are available yet.")
            return

        # Display all rules
        print("\n📋 Available rules (for batch deletion):")
        for i, rule in enumerate(self.rules, 1):
            action_summary = summarise_action(rule.data.get('actions', rule.data.get('action')))
            print(f"  {i:>3}. [{rule.priority:>4}] {rule.name} — {action_summary}")

        # Get rule numbers to delete
        while True:
            user_input = input("\nEnter rule numbers to delete (e.g., 1,3,5 or 1-5): ").strip()
            if not user_input:
                print("Batch deletion cancelled.")
                return

            # Parse user input (support both comma-separated and ranges)
            indices_to_delete = set()
            try:
                for part in user_input.split(","):
                    part = part.strip()
                    if "-" in part:
                        # Handle range like "1-5"
                        start, end = part.split("-")
                        start, end = int(start.strip()), int(end.strip())
                        indices_to_delete.update(range(start, end + 1))
                    else:
                        # Handle single number
                        indices_to_delete.add(int(part))

                # Validate indices
                invalid = [i for i in indices_to_delete if i < 1 or i > len(self.rules)]
                if invalid:
                    print(f"⚠️  Invalid rule numbers: {invalid}. Please try again.")
                    continue

                break
            except ValueError:
                print("⚠️  Invalid input format. Please enter numbers (e.g., 1,3,5 or 1-5).")

        # Sort indices in reverse so deletion doesn't affect remaining indices
        sorted_indices = sorted(indices_to_delete, reverse=True)

        # Show selected rules for confirmation
        print(f"\n🗑️  Will delete {len(sorted_indices)} rule(s):")
        for idx in sorted(indices_to_delete):  # Show in original order
            rule = self.rules[idx - 1]
            print(f"    - {rule.name}")

        # Confirm deletion
        if not confirm("\nDelete these rules?", default=False):
            print("Batch deletion cancelled.")
            return

        # Delete selected rules
        deleted_count = 0
        for idx in sorted_indices:
            rule = self.rules[idx - 1]
            if rule.file.exists():
                backup = rule.file.with_suffix(".bak")
                shutil.copy2(rule.file, backup)
                rule.file.unlink()
                print(f"  ✓ Deleted: {rule.name} (backup: {backup.name})")
            else:
                print(f"  ✓ Removed: {rule.name} (file did not exist)")
            deleted_count += 1

        # Update rules list
        for idx in sorted_indices:
            self.rules.pop(idx - 1)

        print(f"\n✅ Successfully deleted {deleted_count} rule(s).")

    def test_rule(self, rule: RuleRecord) -> None:
        db_path = self.config.paths.db_file
        if not db_path.exists():
            print("⚠️  Cache database not found. Build the cache before testing.")
            return
        print("\nRunning dry-run evaluation for this rule…")
        db = init_db(db_path, logger=self.logger)
        try:
            _, _, matches = evaluate_rules(
                db,
                [rule.data],
                scope="all",
                dry_run=True,
                show_progress=False,
                logger=self.logger,
            )
            print(f"🎯 Dry run complete — matches found: {matches}")
        finally:
            db.close()

    def reorder_priorities(self) -> None:
        if not self.rules:
            print("No rules available to reorder.")
            return

        ordered = list(self.rules)
        while True:
            labels = [f"[{rule.priority:>4}] {rule.name}" for rule in ordered]
            save_index = len(labels)
            back_index = save_index + 1
            labels.append("💾 Save changes")
            labels.append("⬅ Back")

            selection = choose_from_list("Priority manager — select a rule", labels)
            if selection is None or selection == back_index:
                return
            if selection == save_index:
                for record in ordered:
                    self.save_rule(record)
                self.refresh_rules()
                print("✅ Priorities updated.")
                return

            rule = ordered[selection]
            action = choose_menu_option(
                f"Adjust priority — [{rule.priority:>4}] {rule.name}",
                [("u", "Move up"), ("d", "Move down"), ("e", "Edit value"), ("b", "Back")],
            )
            if action in {None, "b"}:
                continue
            if action == "u":
                if selection == 0:
                    print("⚠️  Already at the top.")
                    continue
                current = ordered[selection]
                above = ordered[selection - 1]
                ordered[selection - 1], ordered[selection] = current, above
                current_priority = current.data.get("priority", current.priority)
                above_priority = above.data.get("priority", above.priority)
                current.data["priority"], above.data["priority"] = above_priority, current_priority
            elif action == "d":
                if selection == len(ordered) - 1:
                    print("⚠️  Already at the bottom.")
                    continue
                current = ordered[selection]
                below = ordered[selection + 1]
                ordered[selection + 1], ordered[selection] = current, below
                current_priority = current.data.get("priority", current.priority)
                below_priority = below.data.get("priority", below.priority)
                current.data["priority"], below.data["priority"] = below_priority, current_priority
            elif action == "e":
                rule.data["priority"] = prompt_int("  New priority: ", default=rule.priority)

    def run(self) -> None:
        while True:
            action = choose_menu_option(
                "📬 IMAPFilter Rule Manager",
                [
                    ("l", "List rules"),
                    ("f", "View rules by folder"),
                    ("c", "Create rule"),
                    ("e", "Edit rule"),
                    ("d", "Delete rule"),
                    ("b", "Batch delete rules"),
                    ("p", "Priority manager"),
                    ("r", "Reload from disk"),
                    ("q", "Quit"),
                ],
            )
            if action in {None, "q"}:
                print("Goodbye!")
                return
            if action == "r":
                self.refresh_rules()
                print("🔄 Rules reloaded from disk.")
            elif action == "l":
                self.refresh_rules()
                self.select_rule()  # Listing happens within select_rule
            elif action == "f":
                self.refresh_rules()
                self.view_rules_by_folder()
            elif action == "c":
                self.refresh_rules()
                self.create_rule()
            elif action == "e":
                self.refresh_rules()
                rule = self.select_rule()
                if rule:
                    self.edit_rule(rule)
            elif action == "d":
                self.refresh_rules()
                self.delete_rule()
            elif action == "b":
                self.refresh_rules()
                self.batch_delete_rules()
            elif action == "p":
                self.refresh_rules()
                self.reorder_priorities()
            else:
                print("⚠️  Unknown option. Please choose one of the highlighted letters.")


def main() -> int:
    try:
        manager = RuleManager()
    except Exception as exc:  # pragma: no cover - defensive startup handling
        print(f"❌ Failed to initialise rule manager: {exc}")
        return 1
    try:
        manager.run()
    except KeyboardInterrupt:
        print("\nInterrupted — exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
