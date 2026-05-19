"""Folder classification review — hierarchical folder tree browser with per-level flagging."""
from __future__ import annotations

import curses
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class FolderFlag:
    folder: str                                    # full path (may be intermediate or leaf)
    email_count: int                               # direct emails (leaf) or subtree total
    sample_emails: list[tuple[str, str, str]]      # (date, from_addr, subject)
    comment: str


def load_all_folders(db_path: Path, include_sections: list[str] | None = None) -> dict[str, int]:
    """
    Return {folder_path: email_count} for every known IMAP folder.

    Folder names come from the ``folders`` table (full set discovered from the server).
    Email counts come from ``headers`` (only populated for cached folders); uncached
    folders get a count of 0.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        folder_rows = conn.execute("SELECT name FROM folders ORDER BY name").fetchall()
        count_rows = conn.execute(
            "SELECT folder, COUNT(*) FROM headers GROUP BY folder"
        ).fetchall()
    finally:
        conn.close()

    counts: dict[str, int] = {folder: cnt for folder, cnt in count_rows}
    result: dict[str, int] = {}
    for (name,) in folder_rows:
        if include_sections and name.split("/")[0] not in include_sections:
            continue
        result[name] = counts.get(name, 0)
    return result


def get_children(
    all_folders: dict[str, int], prefix: str
) -> list[tuple[str, str, bool, int]]:
    """
    Return immediate children of *prefix* as ``(component, full_path, is_sub, email_count)``.

    ``is_sub`` is True when the child has its own sub-folders (ENTER drills in).
    ``email_count`` is the direct count for leaf nodes and the subtree total for sub-nodes.
    """
    has_sub: dict[str, bool] = {}
    sub_totals: dict[str, int] = {}
    direct: dict[str, int] = {}

    for folder, count in all_folders.items():
        if prefix:
            if not folder.startswith(prefix + "/"):
                continue
            rest = folder[len(prefix) + 1:]
        else:
            rest = folder

        slash_idx = rest.find("/")
        if slash_idx == -1:
            component = rest
            direct[component] = direct.get(component, 0) + count
        else:
            component = rest[:slash_idx]
            has_sub[component] = True
            sub_totals[component] = sub_totals.get(component, 0) + count

    result = []
    for comp in sorted(set(has_sub) | set(direct)):
        full_path = f"{prefix}/{comp}" if prefix else comp
        is_sub = comp in has_sub
        if is_sub:
            total = sub_totals.get(comp, 0) + direct.get(comp, 0)
            result.append((comp, full_path, True, total))
        else:
            result.append((comp, full_path, False, direct[comp]))
    return result


def load_folder_emails(db_path: Path, folder: str, limit: int = 200) -> list[tuple[str, str, str]]:
    from core.rule_engine import _extract_message_metadata
    from core.tools.cache_viewer import _decode_mime_header

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    emails: list[tuple[str, str, str]] = []
    try:
        rows = conn.execute(
            "SELECT data FROM headers WHERE folder = ? ORDER BY uid DESC LIMIT ?",
            (folder, limit),
        ).fetchall()
        for row in rows:
            try:
                header_dict, _, date_obj = _extract_message_metadata(row["data"])
                from_addr = _decode_mime_header(header_dict.get("from", "")).strip()
                subject = _decode_mime_header(header_dict.get("subject", "")).strip()
                date_str = date_obj.strftime("%Y-%m-%d %H:%M") if date_obj else "Unknown"
                emails.append((date_str, from_addr, subject))
            except Exception:
                continue
    finally:
        conn.close()
    return emails


def _trunc(text: str, width: int, *, strip_display_name: bool = False) -> str:
    if strip_display_name and "<" in text and ">" in text:
        start = text.rfind("<") + 1
        end = text.rfind(">")
        if 0 < start <= end:
            text = text[start:end]
    if len(text) <= width:
        return text.ljust(width)
    return (text[: width - 3] + "...").ljust(width)


class FolderClassifierTool:
    def __init__(
        self,
        all_folders: dict[str, int],
        db_path: Path,
        output_path: Path,
        email_limit: int,
    ) -> None:
        self.all_folders = all_folders
        self.db_path = db_path
        self.output_path = output_path
        self.email_limit = email_limit
        self.flags: list[FolderFlag] = []
        self._email_cache: dict[str, list[tuple[str, str, str]]] = {}

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_folder_emails(self, folder: str) -> list[tuple[str, str, str]]:
        if folder not in self._email_cache:
            self._email_cache[folder] = load_folder_emails(self.db_path, folder, self.email_limit)
        return self._email_cache[folder]

    def _flagged_for(self, path: str) -> FolderFlag | None:
        for f in self.flags:
            if f.folder == path:
                return f
        return None

    def _has_flag_in_subtree(self, path: str) -> bool:
        prefix = path + "/"
        return any(f.folder == path or f.folder.startswith(prefix) for f in self.flags)

    def _subtree_email_count(self, path: str) -> int:
        prefix = path + "/"
        return sum(
            c for folder, c in self.all_folders.items()
            if folder == path or folder.startswith(prefix)
        )

    def _sample_emails_for(self, path: str) -> list[tuple[str, str, str]]:
        """Return sample emails: from path directly if it's a leaf, else from first child leaf."""
        if path in self.all_folders:
            return self._get_folder_emails(path)[:5]
        prefix = path + "/"
        for folder in sorted(self.all_folders):
            if folder.startswith(prefix):
                samples = self._get_folder_emails(folder)
                if samples:
                    return samples[:5]
        return []

    def _set_flag(self, flag: FolderFlag) -> None:
        self.flags = [f for f in self.flags if f.folder != flag.folder]
        self.flags.append(flag)

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self, stdscr: Any) -> list[FolderFlag]:
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except Exception:
            pass
        stdscr.keypad(True)

        path_stack: list[str] = []
        cursor_stack: list[int] = [0]

        while True:
            current_path = "/".join(path_stack)
            action, component, cursor = self._screen_folder_contents(
                stdscr, current_path, cursor_stack[-1]
            )
            cursor_stack[-1] = cursor

            if action == "quit":
                break
            elif action == "back":
                if path_stack:
                    path_stack.pop()
                    cursor_stack.pop()
                else:
                    break
            elif action == "enter_sub":
                path_stack.append(component)
                cursor_stack.append(0)
            elif action == "enter_leaf":
                full_path = f"{current_path}/{component}" if current_path else component
                result = self._screen_email_list(stdscr, full_path)
                if result == "quit":
                    break

        self._screen_complete(stdscr)
        return self.flags

    # ── Screen: folder tree at any level ──────────────────────────────────

    def _screen_folder_contents(
        self, stdscr: Any, current_path: str, start_cursor: int
    ) -> tuple[str, str, int]:
        """
        Show immediate children of *current_path*.
        Returns ``(action, component, cursor)`` where action is one of:
        ``"enter_sub"`` | ``"enter_leaf"`` | ``"back"`` | ``"quit"``.
        """
        children = get_children(self.all_folders, current_path)
        if not children:
            return "back", "", 0

        cursor = min(start_cursor, len(children) - 1)
        scroll_offset = 0
        total_emails = sum(self.all_folders.values()) if not current_path else self._subtree_email_count(current_path)

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            breadcrumb = current_path + "/" if current_path else "(root)"
            title = f"Folder Review: {breadcrumb}"
            stdscr.addnstr(0, 0, title[:width - 1], width - 1, curses.A_BOLD)

            n_items = len(children)
            subtitle = f"  {n_items} item{'s' if n_items != 1 else ''}  —  {total_emails:,} emails  —  {len(self.flags)} flagged"
            stdscr.addnstr(1, 0, subtitle[:width - 1], width - 1, curses.A_DIM)
            stdscr.addnstr(2, 0, "=" * min(width - 1, 72), width - 1)

            HEADER_ROWS = 4
            FOOTER_ROWS = 2
            list_height = max(1, height - HEADER_ROWS - FOOTER_ROWS)

            name_w = max(24, width - 16)
            col_header = f"  {'Name':<{name_w}}  {'Emails':>8}"
            stdscr.addnstr(3, 0, col_header[:width - 1], width - 1, curses.A_UNDERLINE)

            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + list_height:
                scroll_offset = cursor - list_height + 1

            for offset in range(list_height):
                idx = scroll_offset + offset
                if idx >= len(children):
                    break
                comp, full_path, is_sub, email_count = children[idx]
                has_flag = self._has_flag_in_subtree(full_path)
                flag_marker = "* " if has_flag else "  "
                display_name = comp + "/" if is_sub else comp
                line = f"{flag_marker}{_trunc(display_name, name_w)}  {email_count:>8,}"
                attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
                if has_flag:
                    attr |= curses.A_DIM
                stdscr.addnstr(HEADER_ROWS + offset, 0, line[:width - 1], width - 1, attr)

            comp_cur, full_path_cur, is_sub_cur, _ = children[cursor]
            if is_sub_cur:
                enter_hint = "ENTER=drill in"
            else:
                enter_hint = "ENTER=browse emails"
            help_text = f"  ↑/↓  {enter_hint}  f=flag  p=back  q=quit"
            stdscr.addnstr(height - 2, 0, help_text[:width - 1], width - 1, curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (curses.KEY_UP, ord("k")):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                cursor = min(len(children) - 1, cursor + 1)
            elif key == curses.KEY_PPAGE:
                cursor = max(0, cursor - list_height)
            elif key == curses.KEY_NPAGE:
                cursor = min(len(children) - 1, cursor + list_height)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = max(0, len(children) - 1)
            elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                comp, full_path, is_sub, _ = children[cursor]
                action = "enter_sub" if is_sub else "enter_leaf"
                return action, comp, cursor
            elif key in (ord("f"), ord("F")):
                comp, full_path, is_sub, email_count = children[cursor]
                if is_sub:
                    email_count = self._subtree_email_count(full_path)
                samples = self._sample_emails_for(full_path)
                flag = self._dialog_flag(stdscr, full_path, email_count, samples)
                if flag is not None:
                    self._set_flag(flag)
            elif key in (ord("p"), ord("P"), 27, curses.KEY_LEFT):
                return "back", "", cursor
            elif key in (ord("q"), ord("Q")):
                return "quit", "", cursor

    # ── Screen: email list for a leaf folder ──────────────────────────────

    def _screen_email_list(self, stdscr: Any, folder: str) -> str:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f"Loading {folder}...", width - 1, curses.A_DIM)
        stdscr.refresh()

        emails = self._get_folder_emails(folder)
        cursor = 0
        scroll_offset = 0

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            flag = self._flagged_for(folder)
            flag_indicator = "  [FLAGGED]" if flag else ""
            title = f"  {folder}  ({len(emails):,} emails){flag_indicator}"
            stdscr.addnstr(0, 0, title[:width - 1], width - 1, curses.A_BOLD)
            if flag and flag.comment:
                stdscr.addnstr(1, 0, f"  Comment: {flag.comment}"[:width - 1], width - 1, curses.A_DIM)
            stdscr.addnstr(2, 0, "=" * min(width - 1, 72), width - 1)

            HEADER_ROWS = 4
            FOOTER_ROWS = 2
            list_height = max(1, height - HEADER_ROWS - FOOTER_ROWS)

            date_w = 16
            from_w = max(20, (width - date_w - 6) // 3)
            subj_w = max(20, width - date_w - from_w - 6)
            col_header = f"   {'Date':<{date_w}} {'From':<{from_w}} {'Subject'}"
            stdscr.addnstr(3, 0, col_header[:width - 1], width - 1, curses.A_UNDERLINE)

            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + list_height:
                scroll_offset = cursor - list_height + 1

            for offset in range(list_height):
                idx = scroll_offset + offset
                if idx >= len(emails):
                    break
                date_str, from_addr, subject = emails[idx]
                from_text = _trunc(from_addr, from_w, strip_display_name=True)
                subj_text = _trunc(subject, subj_w)
                line = f"   {date_str[:date_w].ljust(date_w)} {from_text} {subj_text}"
                attr = curses.A_REVERSE if idx == cursor else curses.A_NORMAL
                stdscr.addnstr(HEADER_ROWS + offset, 0, line[:width - 1], width - 1, attr)

            status = f"  {cursor + 1}/{len(emails)}" if emails else "  (no emails)"
            stdscr.addnstr(height - 2, 0, f"{status}  ↑/↓  f=flag folder  p=back  q=quit"[:width - 1], width - 1, curses.A_DIM)

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
            elif key in (ord("f"), ord("F")):
                count = self.all_folders.get(folder, 0)
                flag = self._dialog_flag(stdscr, folder, count, emails[:5])
                if flag is not None:
                    self._set_flag(flag)
            elif key in (ord("p"), ord("P"), 27, curses.KEY_LEFT):
                return "back"
            elif key in (ord("q"), ord("Q")):
                return "quit"

    # ── Flag dialog ────────────────────────────────────────────────────────

    @staticmethod
    def _read_text(win: Any, y: int, x: int, display_w: int) -> str:
        chars: list[str] = []
        view_start = 0
        curses.curs_set(1)
        try:
            while True:
                visible = "".join(chars)[view_start:view_start + display_w]
                win.addnstr(y, x, visible.ljust(display_w), display_w)
                cur_col = min(len(chars) - view_start, display_w - 1)
                try:
                    win.move(y, x + cur_col)
                except Exception:
                    pass
                win.refresh()
                key = win.getch()
                if key in (curses.KEY_ENTER, 10, 13):
                    break
                elif key == 27:
                    break
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    if chars:
                        chars.pop()
                        if view_start > 0 and len(chars) - view_start < display_w // 2:
                            view_start = max(0, len(chars) - display_w // 2)
                elif 32 <= key <= 126:
                    chars.append(chr(key))
                    if len(chars) - view_start >= display_w:
                        view_start = len(chars) - display_w + 1
        finally:
            curses.curs_set(0)
        return "".join(chars)

    def _dialog_flag(
        self,
        stdscr: Any,
        folder: str,
        email_count: int,
        samples: list[tuple[str, str, str]],
    ) -> FolderFlag | None:
        height, width = stdscr.getmaxyx()
        n_samples = min(3, len(samples))
        dialog_h = 10 + n_samples
        dialog_w = min(width - 2, 78)
        dialog_y = max(0, height - dialog_h - 1)

        try:
            win = curses.newwin(dialog_h, dialog_w, dialog_y, 0)
            win.keypad(True)
        except Exception:
            return None

        win.erase()
        win.border()
        inner_w = dialog_w - 4

        existing = self._flagged_for(folder)
        title = " Update Flag " if existing else " Flag Folder "
        win.addnstr(0, 2, title, dialog_w - 4, curses.A_BOLD)
        win.addnstr(1, 2, f"Folder: {_trunc(folder, inner_w - 8)}", dialog_w - 3)

        for i, (date_str, from_addr, subject) in enumerate(samples[:3]):
            from_short = _trunc(from_addr, 25, strip_display_name=True).rstrip()
            subj_short = _trunc(subject, inner_w - 28).rstrip()
            win.addnstr(2 + i, 2, f"  {from_short}  —  {subj_short}"[:dialog_w - 3], dialog_w - 3, curses.A_DIM)

        sep_row = 2 + n_samples
        win.addnstr(sep_row, 2, "-" * (dialog_w - 4), dialog_w - 3)
        win.addnstr(sep_row + 1, 2, "  Comment:", dialog_w - 3)

        if existing:
            win.addnstr(sep_row + 2, 2, f"  (current: {existing.comment})"[:dialog_w - 3], dialog_w - 3, curses.A_DIM)

        input_label = "  > "
        input_row = sep_row + 3
        win.addnstr(input_row, 2, input_label, dialog_w - 3)
        win.refresh()

        comment = self._read_text(win, input_row, 2 + len(input_label), dialog_w - 2 - len(input_label) - 2)
        comment = comment.strip()
        if not comment:
            return None

        win.addnstr(input_row + 1, 2, "  Flagged. Press any key...", dialog_w - 3, curses.A_DIM)
        win.refresh()
        win.getch()

        return FolderFlag(
            folder=folder,
            email_count=email_count,
            sample_emails=samples[:5],
            comment=comment,
        )

    # ── Systematic wizard ──────────────────────────────────────────────────

    def run_wizard(self, stdscr: Any) -> list[FolderFlag]:
        """Systematic review: step through every leaf folder one by one."""
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except Exception:
            pass
        stdscr.keypad(True)

        # Only walk folders that have cached emails; tree browser shows all.
        leaves = sorted(f for f, c in self.all_folders.items() if c > 0)
        if not leaves:
            return self.flags

        idx = 0
        while 0 <= idx < len(leaves):
            action = self._screen_wizard_folder(stdscr, leaves[idx], idx, len(leaves))
            if action == "ok":
                idx += 1
            elif action == "skip":
                idx += 1
            elif action == "flag":
                idx += 1
            elif action == "prev":
                idx = max(0, idx - 1)
            elif action == "quit":
                break

        if idx >= len(leaves):
            self._screen_complete(stdscr)

        return self.flags

    def _screen_wizard_folder(
        self, stdscr: Any, folder: str, idx: int, total: int
    ) -> str:
        """
        Display one leaf folder with its emails.
        Returns: ``"ok"`` | ``"flag"`` | ``"skip"`` | ``"prev"`` | ``"quit"``.
        """
        emails = self._get_folder_emails(folder)
        email_cursor = 0
        email_scroll = 0

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            flag = self._flagged_for(folder)
            flag_indicator = "  [FLAGGED]" if flag else ""
            title = f"IMAPFilter Folder Review  —  {idx + 1} of {total}{flag_indicator}"
            stdscr.addnstr(0, 0, title[:width - 1], width - 1, curses.A_BOLD)

            folder_line = f"  {folder}  ({self.all_folders.get(folder, 0):,} emails)"
            stdscr.addnstr(1, 0, folder_line[:width - 1], width - 1)

            filled = int((idx / total) * 30) if total > 0 else 0
            bar = "█" * filled + "░" * (30 - filled)
            stdscr.addnstr(2, 0, f"  [{bar}]  {idx}/{total}  |  {len(self.flags)} flagged"[:width - 1], width - 1, curses.A_DIM)

            if flag and flag.comment:
                stdscr.addnstr(3, 0, f"  Comment: {flag.comment}"[:width - 1], width - 1, curses.A_DIM)
                stdscr.addnstr(4, 0, "=" * min(width - 1, 72), width - 1)
                HEADER_ROWS = 6
            else:
                stdscr.addnstr(3, 0, "=" * min(width - 1, 72), width - 1)
                HEADER_ROWS = 5

            FOOTER_ROWS = 2
            list_height = max(1, height - HEADER_ROWS - FOOTER_ROWS)

            date_w = 16
            from_w = max(20, (width - date_w - 6) // 3)
            subj_w = max(20, width - date_w - from_w - 6)
            col_header = f"   {'Date':<{date_w}} {'From':<{from_w}} {'Subject'}"
            stdscr.addnstr(HEADER_ROWS - 1, 0, col_header[:width - 1], width - 1, curses.A_UNDERLINE)

            if email_cursor < email_scroll:
                email_scroll = email_cursor
            elif email_cursor >= email_scroll + list_height:
                email_scroll = email_cursor - list_height + 1

            for offset in range(list_height):
                row_idx = email_scroll + offset
                if row_idx >= len(emails):
                    break
                date_str, from_addr, subject = emails[row_idx]
                from_text = _trunc(from_addr, from_w, strip_display_name=True)
                subj_text = _trunc(subject, subj_w)
                line = f"   {date_str[:date_w].ljust(date_w)} {from_text} {subj_text}"
                attr = curses.A_REVERSE if row_idx == email_cursor else curses.A_NORMAL
                stdscr.addnstr(HEADER_ROWS + offset, 0, line[:width - 1], width - 1, attr)

            if not emails:
                stdscr.addnstr(HEADER_ROWS, 0, "  (no emails in this folder)", width - 1, curses.A_DIM)

            pos = f"{email_cursor + 1}/{len(emails)}" if emails else "0"
            help_line = f"  {pos}  ↑/↓=scroll  a/ENTER=ok  f=flag  s=skip  p=prev  q=quit"
            stdscr.addnstr(height - 2, 0, help_line[:width - 1], width - 1, curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (curses.KEY_UP, ord("k")):
                email_cursor = max(0, email_cursor - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                email_cursor = min(max(0, len(emails) - 1), email_cursor + 1)
            elif key == curses.KEY_PPAGE:
                email_cursor = max(0, email_cursor - list_height)
            elif key == curses.KEY_NPAGE:
                email_cursor = min(max(0, len(emails) - 1), email_cursor + list_height)
            elif key == curses.KEY_HOME:
                email_cursor = 0
            elif key == curses.KEY_END:
                email_cursor = max(0, len(emails) - 1)
            elif key in (curses.KEY_ENTER, 10, 13, ord("a"), ord("A")):
                return "ok"
            elif key in (ord("f"), ord("F")):
                count = self.all_folders.get(folder, 0)
                new_flag = self._dialog_flag(stdscr, folder, count, emails[:5])
                if new_flag is not None:
                    self._set_flag(new_flag)
                    return "flag"
            elif key in (ord("s"), ord("S")):
                return "skip"
            elif key in (ord("p"), ord("P")):
                return "prev"
            elif key in (ord("q"), ord("Q"), 27):
                return "quit"

    # ── Completion screen ──────────────────────────────────────────────────

    def _screen_complete(self, stdscr: Any) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, "Folder Review Complete", width - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "=" * min(width - 1, 72), width - 1)
        stdscr.addnstr(3, 0, f"  Total folders in cache: {len(self.all_folders):,}", width - 1)
        stdscr.addnstr(4, 0, f"  Folders flagged:        {len(self.flags):,}", width - 1)
        if self.flags:
            stdscr.addnstr(6, 0, f"  Output: {self.output_path}", width - 1)
        stdscr.addnstr(8, 0, "  Press any key to exit...", width - 1, curses.A_DIM)
        stdscr.refresh()
        stdscr.getch()


# ── Markdown generation ────────────────────────────────────────────────────


def generate_markdown(flags: list[FolderFlag], output_path: Path) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        f"# IMAPFilter Folder Review — {ts}",
        "",
        f"{len(flags)} folder{'s' if len(flags) != 1 else ''} flagged for reclassification.",
        "",
    ]

    for i, flag in enumerate(flags, 1):
        lines += [
            "---",
            "",
            f"### {i} of {len(flags)} — `{flag.folder}` ({flag.email_count:,} emails)",
            "",
            f"**User comment:** {flag.comment}",
            "",
        ]
        if flag.sample_emails:
            lines += [
                "**Sample emails:**",
                "",
                "| Date | From | Subject |",
                "|---|---|---|",
            ]
            for date_str, from_addr, subject in flag.sample_emails:
                lines.append(
                    f"| {date_str} | {from_addr.replace('|', chr(92) + '|')} "
                    f"| {subject.replace('|', chr(92) + '|')} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "## Instructions for Claude Code",
        "",
        "For each flagged path, find every rule whose `actions` array contains",
        '`"type": "move"` with a `"target"` that equals or starts with the flagged path.',
        "Update the target(s) to reflect the correct location as described in the user's comment.",
        "Rename each affected rule file if the section slug changes",
        "(replace the section token in the filename after the priority prefix).",
        "",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


# ── Entry point ────────────────────────────────────────────────────────────


def launch_folder_classifier(
    cfg: Any,
    output_path: Path,
    *,
    sections: list[str] | None,
    email_limit: int,
    systematic: bool = False,
) -> int:
    db_path = cfg.paths.db_file

    print("Loading folders from cache...")
    all_folders = load_all_folders(db_path, sections)
    if not all_folders:
        print("No folders found in cache. Run build-cache first.")
        return 1

    total_emails = sum(all_folders.values())
    mode = "systematic" if systematic else "tree browser"
    print(f"Found {len(all_folders):,} folders ({total_emails:,} emails). Starting {mode}...")

    tool = FolderClassifierTool(
        all_folders=all_folders,
        db_path=db_path,
        output_path=output_path,
        email_limit=email_limit,
    )

    entry = tool.run_wizard if systematic else tool.run

    flags: list[FolderFlag] = []
    try:
        flags = curses.wrapper(entry)
    except KeyboardInterrupt:
        flags = tool.flags

    if flags:
        generate_markdown(flags, output_path)
        print(f"\nFolder review saved: {output_path}  ({len(flags)} folders flagged)")
    else:
        print("\nNo folders flagged — no output file written.")

    return 0
