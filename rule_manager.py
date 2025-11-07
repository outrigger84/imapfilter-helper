#!/usr/bin/env python3
"""Interactive rule management console for IMAPFilter Helper.

This tool provides a lightweight text-based UI (similar to the Proxmox
Helper Scripts) for inspecting, creating, and editing rule definition files
stored under the ``rules/`` directory.  Keyboard shortcuts are available for
each action to keep the workflow fully navigable from the keyboard.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - terminal capability detection is environment-specific
    import curses
except Exception:  # pragma: no cover - gracefully degrade when curses is missing
    curses = None  # type: ignore[assignment]

from core.config import build_default_config
from core.database import init_db
from core.logging_utils import JsonLogger
from core.rule_engine import evaluate_rules, load_rules


def prompt(message: str, *, allow_empty: bool = False) -> str:
    """Return user input for *message*, repeating until non-empty if needed."""

    while True:
        value = input(message).strip()
        if value or allow_empty:
            return value
        print("⚠️  Please enter a value or press CTRL+C to cancel.")


def prompt_int(message: str, *, default: int | None = None) -> int:
    """Prompt for an integer value, repeating until a valid number is entered."""

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


def slugify(value: str) -> str:
    """Return a filesystem-safe slug derived from *value*."""

    safe = []
    for ch in value.lower():
        if ch.isalnum():
            safe.append(ch)
        elif ch in {" ", "-", ".", "/", "_"}:
            safe.append("_")
    slug = "".join(safe).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "rule"


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
        header = node.get("header", "<header>")
        if "regex" in node:
            return f"{header} ~= {node['regex']}"
        if "contains" in node:
            return f"{header} ⊃ {node['contains']}"
        return f"{header} (custom)"
    if isinstance(node, list):
        return f"Implicit ALL ({len(node)})"
    return str(node)


def summarise_action(action: Any) -> str:
    """Return a concise description of an action block."""

    if not isinstance(action, dict):
        return "(no action)"
    act_type = action.get("type", "move")
    target = action.get("target")
    if target:
        summary = f"{act_type} → {target}"
    else:
        summary = act_type
    extras = [key for key in action.keys() if key not in {"type", "target"}]
    if extras:
        summary += f" (+{len(extras)} extra field{'s' if len(extras) != 1 else ''})"
    return summary


def interactive_menu(title: str, options: list[str]) -> int | None:
    """Display *options* using an interactive, scrollable curses menu.

    Returns the selected index, or ``None`` if the user aborts with ESC or ``q``.
    Raises ``RuntimeError`` if curses support is unavailable in this environment."""

    if curses is None or not sys.stdin.isatty() or not sys.stdout.isatty():
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

            # Adjust viewport to keep the current selection visible.
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


def choose_menu_option(
    title: str,
    options: list[tuple[str, str]],
    *,
    prompt_text: str = "Select action: ",
) -> str | None:
    """Return the hotkey for the selected *options* or ``None`` if cancelled."""

    labels = [f"[{key.upper()}] {label}" for key, label in options]
    try:
        choice = interactive_menu(title, labels)
    except RuntimeError:
        choice = None
    if choice is not None:
        return options[choice][0]
    if choice is None and curses is not None:
        return None

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


def choose_from_list(
    title: str,
    entries: list[str],
    *,
    prompt_text: str = "Enter number (blank to cancel): ",
    allow_cancel: bool = True,
) -> int | None:
    """Return the index of the selected entry or ``None`` if cancelled."""

    if not entries:
        return None
    try:
        choice = interactive_menu(title, entries)
    except RuntimeError:
        choice = None
    if choice is not None:
        return choice
    if choice is None and curses is not None:
        return None

    while True:
        print(f"\n{title}")
        for idx, label in enumerate(entries, start=1):
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
        if 0 <= index < len(entries):
            return index
        print("⚠️  Number out of range.")


def ensure_group(node: Any) -> dict:
    """Normalise *node* into the internal group representation."""

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
    """Recursively ensure nested groups are using the internal structure."""

    if isinstance(node, dict) and ("all" in node or "any" in node):
        key = "all" if "all" in node else "any"
        items = node.get(key)
        if not isinstance(items, list):
            items = [items]
        return {key: [normalise_condition(item) for item in items]}
    if isinstance(node, list):
        return [normalise_condition(item) for item in node]
    return node


def edit_generic_dict(data: dict[str, Any], *, protected: Iterable[str] = ()) -> None:
    """Generic key/value editor for arbitrary dictionaries."""

    protected = set(protected)
    while True:
        items = list(data.items())
        labels = [
            f"{'*' if key in protected else ' '} {key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in items
        ]
        add_index = len(labels)
        back_index = add_index + 1
        labels.append("➕ Add new entry")
        labels.append("⬅ Back")

        selection = choose_from_list(
            "Additional fields", labels, prompt_text="Select entry (blank to cancel): "
        )
        if selection is None or selection == back_index:
            return
        if selection == add_index:
        print("\nCurrent values:")
        if not data:
            print("  (no entries)")
        else:
            for key, value in data.items():
                marker = "*" if key in protected else " "
                print(f"  {marker} {key}: {json.dumps(value, ensure_ascii=False)}")
        print("Options: [A]dd  [E]dit  [R]emove  [B]ack")
        choice = input("Select action: ").strip().lower()
        if not choice:
            continue
        if choice[0] == "b":
            return
        if choice[0] == "a":
            key = prompt("  Key: ")
            if key in protected:
                print("⚠️  That key is managed elsewhere.")
                continue
            raw = prompt("  Value (JSON encoded): ")
            try:
                data[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"⚠️  Invalid JSON: {exc}")
            continue

        key, value = items[selection]
        if key in protected:
            print("⚠️  That key is managed elsewhere.")
            continue

        action = choose_menu_option(
            f"Entry {key} = {json.dumps(value, ensure_ascii=False)}",
            [("e", "Edit value"), ("r", "Remove entry"), ("b", "Back")],
        )
        if action is None or action == "b":
            continue
        if action == "e":
        if choice[0] == "e":
            key = prompt("  Key to edit: ")
            if key not in data:
                print("⚠️  Unknown key.")
                continue
            if key in protected:
                print("⚠️  That key is managed elsewhere.")
                continue
            raw = prompt("  New value (JSON encoded): ")
            try:
                data[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"⚠️  Invalid JSON: {exc}")
            continue
        if action == "r":
            del data[key]
        if choice[0] == "r":
            key = prompt("  Key to remove: ")
            if key not in data:
                print("⚠️  Unknown key.")
                continue
            if key in protected:
                print("⚠️  That key cannot be removed here.")
                continue
            del data[key]
            continue


def edit_simple_condition(node: dict[str, Any]) -> dict[str, Any]:
    """Interactively edit a single header match condition."""

    while True:
        header = node.get("header", "")
        match_field = "regex" if "regex" in node else "contains"
        match_value = node.get(match_field, "") if match_field in node else ""
        other_keys = {k: v for k, v in node.items() if k not in {"header", "contains", "regex"}}

        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in other_keys.items()
        )
        if not extras_summary:
            extras_summary = "(none)"

        options = [
            ("h", f"Header     : {header or '<unset>'}"),
            ("m", f"Match type : {match_field}"),
            ("v", f"Value      : {match_value}"),
            ("x", f"Extras     : {extras_summary}"),
            ("b", "Back"),
        ]
        action = choose_menu_option("Condition editor", options)
        if action is None:
            action = "b" if any(opt[0] == "b" for opt in options) else None
        if not action:
            continue
        print("\nCondition editor")
        print(f"  Header     : {header or '<unset>'}")
        print(f"  Match type : {match_field}")
        print(f"  Value      : {match_value}")
        if other_keys:
            print("  Extras     :")
            for key, value in other_keys.items():
                print(f"    - {key} = {json.dumps(value, ensure_ascii=False)}")
        else:
            print("  Extras     : (none)")

        print("Options: [H]eader  [M]atch type  [V]alue  E[x]tras  [B]ack")
        choice = input("Select action: ").strip().lower()
        if not choice:
            continue
        action = choice[0]
        if action == "b":
            return node
        if action == "h":
            node["header"] = prompt("  Header name: ")
            continue
        if action == "m":
            new_type = prompt("  Match field (contains/regex): ").lower()
            if new_type not in {"contains", "regex"}:
                print("⚠️  Expected 'contains' or 'regex'.")
                continue
            value = prompt("  Match value: ")
            node.pop("contains", None)
            node.pop("regex", None)
            node[new_type] = value
            continue
        if action == "v":
            if match_field not in {"contains", "regex"}:
                print("⚠️  Set the match type first.")
                continue
            node[match_field] = prompt("  Match value: ", allow_empty=True)
            continue
        if action == "x":
            extras = {k: v for k, v in node.items() if k not in {"header", "contains", "regex"}}
            edit_generic_dict(extras)
            for key in list(node):
                if key not in {"header", "contains", "regex"}:
                    node.pop(key)
            node.update(extras)
            continue


def make_condition() -> dict[str, Any]:
    """Create a new match condition using interactive prompts."""

    header = prompt("  Header name: ")
    while True:
        match_field = prompt("  Match field (contains/regex): ").lower()
        if match_field in {"contains", "regex"}:
            break
        print("⚠️  Please enter either 'contains' or 'regex'.")
    value = prompt("  Match value: ")
    return {"header": header, match_field: value}


def edit_condition_group(node: dict[str, Any]) -> dict[str, Any]:
    """Interactive editor for logical condition groups."""

    node = ensure_group(node)
    while True:
        key = "all" if "all" in node else "any"
        children = node.get(key, [])
        mode_label = "ALL (AND)" if key == "all" else "ANY (OR)"
        options: list[tuple[str, str]] = [
            ("a", "Add condition"),
            ("g", "Add group"),
        ]
        if children:
            options.extend(
                [
                    ("e", "Edit existing entry"),
                    ("r", "Remove entry"),
                ]
            )
        options.extend([("t", f"Toggle mode (currently {mode_label})"), ("b", "Back")])

        action = choose_menu_option("Condition group editor", options)
        if action is None:
            action = "b" if any(opt[0] == "b" for opt in options) else None
        if not action:
            continue
        if action == "b":
            return node
        if action == "t":
            other_key = "any" if key == "all" else "all"
            node = {other_key: [normalise_condition(child) for child in children]}
        print("\nCondition group editor")
        print(f"  Mode : {'ALL (AND)' if key == 'all' else 'ANY (OR)'}")
        if not children:
            print("  (no entries)")
        else:
            for idx, child in enumerate(children, start=1):
                print(f"  {idx:>3}. {summarise_condition(child)}")

        print(
            "Options: [A]dd condition  add [G]roup  [E]dit <n>  [R]emove <n>  "
            "[T]oggle AND/OR  [B]ack"
        )
        choice = input("Select action: ").strip().lower()
        if not choice:
            continue
        action = choice[0]
        if action == "b":
            return node
        if action == "t":
            new_key = "any" if key == "all" else "all"
            node = {new_key: children}
            continue
        if action == "a":
            children.append(make_condition())
            continue
        if action == "g":
            children.append(edit_condition_group({"all": []}))
            node[key] = children
            continue
        if not children:
            print("⚠️  No entries to operate on.")
            continue
        entry_index = choose_from_list(
            "Select condition entry", [summarise_condition(child) for child in children]
        )
        if entry_index is None:
            continue
        if action == "r":
            children.pop(entry_index)
            node[key] = children
            continue
        child = children[entry_index]
        if isinstance(child, dict) and ("all" in child or "any" in child):
            children[entry_index] = edit_condition_group(child)
        elif isinstance(child, list):
            children[entry_index] = edit_condition_group({"all": child})
        else:
            children[entry_index] = edit_simple_condition(dict(child))
        node[key] = children
        continue
            children.append({"all": []})
            continue
        if action in {"e", "r"}:
            rest = choice[1:].strip() if len(choice) > 1 else input("  Item number: ")
            try:
                index = int(rest) - 1
            except ValueError:
                print("⚠️  Please provide a valid number.")
                continue
            if index < 0 or index >= len(children):
                print("⚠️  Number out of range.")
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
                children[index] = edit_simple_condition(dict(child))
            continue


def edit_action_block(action: dict[str, Any]) -> dict[str, Any]:
    """Interactive editor for the rule's action block."""

    if not action:
        action = {"type": "move", "target": ""}
    while True:
        act_type = action.get("type", "move")
        target = action.get("target", "")
        extras = {k: v for k, v in action.items() if k not in {"type", "target"}}

        extras_summary = ", ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in extras.items()
        )
        if not extras_summary:
            extras_summary = "(none)"

        choice = choose_menu_option(
            "Action editor",
            [
                ("t", f"Type   : {act_type}"),
                ("r", f"Target : {target or '<unset>'}"),
                ("x", f"Extras : {extras_summary}"),
                ("b", "Back"),
            ],
        )
        if choice is None:
            choice = "b"
        if choice == "b":
            return action
        if choice == "t":
            action["type"] = prompt("  Action type: ", allow_empty=True) or act_type
            continue
        if choice == "r":
            action["target"] = prompt("  Target folder: ", allow_empty=True)
            continue
        if choice == "x":
        print("\nAction editor")
        print(f"  Type   : {act_type}")
        print(f"  Target : {target or '<unset>'}")
        if extras:
            print("  Extras :")
            for key, value in extras.items():
                print(f"    - {key} = {json.dumps(value, ensure_ascii=False)}")
        else:
            print("  Extras : (none)")

        print("Options: [T]ype  [R]ename target  E[x]tras  [B]ack")
        choice = input("Select action: ").strip().lower()
        if not choice:
            continue
        action_key = choice[0]
        if action_key == "b":
            return action
        if action_key == "t":
            action["type"] = prompt("  Action type: ", allow_empty=True) or act_type
            continue
        if action_key == "r":
            action["target"] = prompt("  Target folder: ", allow_empty=True)
            continue
        if action_key == "x":
            extras = {k: v for k, v in action.items() if k not in {"type", "target"}}
            edit_generic_dict(extras)
            for key in list(action):
                if key not in {"type", "target"}:
                    action.pop(key)
            action.update(extras)
            continue


def edit_comments_list(items: list[str]) -> list[str]:
    """Interactive list editor for comment strings."""

    while True:
        labels = list(items)
        add_index = len(labels)
        back_index = add_index + 1
        labels.append("➕ Add comment")
        labels.append("⬅ Back")

        choice = choose_from_list(
            "Comments", labels, prompt_text="Select comment (blank to cancel): "
        )
        if choice is None or choice == back_index:
            return items
        if choice == add_index:
            items.append(prompt("  Comment: "))
            continue
        comment_index = choice
        action = choose_menu_option(
            f"Comment: {items[comment_index]}",
            [("e", "Edit"), ("r", "Remove"), ("b", "Back")],
        )
        if action is None or action == "b":
            continue
        if action == "e":
            items[comment_index] = prompt("  New comment: ")
        elif action == "r":
            items.pop(comment_index)
        print("\nComments")
        if not items:
            print("  (no comments)")
        else:
            for idx, comment in enumerate(items, start=1):
                print(f"  {idx:>3}. {comment}")

        print("Options: [A]dd  [E]dit <n>  [R]emove <n>  [B]ack")
        choice = input("Select action: ").strip().lower()
        if not choice:
            continue
        action = choice[0]
        if action == "b":
            return items
        if action == "a":
            items.append(prompt("  Comment: "))
            continue
        rest = choice[1:].strip() if len(choice) > 1 else input("  Item number: ")
        try:
            index = int(rest) - 1
        except ValueError:
            print("⚠️  Please provide a valid number.")
            continue
        if index < 0 or index >= len(items):
            print("⚠️  Number out of range.")
            continue
        if action == "e":
            items[index] = prompt("  New comment: ")
        elif action == "r":
            items.pop(index)


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
        rules = load_rules(self.rules_dir, self.logger)
        self.rules: list[RuleRecord] = []
        for raw in rules:
            file_name = raw.pop("_file", None)
            path = self.rules_dir / (file_name or f"{slugify(raw.get('name', 'rule'))}.json")
            self.rules.append(RuleRecord(path, raw))
        self.rules.sort(key=lambda rule: (rule.priority, rule.name.lower()))

    def save_rule(self, rule: RuleRecord) -> None:
        data = dict(rule.data)
        data.pop("_file", None)
        with rule.file.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    def select_rule(self) -> RuleRecord | None:
        if not self.rules:
            print("No rules are available yet.")
            return None
        rule_labels = [
            f"[{rule.priority:>4}] {rule.name} — {summarise_action(rule.data.get('action'))}"
            for rule in self.rules
        ]

        selection = choose_from_list(
            "Available rules (sorted by priority):", rule_labels
        )
        if selection is None:
            return None
        return self.rules[selection]
        try:
            choice = interactive_menu("Available rules (sorted by priority):", rule_labels)
            menu_used = True
        except RuntimeError:
            choice = None
            menu_used = False

        if choice is not None:
            return self.rules[choice]

        if menu_used:
            return None

        while True:
            print("\nAvailable rules (sorted by priority):")
            for idx, rule in enumerate(self.rules, start=1):
                print(f"  {idx:>3}. [{rule.priority:>4}] {rule.name} ({rule.file.name})")
            raw = input("Enter rule number (or blank to cancel): ").strip()
            if not raw:
                return None
            try:
                index = int(raw) - 1
            except ValueError:
                print("⚠️  Please enter a number.")
                continue
            if 0 <= index < len(self.rules):
                return self.rules[index]
            print("⚠️  Number out of range.")

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
        next_id = max(numeric_prefixes, default=int(datetime.now().strftime("%y%j%H%M"))) + 1
        filename = f"{next_id:05d}_{slug}.json"
        return self.rules_dir / filename

    # --------------------------------------------------------------- operations
    def create_rule(self) -> None:
        print("\n➕ Create a new rule")
        name = prompt("  Rule name: ")
        priority = prompt_int("  Priority [100]: ", default=100)
        filename = self.generate_filename(name)
        if confirm(f"Save rule as {filename.name}?", default=True):
            record = RuleRecord(
                file=filename,
                data={
                    "name": name,
                    "priority": priority,
                    "conditions": {"all": []},
                    "action": {"type": "move", "target": ""},
                },
            )
            self.rules.append(record)
            self.edit_rule(record, new_rule=True)
            self.rules.sort(key=lambda rule: (rule.priority, rule.name.lower()))
        else:
            print("❌ Creation cancelled.")

    def edit_rule(self, rule: RuleRecord, *, new_rule: bool = False) -> None:
        while True:
            comments = rule.data.get("comments")
            options = [
                ("n", f"Name      : {rule.data.get('name', '<unnamed>')}"),
                ("p", f"Priority  : {rule.priority}"),
                ("c", f"Conditions: {summarise_condition(rule.data.get('conditions'))}"),
                ("a", f"Action    : {summarise_action(rule.data.get('action'))}"),
            ]
            if comments:
                options.append(("o", f"Comments ({len(comments)})"))
            else:
                options.append(("o", "Comments (none)"))
            options.extend(
                [
                    ("x", "Edit extra fields"),
                    ("t", "Dry-run test"),
                    ("s", "Save & exit"),
                    ("b", "Back"),
                ]
            )

            action = choose_menu_option(
                f"🛠️  Editing rule — {rule.file.name}", options
            )
            if action is None:
                action = "b"
            print("\n🛠️  Editing rule")
            print(f"  File     : {rule.file.name}")
            print(f"  Name     : {rule.data.get('name', '<unnamed>')}")
            print(f"  Priority : {rule.priority}")
            print(f"  Action   : {summarise_action(rule.data.get('action'))}")
            print(f"  Conditions -> {summarise_condition(rule.data.get('conditions'))}")
            if comments:
                print("  Comments :")
                for line in comments:
                    print(f"    - {line}")
            else:
                print("  Comments : (none)")

            print(
                "Options: [N]ame  [P]riority  [C]onditions  [A]ction  "
                "C[o]mments  E[x]tras  [T]est  [S]ave & exit  [B]ack"
            )
            choice = input("Select action: ").strip().lower()
            if not choice:
                continue
            action = choice[0]
            if action == "b":
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
            if action == "n":
                rule.data["name"] = prompt("  Rule name: ")
                continue
            if action == "p":
                rule.data["priority"] = prompt_int("  Priority: ", default=rule.priority)
                continue
            if action == "c":
                existing = rule.data.get("conditions")
                rule.data["conditions"] = edit_condition_group(existing or {"all": []})
                continue
            if action == "a":
                rule.data["action"] = edit_action_block(dict(rule.data.get("action") or {}))
                continue
            if action == "o":
                items = list(rule.data.get("comments") or [])
                rule.data["comments"] = edit_comments_list(items)
                continue
            if action == "x":
                extras = {
                    key: value
                    for key, value in rule.data.items()
                    if key not in {"name", "priority", "conditions", "action", "comments"}
                }
                edit_generic_dict(extras)
                # Remove existing extras before applying updates.
                for key in list(rule.data):
                    if key not in {"name", "priority", "conditions", "action", "comments"}:
                        rule.data.pop(key)
                rule.data.update(extras)
                continue
            if action == "t":
                self.test_rule(rule)
                continue

    def delete_rule(self) -> None:
        rule = self.select_rule()
        if not rule:
            return
        if confirm(f"Delete {rule.file.name}?", default=False):
            if rule.file.exists():
                backup = rule.file.with_suffix(".bak")
                shutil.copy2(rule.file, backup)
                rule.file.unlink()
                print(f"🗑️  Rule deleted. Backup saved to {backup.name}.")
            else:
                print("🗑️  Rule removed (file did not exist on disk).")
            self.rules.remove(rule)

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

            selection = choose_from_list(
                "Priority manager — select a rule", labels
            )
            if selection is None or selection == back_index:
                return
            if selection == save_index:
            print("\nPriority manager")
            for idx, rule in enumerate(ordered, start=1):
                print(f"  {idx:>3}. [{rule.priority:>4}] {rule.name}")
            print(
                "Options: move [U]p <n>  move [D]own <n>  set [E]dit <n>  "
                "[S]ave  [B]ack"
            )
            choice = input("Select action: ").strip().lower()
            if not choice:
                continue
            action = choice[0]
            if action == "b":
                return
            if action == "s":
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
            if action is None or action == "b":
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
                continue
            if action == "d":
                if selection == len(ordered) - 1:
                    print("⚠️  Already at the bottom.")
                    continue
                current = ordered[selection]
                below = ordered[selection + 1]
                ordered[selection + 1], ordered[selection] = current, below
                current_priority = current.data.get("priority", current.priority)
                below_priority = below.data.get("priority", below.priority)
                current.data["priority"], below.data["priority"] = below_priority, current_priority
                continue
            if action == "e":
                rule.data["priority"] = prompt_int(
                    "  New priority: ", default=rule.priority
            rest = choice[1:].strip()
            if not rest:
                rest = input("  Rule number: ").strip()
            try:
                index = int(rest) - 1
            except ValueError:
                print("⚠️  Please provide a valid number.")
                continue
            if index < 0 or index >= len(ordered):
                print("⚠️  Number out of range.")
                continue
            if action == "u":
                if index == 0:
                    print("⚠️  Already at the top.")
                    continue
                ordered[index], ordered[index - 1] = ordered[index - 1], ordered[index]
                ordered[index].data["priority"], ordered[index - 1].data["priority"] = (
                    ordered[index - 1].data.get("priority", ordered[index - 1].priority),
                    ordered[index].data.get("priority", ordered[index].priority),
                )
                continue
            if action == "d":
                if index == len(ordered) - 1:
                    print("⚠️  Already at the bottom.")
                    continue
                ordered[index], ordered[index + 1] = ordered[index + 1], ordered[index]
                ordered[index].data["priority"], ordered[index + 1].data["priority"] = (
                    ordered[index + 1].data.get("priority", ordered[index + 1].priority),
                    ordered[index].data.get("priority", ordered[index].priority),
                )
                continue
            if action == "e":
                current = ordered[index]
                current.data["priority"] = prompt_int(
                    "  New priority: ", default=current.priority
                )
                continue

    def run(self) -> None:
        while True:
            action = choose_menu_option(
                "📬 IMAPFilter Rule Manager",
                [
                    ("l", "List rules"),
                    ("c", "Create rule"),
                    ("e", "Edit rule"),
                    ("d", "Delete rule"),
                    ("p", "Priority manager"),
                    ("r", "Reload from disk"),
                    ("q", "Quit"),
                ],
            )
            if action is None:
                action = "q"
            print("\n📬 IMAPFilter Rule Manager")
            print("  [L]ist rules  [C]reate  [E]dit  [D]elete  [P]riority manager  [R]eload  [Q]uit")
            choice = input("Select action: ").strip().lower()
            if not choice:
                continue
            action = choice[0]
            if action == "q":
                print("Goodbye!")
                return
            if action == "r":
                self.refresh_rules()
                print("🔄 Rules reloaded from disk.")
                continue
            if action == "l":
                self.refresh_rules()
                self.select_rule()  # Listing happens within select_rule
                continue
            if action == "c":
                self.refresh_rules()
                self.create_rule()
                continue
            if action == "e":
                self.refresh_rules()
                rule = self.select_rule()
                if rule:
                    self.edit_rule(rule)
                continue
            if action == "d":
                self.refresh_rules()
                self.delete_rule()
                continue
            if action == "p":
                self.refresh_rules()
                self.reorder_priorities()
                continue
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

