"""Wizard-style review tool for flagging miscategorised emails and generating rule fix prompts."""
from __future__ import annotations

import curses
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ISSUE_CATEGORIES = [
    (1, "Wrong folder", "email should go to a different folder"),
    (2, "Should not be moved", "email should stay in its current folder"),
    (3, "Rule too broad", "rule is catching unrelated emails"),
    (4, "Rule too narrow", "missing similar emails from same sender/domain"),
    (5, "Missing coverage", "no rule matches this type of email"),
    (6, "Other", "free text description"),
]


@dataclass
class ReviewEmail:
    uid: str
    source_folder: str
    from_addr: str
    subject: str
    date: str
    matching_rule_name: str
    matching_rule_file: str
    matching_rule_target: str
    raw_data: str


@dataclass
class Flag:
    email: ReviewEmail
    category: int
    target_suggestion: str
    note: str


def _load_folder_list(db_path: Path, include_folders: list[str] | None) -> list[tuple[str, int]]:
    conn = sqlite3.connect(str(db_path))
    try:
        if include_folders:
            placeholders = ",".join("?" * len(include_folders))
            rows = conn.execute(
                f"SELECT folder, COUNT(*) FROM headers WHERE folder IN ({placeholders})"
                " GROUP BY folder ORDER BY folder",
                include_folders,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT folder, COUNT(*) FROM headers GROUP BY folder ORDER BY folder"
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        conn.close()


def _load_folder_emails(
    db_path: Path,
    folder: str,
    sorted_rules: list[dict],
    limit: int,
) -> list[ReviewEmail]:
    from core.rule_engine import _extract_message_metadata, conditions_match
    from core.tools.cache_viewer import _decode_mime_header

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    emails: list[ReviewEmail] = []
    try:
        rows = conn.execute(
            "SELECT uid, folder, data FROM headers WHERE folder = ? ORDER BY uid LIMIT ?",
            (folder, limit),
        ).fetchall()
        for row in rows:
            try:
                data = row["data"]
                header_dict, flags_list, date_obj = _extract_message_metadata(data)
                from_addr = _decode_mime_header(header_dict.get("from", "")).strip()
                subject = _decode_mime_header(header_dict.get("subject", "")).strip()
                date_str = date_obj.strftime("%Y-%m-%d %H:%M") if date_obj else "Unknown"

                matched_rule: dict | None = None
                for rule in sorted_rules:
                    conds = rule.get("conditions")
                    if conds and conditions_match(header_dict, conds, flags_list, date_obj):
                        matched_rule = rule
                        break

                rule_name = matched_rule.get("name", "") if matched_rule else ""
                rule_file = matched_rule.get("_file", "") if matched_rule else ""
                rule_target = ""
                if matched_rule:
                    for action in matched_rule.get("actions", []):
                        if action.get("type") == "move":
                            rule_target = action.get("target", "")
                            break

                emails.append(ReviewEmail(
                    uid=row["uid"],
                    source_folder=folder,
                    from_addr=from_addr,
                    subject=subject,
                    date=date_str,
                    matching_rule_name=rule_name,
                    matching_rule_file=rule_file,
                    matching_rule_target=rule_target,
                    raw_data=data,
                ))
            except Exception:
                continue
    finally:
        conn.close()
    return emails


def _trunc(text: str, width: int, *, strip_display_name: bool = False) -> str:
    """Truncate and pad text to exactly width chars."""
    if strip_display_name and "<" in text and ">" in text:
        start = text.rfind("<") + 1
        end = text.rfind(">")
        if 0 < start <= end:
            text = text[start:end]
    if len(text) <= width:
        return text.ljust(width)
    return (text[: width - 3] + "...").ljust(width)


class ReviewTool:
    def __init__(
        self,
        db_path: Path,
        rules_dir: Path,
        folder_list: list[tuple[str, int]],
        sorted_rules: list[dict],
        output_path: Path,
        limit: int,
    ) -> None:
        self.db_path = db_path
        self.rules_dir = rules_dir
        self.folder_list = folder_list
        self.sorted_rules = sorted_rules
        self.output_path = output_path
        self.limit = limit
        self.flags: list[Flag] = []
        self.folder_idx = 0
        self._folder_cache: dict[str, list[ReviewEmail]] = {}

    def _get_folder_emails(self, folder_name: str) -> list[ReviewEmail]:
        if folder_name not in self._folder_cache:
            self._folder_cache[folder_name] = _load_folder_emails(
                self.db_path, folder_name, self.sorted_rules, self.limit
            )
        return self._folder_cache[folder_name]

    def _flagged_count(self, folder_name: str) -> int:
        return sum(1 for f in self.flags if f.email.source_folder == folder_name)

    def run(self, stdscr: Any) -> list[Flag]:
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except Exception:
            pass
        stdscr.keypad(True)

        while self.folder_idx < len(self.folder_list):
            action = self._screen_folder(stdscr)
            if action == "quit":
                break
            elif action == "browse":
                self._screen_emails(stdscr, self.folder_list[self.folder_idx][0])
                self.folder_idx += 1
            elif action == "ok":
                self.folder_idx += 1
            elif action == "skip":
                self.folder_idx += 1
            elif action == "prev":
                self.folder_idx = max(0, self.folder_idx - 1)

        if self.folder_idx >= len(self.folder_list):
            self._screen_complete(stdscr)

        return self.flags

    # ── Screen A: Folder wizard ────────────────────────────────────────────

    def _screen_folder(self, stdscr: Any) -> str:
        folder_name, email_count = self.folder_list[self.folder_idx]
        total = len(self.folder_list)

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            stdscr.addnstr(0, 0, f"IMAPFilter Review  —  Folder {self.folder_idx + 1} of {total}", width - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, "=" * min(width - 1, 72), width - 1)

            flagged = self._flagged_count(folder_name)
            count_info = f"{email_count:,} emails"
            if flagged:
                count_info += f"  |  {flagged} flagged"

            stdscr.addnstr(3, 0, f"  {folder_name}", min(width - 1, len(folder_name) + 2), curses.A_BOLD)
            stdscr.addnstr(4, 0, f"  {count_info}", width - 1)

            stdscr.addnstr(6, 0, "  [ENTER]  Browse emails in this folder", width - 1)
            stdscr.addnstr(7, 0, "  [a]      Mark folder OK — advance to next", width - 1)
            stdscr.addnstr(8, 0, "  [s]      Skip (decide later)", width - 1)
            stdscr.addnstr(9, 0, "  [p]      Go back to previous folder", width - 1)
            stdscr.addnstr(10, 0, "  [q]      Quit and save", width - 1)

            progress_row = min(height - 3, 13)
            if total > 0:
                filled = int((self.folder_idx / total) * 30)
                bar = "█" * filled + "░" * (30 - filled)
                progress = f"  [{bar}]  {self.folder_idx}/{total}  |  {len(self.flags)} flagged"
                stdscr.addnstr(progress_row, 0, progress, width - 1, curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (curses.KEY_ENTER, 10, 13):
                return "browse"
            elif key in (ord("a"), ord("A")):
                return "ok"
            elif key in (ord("s"), ord("S")):
                return "skip"
            elif key in (ord("p"), ord("P")):
                return "prev"
            elif key in (ord("q"), ord("Q"), 27):
                return "quit"

    # ── Screen B: Email list ───────────────────────────────────────────────

    def _screen_emails(self, stdscr: Any, folder_name: str) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f"Loading {folder_name}...", width - 1, curses.A_DIM)
        stdscr.refresh()

        emails = self._get_folder_emails(folder_name)

        scroll_offset = 0
        cursor = 0
        selected: set[int] = set()

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            HEADER_ROWS = 4
            FOOTER_ROWS = 2
            list_height = max(1, height - HEADER_ROWS - FOOTER_ROWS)

            stdscr.addnstr(0, 0, f"  {folder_name}  ({len(emails):,} emails)", width - 1, curses.A_BOLD)

            # Subtitle: most-matched rule
            rule_counts: dict[str, tuple[int, str]] = {}
            for em in emails:
                if em.matching_rule_name:
                    prev_count = rule_counts.get(em.matching_rule_name, (0, em.matching_rule_target))[0]
                    rule_counts[em.matching_rule_name] = (prev_count + 1, em.matching_rule_target)
            if rule_counts:
                top_rule = max(rule_counts, key=lambda k: rule_counts[k][0])
                top_target = rule_counts[top_rule][1]
                subtitle = f"  Rule: {top_rule}"
                if top_target:
                    subtitle += f"  →  {top_target}"
                stdscr.addnstr(1, 0, subtitle[:width - 1], width - 1, curses.A_DIM)

            stdscr.addnstr(2, 0, "=" * min(width - 1, 72), width - 1)

            date_w = 16
            from_w = max(20, (width - date_w - 6) // 3)
            subj_w = max(20, width - date_w - from_w - 6)
            col_header = f"   {'Date':<{date_w}} {'From':<{from_w}} {'Subject'}"
            stdscr.addnstr(3, 0, col_header[:width - 1], width - 1, curses.A_UNDERLINE)

            # Clamp scroll
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + list_height:
                scroll_offset = cursor - list_height + 1

            flagged_uids = {(f.email.uid, f.email.source_folder) for f in self.flags}

            for offset in range(list_height):
                idx = scroll_offset + offset
                if idx >= len(emails):
                    break
                em = emails[idx]

                is_flagged = (em.uid, em.source_folder) in flagged_uids
                flag_marker = "*" if is_flagged else " "
                sel_marker = ">" if idx in selected else " "

                from_text = _trunc(em.from_addr, from_w, strip_display_name=True)
                subj_text = _trunc(em.subject, subj_w)
                date_text = em.date[:date_w].ljust(date_w)

                line = f"{flag_marker}{sel_marker} {date_text} {from_text} {subj_text}"

                attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
                if is_flagged:
                    attr |= curses.A_DIM

                stdscr.addnstr(HEADER_ROWS + offset, 0, line[:width - 1], width - 1, attr)

            # Footer
            if emails:
                em = emails[cursor]
                no_rule = "" if em.matching_rule_name else "  [no rule match]"
                status = f"  {cursor + 1}/{len(emails)}{no_rule}"
            else:
                status = "  (no emails in this folder)"
            stdscr.addnstr(height - 2, 0, status[:width - 1], width - 1, curses.A_DIM)
            help_text = "  ↑/↓  f flag  SPACE select  a flag-all  d done  ESC back"
            stdscr.addnstr(height - 1, 0, help_text[:width - 1], width - 1, curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = min(max(0, len(emails) - 1), cursor + 1)
            elif key == curses.KEY_PPAGE:
                cursor = max(0, cursor - list_height)
            elif key == curses.KEY_NPAGE:
                cursor = min(max(0, len(emails) - 1), cursor + list_height)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = max(0, len(emails) - 1)
            elif key == ord(" "):
                if cursor in selected:
                    selected.discard(cursor)
                else:
                    selected.add(cursor)
            elif key in (ord("f"), ord("F")) and emails:
                targets = sorted(selected) if selected else [cursor]
                for idx in targets:
                    flag = self._dialog_flag(stdscr, emails[idx])
                    if flag is not None:
                        self._replace_flag(flag)
                selected.clear()
            elif key in (ord("a"), ord("A")) and emails:
                bulk_flag = self._dialog_flag(stdscr, emails[cursor], bulk_count=len(emails))
                if bulk_flag is not None:
                    for em in emails:
                        self._replace_flag(Flag(
                            email=em,
                            category=bulk_flag.category,
                            target_suggestion=bulk_flag.target_suggestion,
                            note=bulk_flag.note,
                        ))
            elif key in (ord("d"), ord("D"), 27, ord("q"), ord("Q")):
                break

    def _replace_flag(self, flag: Flag) -> None:
        self.flags = [
            f for f in self.flags
            if not (f.email.uid == flag.email.uid and f.email.source_folder == flag.email.source_folder)
        ]
        self.flags.append(flag)

    # ── Screen C: Flag dialog (inline modal) ──────────────────────────────

    def _dialog_flag(self, stdscr: Any, email: ReviewEmail, *, bulk_count: int = 0) -> Flag | None:
        height, width = stdscr.getmaxyx()
        dialog_h = 17
        dialog_w = min(width - 2, 78)
        dialog_y = max(0, height - dialog_h - 1)
        dialog_x = 0

        try:
            win = curses.newwin(dialog_h, dialog_w, dialog_y, dialog_x)
            win.keypad(True)
        except Exception:
            return None

        win.erase()
        win.border()
        title = " Flag Issue " if not bulk_count else f" Flag All {bulk_count} Emails "
        win.addnstr(0, 2, title, dialog_w - 4, curses.A_BOLD)

        inner_w = dialog_w - 4
        win.addnstr(1, 2, f"From:    {_trunc(email.from_addr, inner_w - 9, strip_display_name=True)}", dialog_w - 3)
        win.addnstr(2, 2, f"Subject: {_trunc(email.subject, inner_w - 9)}", dialog_w - 3)
        win.addnstr(3, 2, f"Folder:  {_trunc(email.source_folder, inner_w - 9)}", dialog_w - 3)
        if email.matching_rule_name:
            rule_ctx = f"Rule:    {_trunc(email.matching_rule_name, inner_w - 9)}"
            if email.matching_rule_target:
                rule_ctx = rule_ctx.rstrip() + f"  →  {email.matching_rule_target}"
            win.addnstr(4, 2, rule_ctx[:dialog_w - 3], dialog_w - 3)

        win.addnstr(5, 2, "-" * (dialog_w - 4), dialog_w - 3)
        for i, (num, label, desc) in enumerate(ISSUE_CATEGORIES):
            line = f"  {num}  {label} — {desc}"
            win.addnstr(6 + i, 2, line[:dialog_w - 3], dialog_w - 3)
        win.addnstr(12, 2, "-" * (dialog_w - 4), dialog_w - 3)
        prompt = "  Category (1-6, ESC cancel): "
        win.addnstr(13, 2, prompt, dialog_w - 3)
        win.refresh()

        curses.curs_set(1)
        category = 0
        try:
            while True:
                key = win.getch()
                if key == 27:
                    return None
                if ord("1") <= key <= ord("6"):
                    category = key - ord("0")
                    break
        finally:
            curses.curs_set(0)

        target_suggestion = ""
        note = ""
        input_x = len(prompt) + 2  # column after the prompt text

        if category == 1:
            folder_prompt = "  Correct folder: "
            win.addnstr(13, 2, folder_prompt + " " * (dialog_w - len(folder_prompt) - 3), dialog_w - 3)
            win.addnstr(13, 2, folder_prompt, dialog_w - 3)
            win.refresh()
            curses.curs_set(1)
            curses.echo()
            try:
                raw = win.getstr(13, len(folder_prompt) + 2, dialog_w - len(folder_prompt) - 6)
                target_suggestion = raw.decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            finally:
                curses.noecho()
                curses.curs_set(0)

        note_prompt = "  Note (Enter to skip): "
        win.addnstr(14, 2, note_prompt + " " * (dialog_w - len(note_prompt) - 3), dialog_w - 3)
        win.addnstr(14, 2, note_prompt, dialog_w - 3)
        win.refresh()
        curses.curs_set(1)
        curses.echo()
        try:
            raw = win.getstr(14, len(note_prompt) + 2, dialog_w - len(note_prompt) - 6)
            note = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        finally:
            curses.noecho()
            curses.curs_set(0)

        win.addnstr(15, 2, "  Flagged. Press any key...", dialog_w - 3, curses.A_DIM)
        win.refresh()
        win.getch()

        return Flag(email=email, category=category, target_suggestion=target_suggestion, note=note)

    # ── Completion screen ──────────────────────────────────────────────────

    def _screen_complete(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, "Review Complete", width - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "=" * min(width - 1, 72), width - 1)
        stdscr.addnstr(3, 0, f"  Folders reviewed: {len(self.folder_list):,}", width - 1)
        stdscr.addnstr(4, 0, f"  Emails flagged:   {len(self.flags):,}", width - 1)
        if self.flags:
            stdscr.addnstr(6, 0, f"  Output: {self.output_path}", width - 1)
        stdscr.addnstr(8, 0, "  Press any key to exit...", width - 1, curses.A_DIM)
        stdscr.refresh()
        stdscr.getch()


# ── Markdown generation ────────────────────────────────────────────────────


def generate_markdown(flags: list[Flag], rules_dir: Path, output_path: Path) -> None:
    rule_json_cache: dict[str, str] = {}
    for flag in flags:
        rname = flag.email.matching_rule_name
        rfile = flag.email.matching_rule_file
        if rname and rfile and rname not in rule_json_cache:
            rule_path = rules_dir / rfile
            if rule_path.exists():
                try:
                    raw = json.loads(rule_path.read_text(encoding="utf-8"))
                    rule_json_cache[rname] = json.dumps(raw, indent=2, ensure_ascii=False)
                except Exception:
                    pass

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    lines += [
        f"# IMAPFilter Rule Review — {ts}",
        "",
        "You are a rule editor for an IMAP email filtering system. "
        "The user has flagged emails that are incorrectly handled. "
        "Review each issue below and create or modify rule files in the `rules/` directory.",
        "",
        "## Rule Format Reference",
        "",
        "- **File naming:** `rules/{priority}_{folder_underscored}_{description}.json`",
        "- **Priority:** integer, lower = higher precedence (first-match-wins evaluation)",
        "- **Conditions:** nested `all`/`any` with `header` + operator "
        "(`contains`, `equals`, `regex`, `not_contains`, `not_equals`, `age_days_gt`, `has_keyword`, etc.)",
        '- **Actions:** array of `{"type": "move", "target": "Folder/Path"}` '
        'and/or `{"type": "set_keywords", "keywords": ["tag"]}`',
        "",
        f"## Flagged Emails ({len(flags)} issues)",
        "",
    ]

    for i, flag in enumerate(flags, 1):
        em = flag.email
        cat_label = next((label for num, label, _ in ISSUE_CATEGORIES if num == flag.category), "Other")

        lines += ["---", "", f"### Issue {i} of {len(flags)} — {cat_label}", ""]

        if flag.category == 1 and flag.target_suggestion:
            lines += [f"**User says:** Should go to `{flag.target_suggestion}`", ""]

        lines += [
            "| Field | Value |",
            "|---|---|",
            f"| From | {em.from_addr} |",
            f"| Subject | {em.subject} |",
            f"| Date | {em.date} |",
            f"| Current folder | `{em.source_folder}` |",
        ]
        if em.matching_rule_target:
            lines.append(f"| Rule target | `{em.matching_rule_target}` |")
        if em.matching_rule_name:
            lines.append(f"| Matched rule | {em.matching_rule_name} |")
        else:
            lines.append("| Matched rule | *(no rule matched)* |")
        lines.append("")

        if em.matching_rule_name and em.matching_rule_name in rule_json_cache:
            lines += [
                f"**Matched rule file:** `rules/{em.matching_rule_file}`",
                "```json",
                rule_json_cache[em.matching_rule_name],
                "```",
                "",
            ]

        if flag.note:
            lines += [f"**User note:** {flag.note}", ""]

    lines += [
        "---",
        "",
        "## Instructions for Claude Code",
        "",
        "Review each flagged email above and create or modify rules in `rules/` to address the issues.",
        "",
        "For each issue category:",
        "",
        "1. **Wrong folder** — Add a higher-priority rule targeting the correct folder, or narrow "
        "the existing matched rule with `not_contains`/`not_equals` exclusion conditions.",
        "2. **Should not be moved** — Add an exclusion condition to the matched rule, or add a "
        "higher-priority rule that targets the same source folder.",
        "3. **Rule too broad** — Add `not_contains`/`not_equals` conditions to the matched rule "
        "to exclude the flagged email's sender or subject pattern.",
        "4. **Rule too narrow** — Broaden the matched rule's conditions (e.g. match the full "
        "domain `@domain.com` rather than a single address), or add an `any` branch.",
        "5. **Missing coverage** — Create a new rule for the unmatched email. Choose a priority "
        "that slots it correctly relative to existing rules.",
        "6. **Other** — Apply the user note as guidance.",
        "",
        "Preserve existing rule priorities. When creating new rules, choose a priority value "
        "that positions them correctly relative to existing rules.",
        "",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ── Entry point ────────────────────────────────────────────────────────────


def launch_review_tool(
    cfg: Any,
    output_path: Path,
    *,
    folders: list[str] | None,
    limit: int,
) -> int:
    from core.logging_utils import JsonLogger
    from core.rule_engine import load_rules

    db_path = cfg.paths.db_file
    rules_dir = cfg.paths.rules_dir

    print("Loading folders from cache...")
    folder_list = _load_folder_list(db_path, folders)
    if not folder_list:
        print("No emails found in cache. Run build-cache first.")
        return 1

    print(f"Found {len(folder_list):,} folders.")
    print("Loading rules...")
    logger = JsonLogger(log_file=cfg.paths.log_file)
    sorted_rules = sorted(
        load_rules(rules_dir, logger),
        key=lambda r: int(r.get("priority", 100)),
    )
    print(f"Loaded {len(sorted_rules):,} rules. Starting review...")

    tool = ReviewTool(
        db_path=db_path,
        rules_dir=rules_dir,
        folder_list=folder_list,
        sorted_rules=sorted_rules,
        output_path=output_path,
        limit=limit,
    )

    flags: list[Flag] = []
    try:
        flags = curses.wrapper(tool.run)
    except KeyboardInterrupt:
        flags = tool.flags

    if flags:
        generate_markdown(flags, rules_dir, output_path)
        print(f"\nReview saved: {output_path}  ({len(flags)} issues flagged)")
    else:
        print("\nNo emails flagged — no output file written.")

    return 0
