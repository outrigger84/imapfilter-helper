"""Stream-based message execution for IMAPFilter."""
from __future__ import annotations

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

    # Sort rules by priority (highest first) for consistent matching
    sorted_rules = sorted(rules, key=lambda r: int(r.get("priority", 100)), reverse=True)

    stats = {"done": 0, "skipped": 0, "failed": 0, "matched": 0}
    current_folder: str | None = None
    folder_open = False

    # Convert to list if it's a generator so we can get total count
    message_list = list(messages)
    total_messages = len(message_list)

    progress_bar = tqdm(
        message_list,
        desc="⚙️ Processing messages",
        unit="msg",
        dynamic_ncols=True,
        leave=True,
        disable=not show_progress,
    )

    for msg in progress_bar:
        try:
            # Parse header into dict
            header = _parse_header_map(msg.header_text)

            # Find matching rule
            matching_rule = find_matching_rule(header, sorted_rules)

            if not matching_rule:
                stats["skipped"] += 1
                if verbose:
                    logger.log(
                        "INFO",
                        "stream_no_match",
                        {"folder": msg.folder, "uid": msg.uid},
                        console=f"   ⊘ {msg.folder}/{msg.uid}: no rule matched",
                    )
                continue

            # Get target folder from rule action
            action = matching_rule.get("action", {})
            target = action.get("target")
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
                        console=f"   ⊘ {msg.folder}/{msg.uid}: rule has no target",
                    )
                continue

            stats["matched"] += 1
            rule_name = matching_rule.get("name", "(unnamed)")

            if dry_run:
                progress_bar.set_postfix_str(f"{msg.folder} → {target}")
                stats["done"] += 1
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

            # Try to copy message to target folder
            try:
                copy_typ, copy_resp = client.uid("COPY", msg.uid, f'"{target}"')

                if copy_typ != "OK":
                    # Try to create folder if it doesn't exist
                    if _should_try_create_folder(copy_resp):
                        logger.log(
                            "INFO",
                            "stream_create_folder",
                            {"target": target},
                            console=f"   📂 Creating folder: {target}",
                        )
                        create_typ, create_resp = client.create(f'"{target}"')
                        if create_typ == "OK":
                            # Retry copy
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

                # Mark message as deleted
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
                    # Continue anyway, message was copied
                    stats["done"] += 1
                    if verbose:
                        logger.log(
                            "INFO",
                            "stream_success",
                            {
                                "folder": msg.folder,
                                "uid": msg.uid,
                                "target": target,
                                "rule": rule_name,
                            },
                            console=f"   ✅ Moved {msg.folder}/{msg.uid} → {target}",
                        )
                    # Mark as processed even if delete failed (copy succeeded)
                    if resume_log:
                        resume_log.mark_processed(msg.folder, msg.uid)
                    continue

                # Expunge the message
                exp_typ, exp_resp = client.expunge()
                if exp_typ != "OK":
                    error_detail = _format_imap_details(exp_resp)
                    logger.log(
                        "WARN",
                        "stream_expunge_failed",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "error": error_detail,
                        },
                        console=f"   ⚠️ Expunge warning: {msg.folder}/{msg.uid}{error_detail}",
                    )
                    # Message still moved even if expunge failed

                stats["done"] += 1
                progress_bar.set_postfix_str(f"{msg.folder} → {target}")

                # Mark as processed in resume log
                if resume_log:
                    resume_log.mark_processed(msg.folder, msg.uid)

                if verbose:
                    logger.log(
                        "INFO",
                        "stream_success",
                        {
                            "folder": msg.folder,
                            "uid": msg.uid,
                            "target": target,
                            "rule": rule_name,
                        },
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

    progress_bar.close()

    # Close folder if open
    if folder_open:
        try:
            client.close()
        except (imaplib.IMAP4.error, AttributeError):
            pass

    timer.stop()
    timer.count = len(message_list)

    # Log summary
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "stream-execute",
            "total_messages": total_messages,
            "matched_rules": stats["matched"],
            "moved": stats["done"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Stream Execute\n"
            f"   ✉️  Total messages: {total_messages}\n"
            f"   🎯  Rules matched: {stats['matched']}\n"
            f"   ✅  Moved: {stats['done']}\n"
            f"   ⊘  Skipped: {stats['skipped']}\n"
            f"   ❌  Failed: {stats['failed']}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )

    return timer, stats
