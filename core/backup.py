"""Message backup utilities for IMAPFilter helper."""
from __future__ import annotations

import json
import mailbox
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from pathlib import Path
from typing import Iterable

import imaplib
from tqdm import tqdm

from core.logging_utils import JsonLogger


@dataclass
class BackupResult:
    """Result of a backup operation."""

    backed_up: int
    failed: int
    backup_path: Path | None

    @property
    def success(self) -> bool:
        return self.backed_up > 0 and self.failed == 0

    @property
    def total_attempted(self) -> int:
        return self.backed_up + self.failed


def _coalesce_fetch_payload(msg_data) -> bytes:
    """Extract message payload from IMAP FETCH response."""
    if not msg_data:
        return b""
    parts: list[bytes] = []
    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            payload = item[1]
            if isinstance(payload, (bytes, bytearray)):
                parts.append(bytes(payload))
        elif isinstance(item, (bytes, bytearray)):
            continue
    return b"".join(parts)


def backup_messages(
    client: imaplib.IMAP4_SSL,
    folder: str,
    uids: list[str],
    backup_dir: Path,
    *,
    backup_type: str = "selective",
    logger: JsonLogger,
    show_progress: bool = True,
    rule_name: str | None = None,
    target_folder: str | None = None,
) -> BackupResult:
    """Backup specific messages from a folder.

    Args:
        client: Authenticated IMAP client
        folder: Source folder name
        uids: List of message UIDs to backup
        backup_dir: Directory to write mbox files
        backup_type: Descriptor for backup type (for filename)
        logger: Logger instance
        show_progress: Whether to show progress bar
        rule_name: Optional rule name for filename context
        target_folder: Optional target folder for filename context

    Returns:
        BackupResult with count of backed up messages and backup path
    """
    if not uids:
        return BackupResult(backed_up=0, failed=0, backup_path=None)

    # Generate timestamp for organization and filename
    timestamp_dt = datetime.now(timezone.utc)
    timestamp_str = timestamp_dt.strftime("%Y%m%dT%H%M%SZ")
    date_path = timestamp_dt.strftime("%Y/%m/%d")

    # Create date-based directory structure
    organized_backup_dir = backup_dir / date_path
    organized_backup_dir.mkdir(parents=True, exist_ok=True)

    # Generate backup filename with enhanced naming
    safe_folder = folder.replace("/", "_").replace(" ", "_") or "folder"

    # Build filename components
    name_parts = [safe_folder, backup_type]
    if rule_name:
        safe_rule = rule_name.replace("/", "_").replace(" ", "_").replace(".", "_")
        name_parts.append(safe_rule)
    if target_folder:
        safe_target = target_folder.replace("/", "_").replace(" ", "_") or "folder"
        name_parts.append(f"to_{safe_target}")
    name_parts.append(timestamp_str)

    filename = "_".join(name_parts) + ".mbox"
    backup_path = organized_backup_dir / filename

    logger.log(
        "INFO",
        "backup_start",
        {
            "folder": folder,
            "count": len(uids),
            "type": backup_type,
            "path": str(backup_path),
        },
        console=f"💾 Backing up {len(uids)} messages from {folder}...",
    )

    # Select folder in read-only mode
    try:
        sel_typ, _ = client.select(f'"{folder}"', readonly=True)
        if sel_typ != "OK":
            logger.log(
                "ERROR",
                "backup_select_failed",
                {"folder": folder},
                console=f"❌ Failed to select folder {folder}",
            )
            return BackupResult(backed_up=0, failed=len(uids), backup_path=None)
    except imaplib.IMAP4.error as exc:
        logger.log(
            "ERROR",
            "backup_select_error",
            {"folder": folder, "error": str(exc)},
            console=f"❌ Error selecting folder {folder}: {exc}",
        )
        return BackupResult(backed_up=0, failed=len(uids), backup_path=None)

    # Create mbox file
    mbox = mailbox.mbox(str(backup_path))

    backed_up = 0
    failed = 0

    progress = tqdm(
        uids,
        desc=f"   📦 Backing up {folder}",
        unit="msg",
        dynamic_ncols=True,
        leave=False,
        position=2,
        disable=not show_progress,
    )

    for uid in progress:
        try:
            # Fetch full message body
            typ, msg_data = client.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK":
                failed += 1
                logger.log(
                    "DEBUG",
                    "backup_fetch_failed",
                    {"folder": folder, "uid": uid, "status": typ},
                )
                continue

            raw_msg = _coalesce_fetch_payload(msg_data)
            if not raw_msg:
                failed += 1
                logger.log(
                    "DEBUG",
                    "backup_empty_payload",
                    {"folder": folder, "uid": uid},
                )
                continue

            # Add to mbox
            mbox.add(mailbox.mboxMessage(message_from_bytes(raw_msg)))
            backed_up += 1

        except Exception as exc:
            failed += 1
            logger.log(
                "WARN",
                "backup_message_failed",
                {"folder": folder, "uid": uid, "error": str(exc)},
            )

    progress.close()

    # Flush and close mbox
    try:
        mbox.flush()
        mbox.close()
    except Exception as exc:
        logger.log(
            "ERROR",
            "backup_close_failed",
            {"path": str(backup_path), "error": str(exc)},
        )

    # Log summary
    if backed_up > 0:
        logger.log(
            "INFO",
            "backup_complete",
            {
                "folder": folder,
                "backed_up": backed_up,
                "failed": failed,
                "path": str(backup_path),
            },
            console=f"✅ Backed up {backed_up}/{len(uids)} messages to {backup_path.name}",
        )
    else:
        logger.log(
            "WARN",
            "backup_no_messages",
            {"folder": folder, "attempted": len(uids)},
            console=f"⚠️  Failed to backup any messages from {folder}",
        )

    return BackupResult(
        backed_up=backed_up,
        failed=failed,
        backup_path=backup_path if backed_up > 0 else None,
    )


def backup_all_cached_messages(
    client: imaplib.IMAP4_SSL,
    db: sqlite3.Connection,
    backup_dir: Path,
    *,
    folders: list[str] | None = None,
    logger: JsonLogger,
    show_progress: bool = True,
) -> dict[str, BackupResult]:
    """Backup all messages currently in the cache.

    Args:
        client: Authenticated IMAP client
        db: Database connection with cached headers
        backup_dir: Directory to write mbox files
        folders: Optional list of folders to backup (None = all)
        logger: Logger instance
        show_progress: Whether to show progress bars

    Returns:
        Dictionary mapping folder names to BackupResult objects
    """
    # Get all folders with cached messages
    if folders:
        folder_filter = " AND folder IN (" + ",".join("?" * len(folders)) + ")"
        params: tuple = tuple(folders)
    else:
        folder_filter = ""
        params = ()

    cursor = db.execute(
        f"SELECT folder, COUNT(*) FROM headers WHERE 1=1 {folder_filter} "
        "GROUP BY folder ORDER BY folder",
        params,
    )

    folder_counts = cursor.fetchall()

    if not folder_counts:
        logger.log(
            "INFO",
            "backup_no_cached_messages",
            console="ℹ️  No cached messages to backup",
        )
        return {}

    logger.log(
        "INFO",
        "backup_all_start",
        {"folder_count": len(folder_counts)},
        console=f"💾 Backing up all cached messages from {len(folder_counts)} folders...",
    )

    results = {}

    folders_bar = tqdm(
        folder_counts,
        desc="📦 Backing up folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    for folder, count in folders_bar:
        folders_bar.set_postfix_str(folder)

        # Get all UIDs for this folder
        uid_cursor = db.execute(
            "SELECT uid FROM headers WHERE folder = ? ORDER BY uid",
            (folder,),
        )
        uids = [row[0] for row in uid_cursor.fetchall()]

        # Backup this folder
        result = backup_messages(
            client=client,
            folder=folder,
            uids=uids,
            backup_dir=backup_dir,
            backup_type="full",
            logger=logger,
            show_progress=show_progress,
        )

        results[folder] = result

    folders_bar.close()

    # Summary
    total_backed_up = sum(r.backed_up for r in results.values())
    total_failed = sum(r.failed for r in results.values())

    logger.log(
        "INFO",
        "backup_all_complete",
        {
            "folders": len(results),
            "backed_up": total_backed_up,
            "failed": total_failed,
        },
        console=(
            f"\n✅ Full backup complete:\n"
            f"   📂 Folders: {len(results)}\n"
            f"   ✉️  Messages backed up: {total_backed_up}\n"
            f"   ❌ Failed: {total_failed}\n"
        ),
    )

    return results
