"""Stream-based message execution for IMAPFilter."""
from __future__ import annotations

import base64
import imaplib
import re
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from core.backup import backup_messages, BackupResult
from core.logging_utils import JsonLogger, PhaseTimer, now_iso
from core.rule_engine import find_matching_rule, _parse_header_map
from core.stream_processor import StreamMessage
from core.stream_resume import ResumeLog


def _encode_mailbox_utf7(mailbox: str) -> str:
    """Encode a mailbox name to IMAP modified UTF-7 (mUTF-7, RFC 3501).

    '&' must become '&-'; non-printable-ASCII characters use &<modified-base64>-.
    """
    result = []
    i = 0
    while i < len(mailbox):
        ch = mailbox[i]
        if ch == '&':
            result.append('&-')
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            run = []
            while i < len(mailbox) and (ord(mailbox[i]) < 0x20 or ord(mailbox[i]) > 0x7e):
                run.append(mailbox[i])
                i += 1
            encoded = ''.join(run).encode('utf-16-be')
            b64 = base64.b64encode(encoded).decode('ascii').rstrip('=').replace('/', ',')
            result.append(f'&{b64}-')
            continue
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _format_imap_details(response) -> str:
    """Format IMAP response for logging."""
    if not response:
        return ""
    parts: list[str] = []
    for item in response:
        if not item:
            continue
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", "ignore"))
        else:
            parts.append(str(item))
    text = " ".join(part for part in parts if part).strip()
    return f": {text}" if text else ""


def _should_try_create_folder(response) -> bool:
    """Check if IMAP response suggests folder doesn't exist."""
    if not response:
        return False
    first = response[0] if response else None
    if isinstance(first, bytes):
        text = first.decode("utf-8", "ignore").lower()
    else:
        text = str(first).lower() if first else ""
    keywords = ("trycreate", "no such mailbox", "does not exist", "not found", "nonexistent")
    return any(keyword in text for keyword in keywords)


MAX_FOLDER_LINES = 6


def _update_folder_bars(
    folder_bars: list,
    folder_move_counts: dict[str, int],
) -> None:
    """Update the fixed-height folder move display bars."""
    total = sum(folder_move_counts.values())
    sorted_folders = sorted(folder_move_counts.items(), key=lambda x: -x[1])

    folder_bars[0].set_description_str(f"   → Moves: {total} total")

    display = sorted_folders[:MAX_FOLDER_LINES]
    overflow = len(folder_move_counts) - len(display)

    for i, slot_bar in enumerate(folder_bars[1:MAX_FOLDER_LINES + 1], start=0):
        if i < len(display):
            folder, count = display[i]
            short_name = folder.split("/")[-1]
            slot_bar.set_description_str(f"    {short_name:<30} {count}")
        elif i == len(display) and overflow > 0:
            slot_bar.set_description_str(f"    (+{overflow} more folders)")
        else:
            slot_bar.set_description_str("")


def stream_execute(
    client: imaplib.IMAP4 | None,
    rules: Sequence[dict],
    messages: Sequence[StreamMessage],
    *,
    show_progress: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
    backup_moved: bool = False,
    backup_dir: Path | None = None,
    resume_log: ResumeLog | None = None,
    logger: JsonLogger,
    total: int | None = None,
) -> tuple[PhaseTimer, dict[str, int]]:
    """
    Execute rules against a stream of messages.

    Args:
        client: IMAP connection (None for dry-run)
        rules: List of rules sorted by priority (highest first)
        messages: Sequence/generator of StreamMessage objects
        show_progress: Show progress bars
        dry_run: Simulate only, don't execute
        verbose: Log detailed information
        backup_moved: Backup messages before moving them
        backup_dir: Directory for backup files
        resume_log: ResumeLog instance for tracking processed messages
        logger: JsonLogger instance

    Returns:
        Tuple of (timer, stats dict)
    """
    if not dry_run and client is None:
        raise ValueError("IMAP client required for non-dry-run execution")

    if backup_moved and backup_dir is None:
        raise ValueError("backup_dir must be specified when backup_moved is enabled")

    timer = PhaseTimer("stream-execute")

    # Sort rules by priority (lowest first) for consistent matching
    sorted_rules = sorted(rules, key=lambda r: int(r.get("priority", 100)))

    stats = {"done": 0, "skipped": 0, "failed": 0, "matched": 0}
    folder_move_counts: dict[str, int] = {}
    current_folder: str | None = None
    folder_open = False
    pending_expunge = False  # True when ≥1 COPY+STORE completed; flushed at folder boundary
    supports_uid_move = (
        not dry_run
        and client is not None
        and b"MOVE" in getattr(client, "capabilities", ())
    )
    processed_count = 0

    progress_bar = tqdm(
        messages,
        desc="⚙️ Processing messages",
        unit="msg",
        dynamic_ncols=True,
        leave=True,
        disable=not show_progress,
        total=total,
        position=0,
    )
    folder_bars = [
        tqdm(
            total=0,
            bar_format="{desc}",
            position=i + 1,
            leave=False,
            disable=not show_progress,
        )
        for i in range(MAX_FOLDER_LINES + 2)
    ]

    for msg in progress_bar:
        processed_count += 1
        try:
            # Parse header into dict
            header = _parse_header_map(msg.header_text)

            # Find matching rule
            matching_rule = find_matching_rule(header, sorted_rules)

            if not matching_rule:
                stats["skipped"] += 1
                if resume_log:
                    resume_log.mark_processed(msg.folder, msg.uid)
                if verbose:
                    logger.log(
                        "INFO",
                        "stream_no_match",
                        {"folder": msg.folder, "uid": msg.uid},
                        console=f"   ⊘ {msg.folder}/{msg.uid}: no rule matched",
                    )
                continue

            # Support both "actions" (new format) and "action" (old format)
            actions = matching_rule.get("actions", [])
            if not actions and "action" in matching_rule:
                actions = [matching_rule["action"]]

            # Find the move action to get the target folder
            target = None
            for act in actions:
                if act.get("type") == "move":
                    target = act.get("target")
                    break
            if target:
                target = _encode_mailbox_utf7(target)
            if not target:
                stats["skipped"] += 1
                if verbose:
                    logger.log(
                        "INFO",
                        "stream_no_target",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "rule": matching_rule.get("name"),
                        },
                        console=f"   ⊘ {msg.folder}/{msg.uid}: rule '{matching_rule.get('name', '(unnamed)')}' has no target",
                    )
                continue

            # Skip redundant same-folder move actions
            if msg.folder == target:
                stats["skipped"] += 1
                rule_name = matching_rule.get("name", "(unnamed)")
                if verbose:
                    logger.log(
                        "INFO",
                        "stream_skipped_same_folder_move",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "rule": rule_name,
                            "target": target,
                        },
                        console=f"   ⊘ {msg.folder}/{msg.uid} already in target folder {target}",
                    )
                if resume_log:
                    resume_log.mark_processed(msg.folder, msg.uid)
                continue

            stats["matched"] += 1
            rule_name = matching_rule.get("name", "(unnamed)")

            if dry_run:
                stats["done"] += 1
                folder_move_counts[target] = folder_move_counts.get(target, 0) + 1
                _update_folder_bars(folder_bars, folder_move_counts)
                # Mark as processed in resume log
                if resume_log:
                    resume_log.mark_processed(msg.folder, msg.uid)
                if verbose:
                    logger.log(
                        "INFO",
                        "stream_dry_run",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "rule": rule_name,
                            "target": target,
                        },
                        console=f"   🧪 Would move {msg.folder}/{msg.uid} → {target}",
                    )
                continue

            # Actual execution
            assert client is not None

            # Ensure we have the source folder selected
            if current_folder != msg.folder:
                if folder_open:
                    # Flush deferred EXPUNGE before leaving the folder
                    if pending_expunge:
                        try:
                            client.expunge()
                        except imaplib.IMAP4.error:
                            pass
                        pending_expunge = False
                    try:
                        client.close()
                    except imaplib.IMAP4.error:
                        pass
                    folder_open = False

                sel_typ, sel_resp = client.select(f'"{msg.folder}"')
                if sel_typ != "OK":
                    logger.log(
                        "ERROR",
                        "stream_select_failed",
                        {"folder": msg.folder, "status": sel_typ},
                        console=f"   ❌ Failed to select {msg.folder}",
                    )
                    stats["failed"] += 1
                    continue

                current_folder = msg.folder
                folder_open = True

            # Backup message if requested
            if backup_moved and backup_dir is not None:
                try:
                    backup_result = backup_messages(
                        client=client,
                        folder=msg.folder,
                        uids=[msg.uid],
                        backup_dir=backup_dir,
                        backup_type="pre_move",
                        logger=logger,
                        show_progress=False,  # Don't show progress for single message
                        rule_name=rule_name,
                        target_folder=target,
                    )
                    if backup_result.backed_up == 0:
                        logger.log(
                            "ERROR",
                            "stream_backup_failed",
                            {
                                "folder": msg.folder,
                                "uid": msg.uid,
                                "target": target,
                            },
                            console=f"   ❌ Backup failed: {msg.folder}/{msg.uid}",
                        )
                        stats["failed"] += 1
                        continue
                    if backup_result.failed > 0:
                        logger.log(
                            "WARN",
                            "stream_backup_partial",
                            {
                                "folder": msg.folder,
                                "uid": msg.uid,
                                "failed": backup_result.failed,
                            },
                        )
                except Exception as exc:
                    logger.log(
                        "ERROR",
                        "stream_backup_error",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "error": str(exc),
                        },
                        console=f"   ❌ Backup error: {msg.folder}/{msg.uid}: {exc}",
                    )
                    stats["failed"] += 1
                    continue

            # Move message to target folder
            try:
                move_succeeded = False

                if supports_uid_move:
                    # Single atomic UID MOVE — no STORE or EXPUNGE needed
                    try:
                        move_typ, move_resp = client.uid("MOVE", msg.uid, f'"{target}"')
                    except imaplib.IMAP4.error:
                        move_typ = "NO"
                        move_resp = None

                    if move_typ != "OK" and _should_try_create_folder(move_resp):
                        logger.log(
                            "INFO",
                            "stream_create_folder",
                            {"target": target},
                            console=f"   📂 Creating folder: {target}",
                        )
                        create_typ, _cr = client.create(f'"{target}"')
                        if create_typ == "OK":
                            move_typ, move_resp = client.uid("MOVE", msg.uid, f'"{target}"')

                    if move_typ == "OK":
                        move_succeeded = True

                if not move_succeeded:
                    # COPY + STORE + deferred EXPUNGE fallback
                    copy_typ, copy_resp = client.uid("COPY", msg.uid, f'"{target}"')

                    if copy_typ != "OK":
                        if _should_try_create_folder(copy_resp):
                            logger.log(
                                "INFO",
                                "stream_create_folder",
                                {"target": target},
                                console=f"   📂 Creating folder: {target}",
                            )
                            create_typ, _cr = client.create(f'"{target}"')
                            if create_typ == "OK":
                                copy_typ, copy_resp = client.uid("COPY", msg.uid, f'"{target}"')

                        if copy_typ != "OK":
                            error_detail = _format_imap_details(copy_resp)
                            logger.log(
                                "ERROR",
                                "stream_copy_failed",
                                {
                                    "folder": msg.folder,
                                    "uid": msg.uid,
                                    "target": target,
                                    "error": error_detail,
                                },
                                console=f"   ❌ Copy failed: {msg.folder}/{msg.uid} → {target}{error_detail}",
                            )
                            stats["failed"] += 1
                            continue

                    store_typ, store_resp = client.uid("STORE", msg.uid, "+FLAGS", "(\\Deleted)")
                    if store_typ != "OK":
                        error_detail = _format_imap_details(store_resp)
                        logger.log(
                            "ERROR",
                            "stream_store_failed",
                            {
                                "folder": msg.folder,
                                "uid": msg.uid,
                                "error": error_detail,
                            },
                            console=f"   ⚠️ Failed to mark deleted: {msg.folder}/{msg.uid}{error_detail}",
                        )
                        # Copy succeeded so count as done; expunge not needed
                        stats["done"] += 1
                        folder_move_counts[target] = folder_move_counts.get(target, 0) + 1
                        _update_folder_bars(folder_bars, folder_move_counts)
                        if verbose:
                            logger.log(
                                "INFO",
                                "stream_success",
                                {"folder": msg.folder, "uid": msg.uid, "target": target, "rule": rule_name},
                                console=f"   ✅ Moved {msg.folder}/{msg.uid} → {target}",
                            )
                        if resume_log:
                            resume_log.mark_processed(msg.folder, msg.uid)
                        continue

                    # STORE succeeded — defer EXPUNGE to folder boundary
                    pending_expunge = True
                    move_succeeded = True

                # Common success path
                stats["done"] += 1
                folder_move_counts[target] = folder_move_counts.get(target, 0) + 1
                _update_folder_bars(folder_bars, folder_move_counts)

                if resume_log:
                    resume_log.mark_processed(msg.folder, msg.uid)

                if verbose:
                    logger.log(
                        "INFO",
                        "stream_success",
                        {"folder": msg.folder, "uid": msg.uid, "target": target, "rule": rule_name},
                        console=f"   ✅ Moved {msg.folder}/{msg.uid} → {target}",
                    )

            except imaplib.IMAP4.error as exc:
                logger.log(
                    "ERROR",
                    "stream_imap_error",
                    {
                        "folder": msg.folder,
                        "uid": msg.uid,
                        "target": target,
                        "error": str(exc),
                    },
                    console=f"   ❌ IMAP error: {msg.folder}/{msg.uid} → {target}: {exc}",
                )
                stats["failed"] += 1

        except Exception as exc:
            logger.log(
                "ERROR",
                "stream_processing_error",
                {"folder": msg.folder, "uid": msg.uid, "error": str(exc)},
                console=f"   ❌ Processing error: {msg.folder}/{msg.uid}: {exc}",
            )
            stats["failed"] += 1

    for bar in folder_bars:
        bar.close()
    progress_bar.close()

    # Flush deferred EXPUNGE and close folder if open
    if folder_open:
        if pending_expunge:
            try:
                exp_typ, exp_resp = client.expunge()
                if exp_typ != "OK":
                    error_detail = _format_imap_details(exp_resp)
                    logger.log(
                        "WARN",
                        "stream_expunge_failed",
                        {"folder": current_folder, "error": error_detail},
                        console=f"   ⚠️ Expunge warning on {current_folder}{error_detail}",
                    )
            except (imaplib.IMAP4.error, AttributeError):
                pass
        try:
            client.close()
        except (imaplib.IMAP4.error, AttributeError):
            pass

    timer.stop()
    timer.count = processed_count

    # Build per-folder move breakdown for summary
    folder_breakdown = "".join(
        f"      → {folder}: {count}\n"
        for folder, count in sorted(folder_move_counts.items(), key=lambda x: -x[1])
    )

    # Log summary
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "stream-execute",
            "total_messages": processed_count,
            "matched_rules": stats["matched"],
            "moved": stats["done"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
            "moves_by_folder": folder_move_counts,
        },
        console=(
            "\n📊 Summary — Stream Execute\n"
            f"   ✉️  Total messages: {processed_count}\n"
            f"   🎯  Rules matched: {stats['matched']}\n"
            f"   ✅  Moved: {stats['done']}\n"
            f"{folder_breakdown}"
            f"   ⊘  Skipped: {stats['skipped']}\n"
            f"   ❌  Failed: {stats['failed']}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )

    return timer, stats
