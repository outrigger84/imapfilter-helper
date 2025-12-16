"""Execute queued actions."""
from __future__ import annotations

import concurrent.futures
import datetime
import gc
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import imaplib
from email.parser import HeaderParser
from email.policy import default
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from tqdm import tqdm

from core.backup import backup_messages, backup_all_cached_messages, BackupResult
from core.connection_pool import IMAPConnectionPool
from core.database import init_db
from core.logging_utils import JsonLogger, PhaseTimer, now_iso


HEADER_PARSER = HeaderParser(policy=default)


def _quote_mailbox(mailbox: str) -> str:
    """
    Quote a mailbox name for use in IMAP commands.

    This properly escapes and quotes mailbox names that contain spaces,
    quotes, backslashes, or other special characters.
    """
    if not mailbox:
        return '""'
    # Escape backslashes and quotes, then wrap in quotes
    escaped = mailbox.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _imap_response_text(response: Iterable[bytes | str] | None) -> str:
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
    return " ".join(part for part in parts if part).strip()


def _format_imap_details(response: Iterable[bytes | str] | None) -> str:
    text = _imap_response_text(response)
    return f": {text}" if text else ""


def _should_try_create_folder(response: Iterable[bytes | str] | None) -> bool:
    text = _imap_response_text(response).lower()
    if not text:
        return False
    keywords = (
        "trycreate",
        "no such mailbox",
        "does not exist",
        "not found",
        "nonexistent",
    )
    return any(keyword in text for keyword in keywords)


# ============================================================================
# Phase 2, Part C: Verification and Backup Helpers
# ============================================================================


def _extract_message_id(
    client: imaplib.IMAP4,
    folder: str,
    uid: str,
    logger: JsonLogger | None = None,
) -> str | None:
    """
    Extract Message-ID header from a message.

    Args:
        client: IMAP client connection
        folder: Source folder containing the message
        uid: Message UID
        logger: Optional logger for detailed logging

    Returns:
        Message-ID string (without angle brackets) or None if not found
    """
    try:
        # Fetch message headers
        typ, msg_parts = client.uid("FETCH", uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK" or not msg_parts or not msg_parts[0]:
            if logger:
                logger.log(
                    "DEBUG",
                    "extract_message_id_fetch_failed",
                    {"folder": folder, "uid": uid, "status": typ},
                )
            return None

        # Parse response
        if isinstance(msg_parts[0], tuple) and len(msg_parts[0]) >= 2:
            header_bytes = msg_parts[0][1]
        else:
            if logger:
                logger.log(
                    "DEBUG",
                    "extract_message_id_invalid_response",
                    {"folder": folder, "uid": uid},
                )
            return None

        if not isinstance(header_bytes, bytes):
            return None

        # Decode header text
        header_text = header_bytes.decode("utf-8", "ignore")

        # Extract Message-ID using regex
        # Pattern matches: Message-ID: <value> or Message-Id: value
        match = re.search(
            r"Message-I[Dd]:\s*<?([^\s<>]+)>?",
            header_text,
            re.IGNORECASE | re.MULTILINE,
        )
        if match:
            message_id = match.group(1).strip()
            return message_id

        if logger:
            logger.log(
                "DEBUG",
                "extract_message_id_not_found",
                {"folder": folder, "uid": uid},
            )
        return None

    except Exception as exc:
        if logger:
            logger.log(
                "WARN",
                "extract_message_id_exception",
                {"folder": folder, "uid": uid, "error": str(exc)},
            )
        return None


def _verify_move(
    client: imaplib.IMAP4,
    source_folder: str,
    target_folder: str,
    message_id: str,
    uid: str,
    logger: JsonLogger | None = None,
) -> tuple[bool, str | None]:
    """
    Verify that a message was successfully moved from source to target folder.

    This function searches for the message by Message-ID in both source and
    target folders to confirm the move was successful.

    Args:
        client: IMAP client connection
        source_folder: Source folder (should NOT contain message after move)
        target_folder: Target folder (should contain message after move)
        message_id: Message-ID header value for verification
        uid: Original UID in source folder (for logging)
        logger: Optional logger for detailed logging

    Returns:
        Tuple of (verified, error_message) where:
        - verified is True if move was successful
        - error_message is None on success, error string on failure
    """
    if not message_id:
        # Can't verify without Message-ID - skip verification
        if logger:
            logger.log(
                "DEBUG",
                "verify_move_no_message_id",
                {"source": source_folder, "target": target_folder, "uid": uid},
            )
        return (True, None)

    try:
        # Escape Message-ID for IMAP SEARCH
        # Remove angle brackets if present and escape quotes
        message_id_clean = message_id.strip("<>")
        escaped_id = message_id_clean.replace('"', '\\"')

        # Search in target folder (should be present)
        try:
            sel_typ, _ = client.select(f'"{target_folder}"', readonly=True)
            if sel_typ != "OK":
                error_msg = f"Cannot select target folder {target_folder}"
                if logger:
                    logger.log(
                        "WARN",
                        "verify_move_target_select_failed",
                        {"target": target_folder, "uid": uid},
                    )
                return (False, error_msg)

            # SEARCH for Message-ID in target
            search_typ, search_resp = client.uid("SEARCH", None, "HEADER", "Message-ID", escaped_id)

            if search_typ != "OK":
                error_msg = f"SEARCH failed in target folder: {search_typ}"
                if logger:
                    logger.log(
                        "WARN",
                        "verify_move_target_search_failed",
                        {"target": target_folder, "uid": uid, "status": search_typ},
                    )
                return (False, error_msg)

            # Check if message found in target
            target_found = False
            if search_resp and search_resp[0]:
                # Parse response
                if isinstance(search_resp[0], bytes):
                    result_text = search_resp[0].decode("ascii", "ignore").strip()
                else:
                    result_text = str(search_resp[0]).strip()
                target_found = bool(result_text)

            if not target_found:
                error_msg = f"Message-ID {message_id_clean} not found in target folder"
                if logger:
                    logger.log(
                        "WARN",
                        "verify_move_not_in_target",
                        {"source": source_folder, "target": target_folder, "uid": uid, "message_id": message_id_clean},
                        console=f"      ⚠️  Verification failed: {uid} not found in {target_folder}",
                    )
                return (False, error_msg)

        finally:
            # Always try to close the target folder selection
            try:
                client.close()
            except Exception:
                pass

        # Optionally check source folder (should NOT be present)
        # This is a secondary check - if it fails, we still consider the move successful
        # since the message is in the target folder
        try:
            sel_typ, _ = client.select(f'"{source_folder}"', readonly=True)
            if sel_typ == "OK":
                search_typ, search_resp = client.uid("SEARCH", None, "HEADER", "Message-ID", escaped_id)
                if search_typ == "OK" and search_resp and search_resp[0]:
                    if isinstance(search_resp[0], bytes):
                        result_text = search_resp[0].decode("ascii", "ignore").strip()
                    else:
                        result_text = str(search_resp[0]).strip()

                    if result_text:
                        # Message still in source - this is suspicious but not necessarily wrong
                        # (could be a copy instead of move, or another message with same Message-ID)
                        if logger:
                            logger.log(
                                "DEBUG",
                                "verify_move_still_in_source",
                                {"source": source_folder, "target": target_folder, "uid": uid, "message_id": message_id_clean},
                            )
        except Exception:
            # Ignore errors checking source folder
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

        # Verification passed - message found in target
        if logger:
            logger.log(
                "DEBUG",
                "verify_move_success",
                {"source": source_folder, "target": target_folder, "uid": uid, "message_id": message_id_clean},
            )
        return (True, None)

    except Exception as exc:
        error_msg = f"Verification exception: {str(exc)}"
        if logger:
            logger.log(
                "WARN",
                "verify_move_exception",
                {"source": source_folder, "target": target_folder, "uid": uid, "error": str(exc)},
            )
        return (False, error_msg)


def _backup_message(
    client: imaplib.IMAP4,
    folder: str,
    uid: str,
    backup_dir: Path,
    logger: JsonLogger | None = None,
) -> tuple[bool, str | None]:
    """
    Backup a single message to disk as .eml file.

    Creates a backup directory structure: backup_dir/YYYY-MM-DD/folder_name/uid.eml

    Args:
        client: IMAP client connection
        folder: Source folder containing the message
        uid: Message UID
        backup_dir: Root backup directory
        logger: Optional logger for detailed logging

    Returns:
        Tuple of (success, error_message) where:
        - success is True if backup succeeded
        - error_message is None on success, error string on failure
    """
    try:
        # Determine backup path with date-based structure
        today = datetime.date.today().isoformat()  # YYYY-MM-DD
        folder_safe = folder.replace("/", "_").replace("\\", "_")  # Handle subfolders
        backup_path = backup_dir / today / folder_safe / f"{uid}.eml"

        # Create parent directories
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as mkdir_exc:
            error_msg = f"Failed to create backup directory: {str(mkdir_exc)}"
            if logger:
                logger.log(
                    "ERROR",
                    "backup_mkdir_failed",
                    {"folder": folder, "uid": uid, "path": str(backup_path.parent), "error": str(mkdir_exc)},
                )
            return (False, error_msg)

        # Fetch full message (RFC822)
        try:
            typ, msg_parts = client.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not msg_parts or not msg_parts[0]:
                error_msg = f"Failed to fetch message: {typ}"
                if logger:
                    logger.log(
                        "ERROR",
                        "backup_fetch_failed",
                        {"folder": folder, "uid": uid, "status": typ},
                    )
                return (False, error_msg)

            # Extract message bytes from response
            message_bytes = None
            if isinstance(msg_parts[0], tuple) and len(msg_parts[0]) >= 2:
                message_bytes = msg_parts[0][1]

            if not message_bytes or not isinstance(message_bytes, bytes):
                error_msg = "Invalid message data from FETCH"
                if logger:
                    logger.log(
                        "ERROR",
                        "backup_invalid_data",
                        {"folder": folder, "uid": uid},
                    )
                return (False, error_msg)

        except imaplib.IMAP4.error as fetch_exc:
            error_msg = f"IMAP error fetching message: {str(fetch_exc)}"
            if logger:
                logger.log(
                    "ERROR",
                    "backup_fetch_exception",
                    {"folder": folder, "uid": uid, "error": str(fetch_exc)},
                )
            return (False, error_msg)

        # Write to disk
        try:
            with open(backup_path, "wb") as f:
                f.write(message_bytes)

            if logger:
                logger.log(
                    "DEBUG",
                    "backup_message_success",
                    {"folder": folder, "uid": uid, "path": str(backup_path), "size": len(message_bytes)},
                )
            return (True, None)

        except IOError as write_exc:
            error_msg = f"Failed to write backup file: {str(write_exc)}"
            if logger:
                logger.log(
                    "ERROR",
                    "backup_write_failed",
                    {"folder": folder, "uid": uid, "path": str(backup_path), "error": str(write_exc)},
                )
            return (False, error_msg)

    except Exception as exc:
        error_msg = f"Backup exception: {str(exc)}"
        if logger:
            logger.log(
                "ERROR",
                "backup_message_exception",
                {"folder": folder, "uid": uid, "error": str(exc)},
            )
        return (False, error_msg)


# ============================================================================
# Phase 2: Action Type Support - Helper Functions for Parallel Execution
# ============================================================================


def _perform_move_operation(
    client: imaplib.IMAP4,
    temp_db: sqlite3.Connection,
    action: dict,
    *,
    verify_moves: bool = False,
    backup_moved: bool = False,
    backup_dir: Path | None = None,
    dry_run: bool = False,
    supports_uid_move: bool = False,
    logger: JsonLogger | None = None,
    verbose: bool = False,
) -> tuple[str, str]:
    """
    Perform a move operation for a single action.

    Args:
        client: IMAP client connection
        temp_db: Per-worker temporary database connection
        action: Action dict with {id, uid, folder, target, rule_name, ...}
        verify_moves: Whether to verify moves by Message-ID search
        backup_moved: Whether to backup before moving
        backup_dir: Directory for backup files
        dry_run: If True, only simulate the operation
        supports_uid_move: Whether server supports UID MOVE extension
        logger: Optional logger for detailed logging
        verbose: Enable verbose logging

    Returns:
        Tuple of (status, error_message) where status is 'done', 'failed', or 'skipped'
    """
    action_id = action['id']
    uid = action['uid']
    folder = action['folder']
    target = action.get('target')
    rule_name = action.get('rule_name')

    # Guard: Skip if email is already in target folder
    if target and folder == target:
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('skipped', now_iso(), 'Already in target folder', action_id)
        )
        temp_db.commit()
        if logger and verbose:
            logger.log(
                "INFO",
                "skipped_redundant_move",
                {"folder": folder, "uid": uid, "target": target},
                console=f"      ⊘ {folder}/{uid} already in {target}",
            )
        return ('skipped', 'Already in target folder')

    if dry_run:
        # Simulate backup if requested
        if backup_moved and backup_dir is not None:
            if logger and verbose:
                backup_path = backup_dir / datetime.date.today().isoformat() / folder.replace("/", "_") / f"{uid}.eml"
                logger.log(
                    "INFO",
                    "parallel_dry_run_backup",
                    {"folder": folder, "uid": uid, "backup_path": str(backup_path)},
                    console=f"      📝 Would backup {folder}/{uid} to {backup_path}",
                )

        # Simulate move operation - just mark as done
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('done', now_iso(), action_id)
        )
        temp_db.commit()
        if logger and verbose:
            logger.log(
                "INFO",
                "parallel_dry_run_move",
                {"folder": folder, "uid": uid, "target": target},
                console=f"      📝 Would move {folder}/{uid} → {target}",
            )

        # Simulate verification if requested
        if verify_moves:
            if logger and verbose:
                logger.log(
                    "INFO",
                    "parallel_dry_run_verify",
                    {"folder": folder, "uid": uid, "target": target},
                    console=f"      📝 Would verify move of {folder}/{uid} → {target}",
                )

        return ('done', '')

    try:
        # Extract Message-ID for verification (if needed)
        message_id = None
        if verify_moves and target:
            message_id = _extract_message_id(client, folder, uid, logger)

        # Backup if requested
        if backup_moved and backup_dir is not None:
            backup_success, backup_error = _backup_message(
                client=client,
                folder=folder,
                uid=uid,
                backup_dir=backup_dir,
                logger=logger,
            )
            if not backup_success:
                error_msg = f"Backup failed: {backup_error}"
                temp_db.execute(
                    "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                    ('failed', now_iso(), error_msg, action_id)
                )
                temp_db.commit()
                if logger:
                    logger.log(
                        "ERROR",
                        "parallel_backup_failed",
                        {"folder": folder, "uid": uid, "error": backup_error},
                        console=f"      ❌ Backup failed for {folder}/{uid}: {backup_error}",
                    )
                return ('failed', error_msg)

        # Try UID MOVE if supported
        if supports_uid_move and target:
            try:
                move_typ, move_resp = client.uid("MOVE", uid, f'"{target}"')
                if move_typ == "OK":
                    # Success - mark as done and track deleted message
                    temp_db.execute(
                        "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
                        ('done', now_iso(), action_id)
                    )
                    temp_db.execute(
                        "INSERT OR IGNORE INTO deleted_headers (folder, uid) VALUES (?, ?)",
                        (folder, uid)
                    )
                    temp_db.commit()

                    # Verify move if requested
                    if verify_moves and target and message_id:
                        verified, verify_error = _verify_move(
                            client=client,
                            source_folder=folder,
                            target_folder=target,
                            message_id=message_id,
                            uid=uid,
                            logger=logger,
                        )
                        if not verified:
                            # Update action to failed status
                            temp_db.execute(
                                "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                                ('failed', verify_error, action_id)
                            )
                            temp_db.commit()
                            if logger:
                                logger.log(
                                    "ERROR",
                                    "parallel_verify_failed",
                                    {"folder": folder, "uid": uid, "target": target, "error": verify_error},
                                    console=f"      ⚠️  Verification failed: {folder}/{uid} → {target}",
                                )
                            return ('failed', verify_error or 'Verification failed')

                    return ('done', '')
            except Exception:
                # Fall back to COPY+STORE
                pass

        # COPY + STORE method
        copy_typ, copy_resp = client.uid("COPY", uid, f'"{target}"')

        # Handle folder creation if needed
        if copy_typ != "OK" and _should_try_create_folder(copy_resp):
            create_typ, create_resp = client.create(f'"{target}"')
            if create_typ == "OK":
                if logger and verbose:
                    logger.log(
                        "INFO",
                        "parallel_create_folder",
                        {"target": target},
                        console=f"      📁 Created folder {target}",
                    )
                # Retry copy
                copy_typ, copy_resp = client.uid("COPY", uid, f'"{target}"')

        if copy_typ != "OK":
            error_msg = f"COPY failed: {_format_imap_details(copy_resp)}"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            return ('failed', error_msg)

        # Mark as deleted
        store_typ, store_resp = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if store_typ != "OK":
            error_msg = f"STORE failed: {_format_imap_details(store_resp)}"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            return ('failed', error_msg)

        # Success - mark as done and track deleted message
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('done', now_iso(), action_id)
        )
        temp_db.execute(
            "INSERT OR IGNORE INTO deleted_headers (folder, uid) VALUES (?, ?)",
            (folder, uid)
        )
        temp_db.commit()

        if logger and verbose:
            logger.log(
                "INFO",
                "parallel_move_done",
                {"folder": folder, "uid": uid, "target": target},
                console=f"      ✅ Moved {folder}/{uid} → {target}",
            )

        # Verify move if requested
        if verify_moves and target and message_id:
            verified, verify_error = _verify_move(
                client=client,
                source_folder=folder,
                target_folder=target,
                message_id=message_id,
                uid=uid,
                logger=logger,
            )
            if not verified:
                # Update action to failed status
                temp_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', verify_error, action_id)
                )
                temp_db.commit()
                if logger:
                    logger.log(
                        "ERROR",
                        "parallel_verify_failed",
                        {"folder": folder, "uid": uid, "target": target, "error": verify_error},
                        console=f"      ⚠️  Verification failed: {folder}/{uid} → {target}",
                    )
                return ('failed', verify_error or 'Verification failed')

        return ('done', '')

    except imaplib.IMAP4.error as exc:
        message = str(exc).lower()
        # Check for missing message errors
        if any(keyword in message for keyword in ["no such message", "uid command error", "failed"]):
            # Message likely already moved or doesn't exist - skip it
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('skipped', now_iso(), str(exc), action_id)
            )
            temp_db.execute(
                "INSERT OR IGNORE INTO deleted_headers (folder, uid) VALUES (?, ?)",
                (folder, uid)
            )
            temp_db.commit()
            if logger:
                logger.log(
                    "WARN",
                    "parallel_message_missing",
                    {"folder": folder, "uid": uid, "error": str(exc)},
                )
            return ('skipped', str(exc))
        else:
            # Other IMAP error - mark as failed
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), str(exc), action_id)
            )
            temp_db.commit()
            return ('failed', str(exc))

    except Exception as exc:
        # Unexpected error - mark as failed
        error_msg = str(exc)
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('failed', now_iso(), error_msg, action_id)
        )
        temp_db.commit()
        return ('failed', error_msg)


def _perform_batch_keyword_operations(
    client: imaplib.IMAP4,
    temp_db: sqlite3.Connection,
    folder: str,
    actions: list[dict],
    action_type: str,
    logger: JsonLogger | None = None,
    verbose: bool = False,
) -> tuple[int, int]:
    """
    Perform batch keyword operations (set or remove) for multiple actions.

    Dramatically reduces IMAP operations by batching multiple UIDs with the same
    keywords into a single STORE command.

    Args:
        client: IMAP client connection
        temp_db: Per-worker temporary database connection
        folder: Source folder containing messages
        actions: List of action dicts with {id, uid, action_type, action_data, ...}
        action_type: Either 'set_keywords' or 'remove_keywords'
        logger: Optional logger for detailed logging
        verbose: Enable verbose logging

    Returns:
        Tuple of (actions_done, actions_failed)
    """
    actions_done = 0
    actions_failed = 0

    # First, select the folder once
    try:
        sel_typ, sel_resp = client.select(f'"{folder}"', readonly=False)
        if sel_typ != "OK":
            error_msg = f"Cannot open folder: {_imap_response_text(sel_resp)}"
            for action in actions:
                temp_db.execute(
                    "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                    (action["id"], "failed", now_iso(), error_msg),
                )
                actions_failed += 1
            temp_db.commit()
            return actions_done, actions_failed
    except Exception as exc:
        error_msg = str(exc)
        for action in actions:
            temp_db.execute(
                "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                (action["id"], "failed", now_iso(), error_msg),
            )
            actions_failed += 1
        temp_db.commit()
        return actions_done, actions_failed

    # Parse and group actions by keyword set
    uid_keyword_map: dict[str, tuple[int, list[str]]] = {}  # uid -> (action_id, keywords)
    invalid_actions: list[tuple[int, str]] = []  # (action_id, reason)

    for action in actions:
        action_id = action["id"]
        uid = action["uid"]
        action_data = action.get("action_data")

        keywords = []
        if action_data:
            try:
                data = json.loads(action_data)
                keywords = data.get("keywords", [])
            except json.JSONDecodeError:
                invalid_actions.append((action_id, "Invalid action_data JSON"))
                continue

        if not keywords:
            invalid_actions.append((action_id, "No keywords specified"))
            continue

        uid_keyword_map[uid] = (action_id, keywords)

    # Handle invalid actions
    for action_id, reason in invalid_actions:
        temp_db.execute(
            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
            (action_id, "skipped", now_iso(), reason),
        )
        actions_failed += 1

    # Group UIDs by keyword set for batching
    if uid_keyword_map:
        keyword_set_to_data: dict[tuple, list[tuple[str, int]]] = {}  # keyword_tuple -> [(uid, action_id)]
        for uid, (action_id, keywords) in uid_keyword_map.items():
            key = tuple(sorted(keywords))
            if key not in keyword_set_to_data:
                keyword_set_to_data[key] = []
            keyword_set_to_data[key].append((uid, action_id))

        # Execute one STORE per keyword set (batch operation)
        for keyword_tuple, uid_action_pairs in keyword_set_to_data.items():
            keywords = list(keyword_tuple)
            uids = [uid for uid, _ in uid_action_pairs]
            uid_str = ",".join(uids)
            flags_str = " ".join(keywords)

            try:
                if action_type == "set_keywords":
                    typ, resp = client.uid("STORE", uid_str, "+FLAGS", f"({flags_str})")
                else:  # remove_keywords
                    typ, resp = client.uid("STORE", uid_str, "-FLAGS", f"({flags_str})")

                # Record result for each UID
                for uid, action_id in uid_action_pairs:
                    if typ == "OK":
                        temp_db.execute(
                            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                            (action_id, "done", now_iso(), None),
                        )
                        actions_done += 1
                    else:
                        error_msg = f"STORE {action_type} failed: {_format_imap_details(resp)}"
                        temp_db.execute(
                            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                            (action_id, "failed", now_iso(), error_msg),
                        )
                        actions_failed += 1

            except Exception as exc:
                error_msg = str(exc)
                for uid, action_id in uid_action_pairs:
                    temp_db.execute(
                        "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                        (action_id, "failed", now_iso(), error_msg),
                    )
                    actions_failed += 1

    temp_db.commit()
    return actions_done, actions_failed


def _perform_keyword_operation(
    client: imaplib.IMAP4,
    temp_db: sqlite3.Connection,
    action: dict,
    *,
    dry_run: bool = False,
    strict: bool = False,
    logger: JsonLogger | None = None,
    verbose: bool = False,
) -> tuple[str, str]:
    """
    Perform a keyword operation (set or remove) for a single action.

    Args:
        client: IMAP client connection
        temp_db: Per-worker temporary database connection
        action: Action dict with {id, uid, folder, action_type, action_data, ...}
        dry_run: If True, only simulate the operation
        strict: If True, raise exceptions on errors instead of returning failed status
        logger: Optional logger for detailed logging
        verbose: Enable verbose logging

    Returns:
        Tuple of (status, error_message) where status is 'done', 'failed', or 'skipped'
    """
    action_id = action['id']
    uid = action['uid']
    folder = action['folder']
    action_type = action.get('action_type', 'set_keywords')
    action_data = action.get('action_data')

    # Parse keywords from action_data
    keywords = []
    if action_data:
        try:
            data = json.loads(action_data)
            keywords = data.get('keywords', [])
        except json.JSONDecodeError:
            error_msg = "Invalid action_data JSON"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            if strict:
                raise ValueError(error_msg)
            return ('failed', error_msg)

    if not keywords:
        # No keywords to process - skip
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('skipped', now_iso(), action_id)
        )
        temp_db.commit()
        return ('skipped', 'No keywords specified')

    # Validate keywords
    for keyword in keywords:
        if not keyword:
            error_msg = "Empty keyword provided"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            if strict:
                raise ValueError(error_msg)
            return ('failed', error_msg)
        if ' ' in keyword:
            error_msg = f"Invalid keyword '{keyword}': contains space"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            if strict:
                raise ValueError(error_msg)
            return ('failed', error_msg)
        if '"' in keyword:
            error_msg = f"Invalid keyword '{keyword}': contains quote"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            if strict:
                raise ValueError(error_msg)
            return ('failed', error_msg)

    # Check IMAP limit for custom flags (32 per message)
    if len(keywords) > 32:
        error_msg = f"Too many keywords ({len(keywords)}, max 32 per IMAP spec)"
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('failed', now_iso(), error_msg, action_id)
        )
        temp_db.commit()
        if strict:
            raise ValueError(error_msg)
        return ('failed', error_msg)

    if dry_run:
        # Simulate operation - just mark as done
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('done', now_iso(), action_id)
        )
        temp_db.commit()
        if logger and verbose:
            logger.log(
                "INFO",
                "parallel_dry_run_keyword",
                {"folder": folder, "uid": uid, "action_type": action_type, "keywords": keywords},
                console=f"      📝 Would {action_type} on {folder}/{uid}: {keywords}",
            )
        return ('done', '')

    try:
        # Build flags string
        flags_str = " ".join(keywords)

        # Execute STORE command
        if action_type == 'set_keywords':
            typ, resp = client.uid("STORE", uid, "+FLAGS", f"({flags_str})")
        else:  # remove_keywords
            typ, resp = client.uid("STORE", uid, "-FLAGS", f"({flags_str})")

        if typ == "OK":
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
                ('done', now_iso(), action_id)
            )
            temp_db.commit()
            if logger and verbose:
                logger.log(
                    "INFO",
                    "parallel_keyword_done",
                    {"folder": folder, "uid": uid, "action_type": action_type, "keywords": keywords},
                    console=f"      🏷️  {action_type} on {folder}/{uid}: {keywords}",
                )
            return ('done', '')
        else:
            error_msg = f"STORE {action_type} failed: {_format_imap_details(resp)}"
            temp_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            temp_db.commit()
            if strict:
                raise imaplib.IMAP4.error(error_msg)
            return ('failed', error_msg)

    except Exception as exc:
        error_msg = str(exc)
        temp_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('failed', now_iso(), error_msg, action_id)
        )
        temp_db.commit()
        if strict:
            raise
        return ('failed', error_msg)


def _verify_move_operation(
    client: imaplib.IMAP4,
    temp_db: sqlite3.Connection,
    action: dict,
    message_id: str | None,
    *,
    logger: JsonLogger | None = None,
    verbose: bool = False,
) -> bool:
    """
    Verify a move operation by searching for the message in source and target folders.

    Args:
        client: IMAP client connection
        temp_db: Per-worker temporary database connection
        action: Action dict with {id, uid, folder, target, ...}
        message_id: Message-ID header value for verification
        logger: Optional logger for detailed logging
        verbose: Enable verbose logging

    Returns:
        True if verification passed, False if failed
    """
    if not message_id:
        # Can't verify without Message-ID
        return True

    action_id = action['id']
    uid = action['uid']
    folder = action['folder']
    target = action.get('target')

    if not target:
        return True

    try:
        # Search criteria for Message-ID
        escaped = message_id.replace('"', '\\"')
        criteria = f'(UNDELETED HEADER Message-ID "{escaped}")'

        # Check source folder (should NOT be present)
        sel_typ, _ = client.select(f'"{folder}"', readonly=True)
        if sel_typ == "OK":
            search_typ, search_resp = client.uid("SEARCH", None, criteria)
            if search_typ == "OK" and search_resp and search_resp[0]:
                # Message still in source - verification failed
                temp_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', 'Verification failed: message still in source', action_id)
                )
                temp_db.commit()
                if logger and verbose:
                    logger.log(
                        "WARN",
                        "parallel_verify_failed_source",
                        {"folder": folder, "uid": uid, "target": target},
                        console=f"      ⚠️  Verify failed: {folder}/{uid} still in source",
                    )
                return False

        # Check target folder (should be present)
        sel_typ, _ = client.select(f'"{target}"', readonly=True)
        if sel_typ == "OK":
            search_typ, search_resp = client.uid("SEARCH", None, criteria)
            if search_typ != "OK" or not search_resp or not search_resp[0]:
                # Message not in target - verification failed
                temp_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', 'Verification failed: message not in target', action_id)
                )
                temp_db.commit()
                if logger and verbose:
                    logger.log(
                        "WARN",
                        "parallel_verify_failed_target",
                        {"folder": folder, "uid": uid, "target": target},
                        console=f"      ⚠️  Verify failed: {folder}/{uid} not in {target}",
                    )
                return False

        # Verification passed
        if logger and verbose:
            logger.log(
                "INFO",
                "parallel_verify_passed",
                {"folder": folder, "uid": uid, "target": target},
                console=f"      ✅ Verified {folder}/{uid} → {target}",
            )
        return True

    except Exception as exc:
        # Verification error - log but don't fail the action
        if logger:
            logger.log(
                "WARN",
                "parallel_verify_error",
                {"folder": folder, "uid": uid, "target": target, "error": str(exc)},
            )
        return True  # Allow action to succeed despite verification error


# ============================================================================
# End Phase 2 Helper Functions
# ============================================================================

def execute_actions(
    client: imaplib.IMAP4 | None,
    db,
    *,
    show_progress: bool,
    dry_run: bool,
    strict: bool,
    logger: JsonLogger,
    verbose: bool = False,
    limit: int | None = None,
    folders: Sequence[str] | None = None,
    verify_moves: bool = False,
    backup_moved: bool = False,
    backup_all: bool = False,
    backup_dir: Path | None = None,
) -> tuple[PhaseTimer, Dict[str, int]]:
    if not dry_run and client is None:
        raise ValueError("An IMAP client is required when not running in dry-run mode")

    timer = PhaseTimer("execute")

    # Validate backup parameters
    if (backup_moved or backup_all) and backup_dir is None:
        raise ValueError("backup_dir must be specified when backup is enabled")

    if backup_moved and backup_all:
        logger.log(
            "WARN",
            "backup_both_enabled",
            console="⚠️  Both --backup-moved and --backup-all specified. Using --backup-all.",
        )
        backup_moved = False  # backup_all takes precedence

    logger.log(
        "INFO",
        "execute_log_hint",
        {"log_file": str(logger.log_file)},
        console=f"📝 Detailed logs: {logger.log_file}",
    )

    folder_params = tuple(folders) if folders else ()
    folder_filter = ""
    if folder_params:
        placeholders = ",".join("?" for _ in folder_params)
        folder_filter = f" AND folder IN ({placeholders})"

    pending_cur = db.cursor()
    pending_cur.execute(
        "SELECT COUNT(*) FROM actions WHERE status='pending'" + folder_filter,
        folder_params,
    )
    pending_total = pending_cur.fetchone()[0] or 0
    if pending_total == 0:
        logger.log("INFO", "execute_nothing", {"dry_run": dry_run}, console="ℹ️ No pending actions")
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    distinct_cur = db.cursor()
    distinct_cur.execute(
        "SELECT COUNT(DISTINCT uid) FROM actions WHERE status='pending'" + folder_filter,
        folder_params,
    )
    distinct_uids = distinct_cur.fetchone()[0] or 0
    suppressed = pending_total - distinct_uids

    # Order by effective priority first (ensures keywords execute before moves across all rules)
    # Then by folder/target for batch processing, then by creation order
    order_clause = "ORDER BY priority DESC, folder, target, created_at ASC, id ASC"

    # Deduplication: Keep only one occurrence of each unique action per UID
    # PARTITION BY (uid, folder, rule_name, action_type, target, action_data) allows:
    # - All actions from same rule to execute
    # - Different rules' actions to execute
    # - Only prevents identical duplicate actions in the same folder
    # - Includes folder to handle cases where emails are moved and re-matched
    dedup_cte = """
        WITH ranked AS (
            SELECT
                id,
                uid,
                folder,
                target,
                rule_name,
                priority,
                created_at,
                action_type,
                action_data,
                ROW_NUMBER() OVER (
                    PARTITION BY uid, folder, rule_name, action_type, target, action_data
                    ORDER BY created_at ASC, id ASC
                ) AS rn
            FROM actions
            WHERE status='pending'
    )
"""
    if folder_filter:
        dedup_cte = dedup_cte.replace(
            "WHERE status='pending'",
            f"WHERE status='pending'{folder_filter}",
        )
    selection_source = "ranked WHERE rn=1"
    limit_param: tuple[int, ...] = ()
    if limit is not None:
        dedup_cte += (
            "    , limited AS (\n"
            "        SELECT id, uid, folder, target, rule_name, priority, created_at, action_type, action_data\n"
            "        FROM ranked\n"
            "        WHERE rn=1\n"
            f"        {order_clause}\n"
            "        LIMIT ?\n"
            "    )\n"
        )
        selection_source = "limited"
        limit_param = (int(limit),)

    dedup_params = folder_params + limit_param

    if suppressed > 0:
        duplicates_cur = db.cursor()
        duplicates_cur.execute(
            dedup_cte
            + "SELECT id, uid, folder, target, rule_name, priority FROM ranked WHERE rn>1 ORDER BY uid",
            dedup_params,
        )
        while True:
            rows = duplicates_cur.fetchmany(256)
            if not rows:
                break
            updates: list[tuple[str, int]] = []
            timestamp = now_iso() if not dry_run else ""
            for a_id, uid, folder, target, rule_name, priority in rows:
                logger.log(
                    "INFO",
                    "duplicate_action_suppressed",
                    {"uid": uid, "rule": rule_name, "priority": priority, "target": target},
                )
                if not dry_run:
                    updates.append((timestamp, a_id))
            if updates:
                db.executemany(
                    "UPDATE actions SET status='suppressed', executed_at=? WHERE id=?",
                    updates,
                )
        if not dry_run:
            db.commit()

    group_counts_cur = db.cursor()
    group_counts_cur.execute(
        dedup_cte
        + f"SELECT folder, target, COUNT(*) FROM {selection_source} GROUP BY folder, target ORDER BY folder, target",
        dedup_params,
    )
    group_counts = group_counts_cur.fetchall()
    group_totals = {(folder, target): count for folder, target, count in group_counts}

    if verbose:
        groups_context = {
            f"{folder}→{target or '(no target)'}": count for (folder, target), count in group_totals.items()
        }
        lines = "\n".join(
            f"      • {pair}: {count}" for pair, count in sorted(groups_context.items())
        )
        logger.log(
            "INFO",
            "execute_overview",
            {"groups": groups_context, "dry_run": dry_run, "strict": strict},
            console=("📂 Execution plan:" + (f"\n{lines}" if lines else "")),
        )

    actions_total = sum(group_totals.values())
    actions_bar = tqdm(
        total=actions_total if actions_total > 0 else None,
        desc="⚙️ Executing actions",
        unit="action",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    folders_bar = tqdm(
        total=len(group_totals) if group_totals else None,
        desc="📦 Executing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=1,
        disable=not show_progress,
    )

    stats = {"done": 0, "skipped": 0, "failed": 0, "suppressed": suppressed}

    if verbose and suppressed:
        logger.log(
            "INFO",
            "execute_suppressed_duplicates",
            {"suppressed": suppressed},
            console=f"🚫 Suppressed {suppressed} duplicate actions",
        )

    message_id_cache: dict[tuple[str, str], str | None] = {}

    def _cached_message_id(folder: str, uid: str) -> str | None:
        key = (folder, uid)
        if key in message_id_cache:
            return message_id_cache[key]
        message_id: str | None = None
        try:
            row = db.execute(
                "SELECT data FROM headers WHERE folder=? AND uid=?",
                (folder, uid),
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            try:
                payload = json.loads(row[0])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                header_text = payload.get("header")
                if isinstance(header_text, str):
                    try:
                        parsed = HEADER_PARSER.parsestr(header_text)
                        if parsed is not None:
                            value = parsed.get("Message-ID") or parsed.get("Message-Id")
                            if isinstance(value, str) and value.strip():
                                message_id = value.strip()
                    except Exception:
                        # Fallback: extract Message-ID with regex for malformed headers
                        match = re.search(
                            r'Message-I[Dd]:\s*<?(\[?[^\]>\s]+\]?)>?',
                            header_text,
                            re.IGNORECASE | re.MULTILINE
                        )
                        if match:
                            message_id = match.group(1).strip()
        message_id_cache[key] = message_id
        return message_id

    def _message_id_criteria(message_id: str) -> str:
        escaped = message_id.replace('"', '\\"')
        return f'(UNDELETED HEADER Message-ID "{escaped}")'

    def _search_has_results(search_resp) -> bool:
        if not search_resp:
            return False
        first = search_resp[0]
        if isinstance(first, bytes):
            text = first.decode("ascii", "ignore")
        else:
            text = str(first)
        return bool(text.strip())

    def _uid_search_mailbox(
        mailbox: str,
        message_id: str,
        *,
        target: str | None,
        uid: str,
    ) -> tuple[bool, str, Iterable | None]:
        assert client is not None
        criteria = _message_id_criteria(message_id)
        search_typ, search_resp = client.uid("SEARCH", None, criteria)
        log_imap_call(
            "imap_uid_search",
            op_label="UID SEARCH",
            status=search_typ,
            response=search_resp,
            folder=mailbox,
            target=target,
            uid=uid,
        )
        if search_typ != "OK":
            return False, search_typ, search_resp
        return _search_has_results(search_resp), search_typ, search_resp

    def _verify_destination_mailbox(
        folder: str,
        target: str,
        message_id: str,
        *,
        uid: str,
    ) -> tuple[bool, str]:
        assert client is not None
        quoted_target = f'"{target}"'
        dest_selected = False
        sel_typ, sel_resp = client.select(quoted_target, readonly=True)
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{target}"',
            status=sel_typ,
            response=sel_resp,
            folder=target,
            target=target,
            uid=uid,
        )
        if sel_typ != "OK":
            rese_typ, rese_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=rese_typ,
                response=rese_resp,
                folder=folder,
                target=target,
                uid=uid,
            )
            if rese_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot re-open folder {folder}")
            return False, sel_typ
        dest_selected = True
        try:
            found, search_status, _ = _uid_search_mailbox(
                target, message_id, target=target, uid=uid
            )
        finally:
            if dest_selected:
                try:
                    client.close()
                except imaplib.IMAP4.error:
                    pass
            rese_typ, rese_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=rese_typ,
                response=rese_resp,
                folder=folder,
                target=target,
                uid=uid,
            )
            if rese_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot re-open folder {folder}")
        if search_status != "OK":
            return False, search_status
        return found, search_status

    select_cur = db.cursor()
    select_cur.execute(
        dedup_cte
        + "SELECT id, uid, folder, target, rule_name, priority, created_at, action_type, action_data "
        f"FROM {selection_source} "
        f"{order_clause}",
        dedup_params,
    )

    chunk_size = 512
    current_key: tuple[str, str | None, str] | None = None
    # Store: (action_id, uid, rule_name, action_type, action_data)
    current_items: list[tuple[int, str, str | None, str, str | None]] = []
    current_rule_name: str | None = None

    def _has_capability(name: str) -> bool:
        if dry_run or client is None:
            return False
        caps = getattr(client, "capabilities", ())
        desired = name.upper()
        for cap in caps:
            if isinstance(cap, bytes):
                cap_text = cap.decode("ascii", "ignore").upper()
            else:
                cap_text = str(cap).upper()
            if cap_text == desired:
                return True
        return False

    supports_uid_move = _has_capability("MOVE")

    def log_imap_call(
        message: str,
        *,
        op_label: str,
        status: str,
        response,
        folder: str,
        target: str | None = None,
        uid: str | None = None,
    ) -> None:
        if not verbose:
            return
        details = _format_imap_details(response)
        context: dict[str, str] = {"folder": folder, "status": status, "details": details}
        if target is not None:
            context["target"] = target
        if uid is not None:
            context["uid"] = uid
        suffix_parts: list[str] = []
        if uid is not None:
            suffix_parts.append(f"UID {uid}")
        if target is not None:
            suffix_parts.append(f"→ {target}")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        logger.log(
            "DEBUG",
            message,
            context,
            console=f"      ↪ {op_label} {status}{details}{suffix}",
        )

    def _execute_batch_set_keywords(
        folder: str,
        uid_keyword_map: dict[str, list[str]],
    ) -> dict[str, tuple[str, Any]]:
        """
        Batch set keywords on multiple messages with different keywords.

        Groups UIDs by keyword set and executes one STORE per keyword group,
        dramatically reducing IMAP operations.

        Args:
            folder: Folder containing messages
            uid_keyword_map: Dict mapping UID -> list of keywords

        Returns:
            Dict mapping UID -> (status, response)
        """
        assert client is not None
        results: dict[str, tuple[str, Any]] = {}

        # Select folder once for all operations
        sel_typ, sel_resp = client.select(f'"{folder}"')
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{folder}"',
            status=sel_typ,
            response=sel_resp,
            folder=folder,
        )
        if sel_typ != "OK":
            raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

        # Group UIDs by keyword set for efficient batching
        keyword_set_to_uids: dict[tuple, list[str]] = {}
        for uid, keywords in uid_keyword_map.items():
            key = tuple(sorted(keywords))
            if key not in keyword_set_to_uids:
                keyword_set_to_uids[key] = []
            keyword_set_to_uids[key].append(uid)

        # Execute one STORE per keyword set (batch operation)
        for keyword_tuple, uids in keyword_set_to_uids.items():
            keywords = list(keyword_tuple)
            flags_str = " ".join(keywords)
            uid_str = ",".join(uids)

            typ, resp = client.uid("STORE", uid_str, "+FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_batch_set",
                op_label="UID STORE +FLAGS (batch)",
                status=typ,
                response=resp,
                folder=folder,
            )

            # Record result for each UID
            for uid in uids:
                results[uid] = (typ, resp)

        return results

    def _execute_batch_remove_keywords(
        folder: str,
        uid_keyword_map: dict[str, list[str]],
    ) -> dict[str, tuple[str, Any]]:
        """
        Batch remove keywords from multiple messages with different keywords.

        Groups UIDs by keyword set and executes one STORE per keyword group,
        dramatically reducing IMAP operations.

        Args:
            folder: Folder containing messages
            uid_keyword_map: Dict mapping UID -> list of keywords to remove

        Returns:
            Dict mapping UID -> (status, response)
        """
        assert client is not None
        results: dict[str, tuple[str, Any]] = {}

        # Select folder once for all operations
        sel_typ, sel_resp = client.select(f'"{folder}"')
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{folder}"',
            status=sel_typ,
            response=sel_resp,
            folder=folder,
        )
        if sel_typ != "OK":
            raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

        # Group UIDs by keyword set for efficient batching
        keyword_set_to_uids: dict[tuple, list[str]] = {}
        for uid, keywords in uid_keyword_map.items():
            key = tuple(sorted(keywords))
            if key not in keyword_set_to_uids:
                keyword_set_to_uids[key] = []
            keyword_set_to_uids[key].append(uid)

        # Execute one STORE per keyword set (batch operation)
        for keyword_tuple, uids in keyword_set_to_uids.items():
            keywords = list(keyword_tuple)
            flags_str = " ".join(keywords)
            uid_str = ",".join(uids)

            typ, resp = client.uid("STORE", uid_str, "-FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_batch_remove",
                op_label="UID STORE -FLAGS (batch)",
                status=typ,
                response=resp,
                folder=folder,
            )

            # Record result for each UID
            for uid in uids:
                results[uid] = (typ, resp)

        return results

    def _execute_set_keywords(
        folder: str,
        uid: str,
        keywords: list[str],
    ) -> tuple[str, Any]:
        """
        Set (add) IMAP keywords/flags on a message.

        Args:
            folder: Source folder containing the message
            uid: Message UID
            keywords: List of keywords/flags to set

        Returns:
            Tuple of (status, response) from IMAP STORE command
        """
        assert client is not None
        try:
            # Select the folder
            sel_typ, sel_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=sel_typ,
                response=sel_resp,
                folder=folder,
                uid=uid,
            )
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            # Build flags string - ensure proper formatting
            flags_str = " ".join(keywords)

            # Execute STORE command to add keywords
            typ, resp = client.uid("STORE", uid, "+FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_set_keywords",
                op_label="UID STORE +FLAGS",
                status=typ,
                response=resp,
                folder=folder,
                uid=uid,
            )

            if typ == "OK":
                console_msg = f"   🏷️  Set keywords on {folder}/{uid}: {keywords}" if verbose else None
                logger.log(
                    "INFO",
                    "execute_set_keywords_done",
                    {"folder": folder, "uid": uid, "keywords": keywords},
                    console=console_msg,
                )
            else:
                logger.log(
                    "ERROR",
                    "execute_set_keywords_failed",
                    {
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "status": typ,
                        "details": _format_imap_details(resp),
                    },
                )

            return typ, resp

        except Exception as exc:
            logger.log(
                "ERROR",
                "execute_set_keywords_exception",
                {"folder": folder, "uid": uid, "keywords": keywords, "error": str(exc)},
            )
            raise

    def _execute_remove_keywords(
        folder: str,
        uid: str,
        keywords: list[str],
    ) -> tuple[str, Any]:
        """
        Remove IMAP keywords/flags from a message.

        Args:
            folder: Source folder containing the message
            uid: Message UID
            keywords: List of keywords/flags to remove

        Returns:
            Tuple of (status, response) from IMAP STORE command
        """
        assert client is not None
        try:
            # Select the folder
            sel_typ, sel_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=sel_typ,
                response=sel_resp,
                folder=folder,
                uid=uid,
            )
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            # Build flags string - ensure proper formatting
            flags_str = " ".join(keywords)

            # Execute STORE command to remove keywords
            typ, resp = client.uid("STORE", uid, "-FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_remove_keywords",
                op_label="UID STORE -FLAGS",
                status=typ,
                response=resp,
                folder=folder,
                uid=uid,
            )

            if typ == "OK":
                console_msg = f"   🏷️  Removed keywords from {folder}/{uid}: {keywords}" if verbose else None
                logger.log(
                    "INFO",
                    "execute_remove_keywords_done",
                    {"folder": folder, "uid": uid, "keywords": keywords},
                    console=console_msg,
                )
            else:
                logger.log(
                    "ERROR",
                    "execute_remove_keywords_failed",
                    {
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "status": typ,
                        "details": _format_imap_details(resp),
                    },
                )

            return typ, resp

        except Exception as exc:
            logger.log(
                "ERROR",
                "execute_remove_keywords_exception",
                {"folder": folder, "uid": uid, "keywords": keywords, "error": str(exc)},
            )
            raise

    def flush_group() -> None:
        nonlocal current_key, current_items, current_rule_name
        if current_key is None or not current_items:
            return

        folder, target, action_type = current_key
        display_target = target or "(no target)"
        total_for_group = group_totals.get(current_key, len(current_items))
        folders_bar.set_postfix_str(f"{folder} → {display_target}")

        # Extract metadata from first item
        # current_items: (action_id, uid, rule_name, action_type, action_data)
        rule_name = current_items[0][2] if current_items else None

        # Handle keyword actions (set_keywords, remove_keywords)
        if action_type in ("set_keywords", "remove_keywords"):
            if dry_run:
                for a_id, uid, _, _, action_data in current_items:
                    keywords = []
                    if action_data:
                        try:
                            data = json.loads(action_data)
                            keywords = data.get("keywords", [])
                        except json.JSONDecodeError:
                            pass

                    actions_bar.update(1)
                    if verbose:
                        logger.log(
                            "INFO",
                            "dry_action_preview",
                            {"folder": folder, "uid": uid, "action_type": action_type, "keywords": keywords},
                            console=f"   📝 Would {action_type} on {folder}/{uid}: {keywords}",
                        )
            else:
                assert client is not None

                # Build UID->keywords map for batching, validate keywords first
                uid_keyword_map: dict[str, list[str]] = {}
                items_to_skip: dict[int, str] = {}  # action_id -> reason

                for a_id, uid, _, _, action_data in current_items:
                    keywords = []
                    if action_data:
                        try:
                            data = json.loads(action_data)
                            keywords = data.get("keywords", [])
                        except json.JSONDecodeError:
                            logger.log(
                                "WARN",
                                "action_data_parse_failed",
                                {"folder": folder, "uid": uid, "action_id": a_id},
                            )
                            items_to_skip[a_id] = "Invalid action_data JSON"
                            continue

                    if not keywords:
                        logger.log(
                            "WARN",
                            f"{action_type}_empty",
                            {"folder": folder, "uid": uid},
                        )
                        items_to_skip[a_id] = "No keywords specified"
                        continue

                    uid_keyword_map[uid] = (a_id, keywords)

                # Process skipped items first
                for a_id, reason in items_to_skip.items():
                    db.execute(
                        "UPDATE actions SET status='skipped', executed_at=? WHERE id=?",
                        (now_iso(), a_id),
                    )
                    stats["skipped"] += 1
                    actions_bar.update(1)

                # Execute batch operation if there are items to process
                if uid_keyword_map:
                    try:
                        # Build map for batch execution (uid -> keywords only)
                        batch_map: dict[str, list[str]] = {
                            uid: keywords for uid, (_, keywords) in uid_keyword_map.items()
                        }

                        # Execute batch operation
                        if action_type == "set_keywords":
                            results = _execute_batch_set_keywords(folder, batch_map)
                        else:  # remove_keywords
                            results = _execute_batch_remove_keywords(folder, batch_map)

                        # Process results
                        for uid, (a_id, keywords) in uid_keyword_map.items():
                            typ, resp = results.get(uid, ("FAILED", None))

                            if typ == "OK":
                                db.execute(
                                    "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                                    (now_iso(), a_id),
                                )
                                stats["done"] += 1
                                if verbose:
                                    logger.log(
                                        "INFO",
                                        f"batch_{action_type}_done",
                                        {"folder": folder, "uid": uid, "keywords": keywords},
                                        console=f"   🏷️  Batch {action_type} on {folder}/{uid}: {keywords}",
                                    )
                            else:
                                db.execute(
                                    "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                    (now_iso(), a_id),
                                )
                                stats["failed"] += 1

                            actions_bar.update(1)

                    except Exception as exc:
                        # On batch error, mark all as failed
                        for uid, (a_id, keywords) in uid_keyword_map.items():
                            db.execute(
                                "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                (now_iso(), a_id),
                            )
                            stats["failed"] += 1
                            actions_bar.update(1)

                        logger.log(
                            "ERROR",
                            f"batch_{action_type}_exception",
                            {"folder": folder, "error": str(exc)},
                            console=f"❌ Batch {action_type} failed for {folder}: {exc}",
                        )

                db.commit()

            folders_bar.update(1)
            current_items = []
            current_key = None
            return

        # Handle move actions (original logic)
        uids = [uid for _, uid, _, _, _ in current_items]

        # Backup messages before moving (if requested and not in dry-run)
        backup_result: BackupResult | None = None
        if backup_moved and not dry_run and client is not None:
            assert backup_dir is not None
            backup_result = backup_messages(
                client=client,
                folder=folder,
                uids=uids,
                backup_dir=backup_dir,
                backup_type="pre_move",
                logger=logger,
                show_progress=show_progress,
                rule_name=rule_name,
                target_folder=target,
            )

            # Check if backup succeeded - if not, we should not proceed
            if backup_result.backed_up == 0 and len(uids) > 0:
                logger.log(
                    "ERROR",
                    "backup_failed_abort",
                    {"folder": folder, "target": target, "uid_count": len(uids)},
                    console=f"❌ Backup failed for {folder}. Skipping moves for safety.",
                )
                # Mark actions as failed
                for a_id, _, _, _, _ in current_items:
                    db.execute(
                        "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                        (now_iso(), a_id),
                    )
                db.commit()
                stats["failed"] += len(current_items)
                return

            if backup_result.failed > 0:
                logger.log(
                    "WARN",
                    "backup_partial",
                    {
                        "folder": folder,
                        "backed_up": backup_result.backed_up,
                        "failed": backup_result.failed,
                    },
                    console=f"⚠️  Partial backup: {backup_result.backed_up}/{len(uids)} succeeded",
                )

            # Track backup stats
            stats["backed_up"] = stats.get("backed_up", 0) + backup_result.backed_up
            stats["backup_failed"] = stats.get("backup_failed", 0) + backup_result.failed

        if dry_run:
            if show_progress:
                msgs_bar = tqdm(
                    total=total_for_group,
                    desc=f"   🚚 Moving {folder}",
                    unit="msg",
                    dynamic_ncols=True,
                    leave=False,
                    position=2,
                    disable=not show_progress,
                )
                msgs_bar.update(len(uids))
                msgs_bar.close()
            logger.log(
                "INFO",
                "dry_action_group",
                {"folder": folder, "target": target, "count": len(uids)},
                console=f"🧪 Dry run: {folder} → {display_target} ({len(uids)})",
            )
            for uid in uids:
                actions_bar.update(1)
                if verbose:
                    logger.log(
                        "INFO",
                        "dry_action_preview",
                        {"folder": folder, "target": target, "uid": uid},
                        console=f"   📝 Would move {folder}/{uid} → {display_target}",
                    )
        else:
            assert client is not None  # for type checkers
            msgs_bar = tqdm(
                total=total_for_group,
                desc=f"   🚚 Moving {folder}",
                unit="msg",
                dynamic_ncols=True,
                leave=False,
                position=2,
                disable=not show_progress,
            )
            try:
                sel_typ, sel_resp = client.select(f'"{folder}"')
                log_imap_call(
                    "imap_select",
                    op_label=f'SELECT "{folder}"',
                    status=sel_typ,
                    response=sel_resp,
                    folder=folder,
                )
                if sel_typ != "OK":
                    raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

                target_ready = target is None
                successful_moves: list[tuple[int, str]] = []  # Track (action_id, uid) for post-EXPUNGE verification

                def _record_success(action_id: int, uid_value: str) -> None:
                    """Mark action as successful and track for later verification."""
                    db.execute(
                        "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                        (now_iso(), action_id),
                    )
                    removed = db.execute(
                        "DELETE FROM headers WHERE folder=? AND uid=?",
                        (folder, uid_value),
                    ).rowcount
                    stats["done"] += 1

                    if verify_moves and target and not dry_run:
                        successful_moves.append((action_id, uid_value))

                    console_msg = f"   ✅ Moved {folder}/{uid_value} → {display_target}" if verbose else None
                    logger.log(
                        "INFO",
                        "execute_uid_done",
                        {"folder": folder, "target": target, "uid": uid_value},
                        console=console_msg,
                    )
                    if removed:
                        logger.log(
                            "INFO",
                            "execute_header_removed",
                            {"folder": folder, "uid": uid_value},
                            console=(
                                f"      🧹 Removed cached header for {folder}/{uid_value}"
                                if verbose
                                else None
                            ),
                        )

                def _verify_move(action_id: int, uid_value: str) -> None:
                    """Verify a successful move after EXPUNGE."""
                    message_id = _cached_message_id(folder, uid_value)
                    if not message_id:
                        logger.log(
                            "DEBUG",
                            "execute_verify_missing_message_id",
                            {
                                "folder": folder,
                                "target": target,
                                "uid": uid_value,
                            },
                        )
                        return

                    verify_errors: list[str] = []
                    source_found = False
                    source_status = "OK"

                    try:
                        # Ensure source folder is selected for verification
                        sel_typ, sel_resp = client.select(f'"{folder}"')
                        if sel_typ != "OK":
                            source_found = True
                            source_status = "ERROR"
                            verify_errors.append(f"Cannot select {folder}: {sel_typ}")
                        else:
                            source_found, source_status, _ = _uid_search_mailbox(
                                folder,
                                message_id,
                                target=target,
                                uid=uid_value,
                            )
                    except Exception as search_exc:
                        source_found = True
                        source_status = "ERROR"
                        verify_errors.append(str(search_exc))

                    dest_found = True
                    dest_status = "OK"

                    try:
                        dest_found, dest_status = _verify_destination_mailbox(
                            folder,
                            target,
                            message_id,
                            uid=uid_value,
                        )
                    except Exception as dest_exc:
                        dest_found = False
                        dest_status = "ERROR"
                        verify_errors.append(str(dest_exc))

                    issues: list[str] = []
                    if source_status != "OK":
                        issues.append("source_search_failed")
                    if source_found:
                        issues.append("source_present")
                    if dest_status != "OK":
                        issues.append("destination_search_failed")
                    if not dest_found:
                        issues.append("destination_missing")

                    if issues:
                        db.execute(
                            "UPDATE actions SET status=?, executed_at=? WHERE id=?",
                            ("failed", now_iso(), action_id),
                        )
                        stats["failed"] += 1
                        stats["done"] -= 1

                        context = {
                            "folder": folder,
                            "target": target,
                            "uid": uid_value,
                            "message_id": message_id,
                            "issues": issues,
                        }
                        if verify_errors:
                            context["errors"] = verify_errors

                        console_warn = None
                        if verbose:
                            console_warn = (
                                f"   ⚠️ Verification failed for {folder}/{uid_value} → {display_target}"
                            )
                        logger.log(
                            "WARN",
                            "execute_verify_failed",
                            context,
                            console=console_warn,
                        )

                for a_id, uid, _, _, _ in current_items:
                    # Guard: Skip if email is already in target folder
                    if target and folder == target:
                        db.execute(
                            "UPDATE actions SET status='skipped', executed_at=?, error_message=? WHERE id=?",
                            (now_iso(), 'Already in target folder', a_id),
                        )
                        stats["skipped"] += 1
                        actions_bar.update(1)
                        if verbose:
                            logger.log(
                                "INFO",
                                "skipped_redundant_move",
                                {"folder": folder, "uid": uid, "target": target},
                                console=f"      ⊘ {folder}/{uid} already in {target}",
                            )
                        continue

                    deleted_flagged = False
                    try:
                        move_typ = "NO"
                        move_resp = None
                        if supports_uid_move and target:
                            try:
                                move_typ, move_resp = client.uid(
                                    "MOVE", uid, f'"{target}"'
                                )
                            except Exception as move_exc:
                                log_imap_call(
                                    "imap_uid_move",
                                    op_label="UID MOVE",
                                    status="ERROR",
                                    response=[str(move_exc)],
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            else:
                                log_imap_call(
                                    "imap_uid_move",
                                    op_label="UID MOVE",
                                    status=move_typ,
                                    response=move_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                                if move_typ == "OK":
                                    if target and not target_ready:
                                        target_ready = True
                                    _record_success(a_id, uid)
                                    continue

                        typ1, copy_resp = client.uid("COPY", uid, f'"{target}"')
                        log_imap_call(
                            "imap_uid_copy",
                            op_label="UID COPY",
                            status=typ1,
                            response=copy_resp,
                            folder=folder,
                            target=target,
                            uid=uid,
                        )
                        if (
                            typ1 != "OK"
                            and target
                            and not target_ready
                            and _should_try_create_folder(copy_resp)
                        ):
                            if verbose:
                                logger.log(
                                    "WARN",
                                    "imap_copy_missing_target",
                                    {
                                        "folder": folder,
                                        "target": target,
                                        "uid": uid,
                                        "status": typ1,
                                        "details": _format_imap_details(copy_resp),
                                    },
                                    console=(
                                        f"      ↪ UID COPY retry required {typ1}{_format_imap_details(copy_resp)}"
                                        f" (UID {uid} → {target})"
                                    ),
                                )
                            create_typ, create_resp = client.create(f'"{target}"')
                            log_imap_call(
                                "imap_create",
                                op_label="CREATE",
                                status=create_typ,
                                response=create_resp,
                                folder=folder,
                                target=target,
                            )
                            if create_typ != "OK":
                                create_details = _format_imap_details(create_resp)
                                raise imaplib.IMAP4.error(
                                    f"CREATE {target} failed{create_details}"
                                )
                            target_ready = True
                            logger.log(
                                "INFO",
                                "create_missing_target",
                                {"folder": folder, "target": target},
                                console=f"   📁 Created missing folder {target}",
                            )
                            typ1, copy_resp = client.uid("COPY", uid, f'"{target}"')
                            log_imap_call(
                                "imap_uid_copy",
                                op_label="UID COPY",
                                status=typ1,
                                response=copy_resp,
                                folder=folder,
                                target=target,
                                uid=uid,
                            )

                        if typ1 != "OK":
                            details = _format_imap_details(copy_resp)
                            raise imaplib.IMAP4.error(f"UID COPY failed{details}")

                        if target and not target_ready:
                            target_ready = True

                        typ2, store_resp = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                        log_imap_call(
                            "imap_uid_store",
                            op_label="UID STORE +FLAGS",
                            status=typ2,
                            response=store_resp,
                            folder=folder,
                            target=target,
                            uid=uid,
                        )
                        if typ2 != "OK":
                            raise imaplib.IMAP4.error("UID STORE +FLAGS \\Deleted failed")
                        deleted_flagged = True

                        _record_success(a_id, uid)

                    except imaplib.IMAP4.error as exc:
                        if deleted_flagged:
                            try:
                                cleanup_typ, cleanup_resp = client.uid(
                                    "STORE", uid, "-FLAGS", "(\\Deleted)"
                                )
                                log_imap_call(
                                    "imap_uid_store_cleanup",
                                    op_label="UID STORE -FLAGS",
                                    status=cleanup_typ,
                                    response=cleanup_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            except Exception:  # pragma: no cover - best effort cleanup
                                pass
                        message = str(exc).lower()
                        if (
                            "no such message" in message
                            or "uid command error" in message
                            or "failed" in message
                        ):
                            if strict:
                                db.execute(
                                    "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                    (now_iso(), a_id),
                                )
                                db.commit()
                                logger.log(
                                    "ERROR",
                                    "message_missing_strict_abort",
                                    {
                                        "uid": uid,
                                        "folder": folder,
                                        "target": target,
                                        "error": str(exc),
                                    },
                                    console=f"💥 STRICT: missing UID {uid} in {folder} — aborting",
                                )
                                raise
                            db.execute(
                                "UPDATE actions SET status='skipped', executed_at=? WHERE id=?",
                                (now_iso(), a_id),
                            )
                            try:
                                db.execute(
                                    "DELETE FROM headers WHERE uid=? AND folder=?",
                                    (uid, folder),
                                )
                                logger.log("INFO", "cache_cleanup", {"uid": uid, "folder": folder})
                            except Exception as cleanup_exc:  # pragma: no cover - best effort cleanup
                                logger.log(
                                    "WARN",
                                    "cache_cleanup_failed",
                                    {"uid": uid, "folder": folder, "error": str(cleanup_exc)},
                                )
                            stats["skipped"] += 1
                            logger.log(
                                "WARN",
                                "message_missing_skipped",
                                {
                                    "uid": uid,
                                    "folder": folder,
                                    "target": target,
                                    "error": str(exc),
                                },
                                console=(
                                    f"   ⚠️ Skipped missing {folder}/{uid}: {exc}"
                                    if verbose
                                    else None
                                ),
                            )
                            continue

                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                            (now_iso(), a_id),
                        )
                        stats["failed"] += 1
                        logger.log(
                            "ERROR",
                            "execute_failed",
                            {"uid": uid, "folder": folder, "target": target, "error": str(exc)},
                            console=f"❌ {folder}/{uid}: {exc}",
                        )
                    except Exception:
                        if deleted_flagged:
                            try:
                                cleanup_typ, cleanup_resp = client.uid(
                                    "STORE", uid, "-FLAGS", "(\\Deleted)"
                                )
                                log_imap_call(
                                    "imap_uid_store_cleanup",
                                    op_label="UID STORE -FLAGS",
                                    status=cleanup_typ,
                                    response=cleanup_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            except Exception:  # pragma: no cover - best effort cleanup
                                pass
                        raise
                    finally:
                        msgs_bar.update(1)
                        actions_bar.update(1)

                try:
                    exp_typ, exp_resp = client.expunge()
                    log_imap_call(
                        "imap_expunge",
                        op_label="EXPUNGE",
                        status=exp_typ,
                        response=exp_resp,
                        folder=folder,
                        target=target,
                    )
                except Exception:  # pragma: no cover - best effort cleanup
                    pass

                # Verify all successful moves after EXPUNGE completes
                if successful_moves and verify_moves:
                    for action_id, uid_value in successful_moves:
                        try:
                            _verify_move(action_id, uid_value)
                        except Exception as verify_exc:  # pragma: no cover
                            logger.log(
                                "ERROR",
                                "execute_verify_exception",
                                {
                                    "action_id": action_id,
                                    "uid": uid_value,
                                    "error": str(verify_exc),
                                },
                                console=f"❌ Verification error for {folder}/{uid_value}: {verify_exc}",
                            )
                    db.commit()  # Commit any verification failures

                db.commit()
                logger.log(
                    "INFO",
                    "execute_folder_done",
                    {"folder": folder, "target": target, "moved": len(uids)},
                    console=f"✅ {folder}: handled {len(uids)} → {display_target}",
                )

            except Exception as exc:
                if strict:
                    raise
                logger.log(
                    "ERROR",
                    "execute_group_failed",
                    {"folder": folder, "target": target, "error": str(exc)},
                    console=f"💥 Group failed: {folder} → {target}: {exc}",
                )
            finally:
                msgs_bar.close()

        folders_bar.update(1)
        current_items = []
        current_key = None

    while True:
        rows = select_cur.fetchmany(chunk_size)
        if not rows:
            break
        for a_id, uid, folder, target, rule_name, _priority, _created_at, action_type, action_data in rows:
            # Group by (folder, target, action_type) to prevent mixing moves with keyword actions
            key = (folder, target, action_type)
            if current_key is not None and key != current_key:
                flush_group()
            if key != current_key:
                current_key = key
                current_rule_name = rule_name
            current_items.append((a_id, uid, rule_name, action_type, action_data))

    flush_group()
    folders_bar.close()
    actions_bar.close()

    # Backup all cached messages if requested (after moves complete)
    if backup_all and not dry_run and client is not None:
        assert backup_dir is not None
        logger.log(
            "INFO",
            "backup_all_start",
            console="\n💾 Backing up all cached messages...",
        )

        backup_results = backup_all_cached_messages(
            client=client,
            db=db,
            backup_dir=backup_dir,
            folders=list(folders) if folders else None,
            logger=logger,
            show_progress=show_progress,
        )

        total_backed_up = sum(r.backed_up for r in backup_results.values())
        total_failed = sum(r.failed for r in backup_results.values())
        stats["backed_up"] = stats.get("backed_up", 0) + total_backed_up
        stats["backup_failed"] = stats.get("backup_failed", 0) + total_failed

    timer.stop()
    timer.count = stats["done"]

    # Build summary message
    summary_parts = [
        "\n📊 Summary — Execute Actions\n",
        f"   📦  Actions executed: {stats['done']}\n",
        f"   ⚠️  Skipped (missing): {stats['skipped']}\n",
        f"   🚫  Suppressed (duplicates): {stats['suppressed']}\n",
        f"   💥  Failed: {stats['failed']}\n",
    ]

    # Add backup stats if applicable
    if "backed_up" in stats:
        summary_parts.append(f"   💾  Messages backed up: {stats['backed_up']}\n")
    if stats.get("backup_failed", 0) > 0:
        summary_parts.append(f"   ⚠️  Backup failures: {stats['backup_failed']}\n")

    summary_parts.extend([
        f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n",
        f"   {'🔒 STRICT' if strict else '✅ Completed'} {'(dry-run)' if dry_run else ''}\n",
    ])

    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "execute",
            **stats,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console="".join(summary_parts),
    )
    return timer, stats


def _count_unique_source_folders(db_path: Path) -> int:
    """Count unique source folders in pending actions."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT folder) FROM actions WHERE status = 'pending'")
        count = cur.fetchone()[0]
        return count if count else 0
    finally:
        conn.close()


def _precreate_target_folders(
    client: imaplib.IMAP4,
    db_path: Path,
    strict: bool = False,
    logger: JsonLogger | None = None,
) -> None:
    """
    Pre-create all target folders before parallel execution.

    This eliminates race conditions where multiple workers try to create
    the same target folder simultaneously.

    Args:
        client: Authenticated IMAP client
        db_path: Path to main SQLite database
        strict: Abort on any CREATE failure
        logger: JsonLogger for logging
    """
    if logger is None:
        logger = JsonLogger(Path("imapfilter.log"))

    # Query unique target folders from pending actions
    db = sqlite3.connect(str(db_path), timeout=10.0)
    cursor = db.cursor()
    cursor.execute("""
        SELECT DISTINCT target
        FROM actions
        WHERE status='pending' AND target IS NOT NULL
        ORDER BY target
    """)
    targets = [row[0] for row in cursor.fetchall()]
    db.close()

    if not targets:
        logger.log("INFO", "precreate_no_targets", console="ℹ️ No target folders to create")
        return

    logger.log(
        "INFO",
        "precreate_start",
        {"count": len(targets)},
        console=f"📁 Pre-creating {len(targets)} target folders...",
    )

    created_count = 0
    failed_count = 0

    for target in targets:
        try:
            # Check if folder exists with LIST
            quoted_target = _quote_mailbox(target)
            list_typ, list_resp = client.list('""', quoted_target)
            exists = list_typ == "OK" and list_resp and list_resp[0] is not None

            if not exists:
                # CREATE folder
                create_typ, create_resp = client.create(quoted_target)
                if create_typ == "OK":
                    created_count += 1
                    logger.log(
                        "INFO",
                        "precreate_success",
                        {"target": target},
                        console=f"   ✅ Created: {target}",
                    )
                else:
                    failed_count += 1
                    error_msg = _imap_response_text(create_resp)
                    logger.log(
                        "WARN" if not strict else "ERROR",
                        "precreate_failed",
                        {"target": target, "error": error_msg},
                        console=f"   ⚠️ Failed to create {target}: {error_msg}",
                    )
                    if strict:
                        raise imaplib.IMAP4.error(f"CREATE {target} failed: {error_msg}")
            else:
                logger.log(
                    "DEBUG",
                    "precreate_exists",
                    {"target": target},
                )

        except Exception as exc:
            failed_count += 1
            logger.log(
                "ERROR",
                "precreate_exception",
                {"target": target, "error": str(exc)},
                console=f"   ❌ Exception creating {target}: {exc}",
            )
            if strict:
                raise

    logger.log(
        "INFO",
        "precreate_complete",
        {"created": created_count, "failed": failed_count},
        console=f"📁 Pre-creation complete: {created_count} created, {failed_count} failed",
    )


def _merge_worker_databases(
    db_path: Path,
    temp_dir: Path,
    show_progress: bool = True,
    logger: JsonLogger | None = None,
) -> tuple[int, int]:
    """
    Merge per-worker temp databases into the main database.

    After all workers complete, this function:
    1. Discovers all temp databases (thread_*.db)
    2. For each temp DB:
       - ATTACH with retry logic
       - UPDATE main actions table with status/executed_at
       - DELETE from headers cache for moved messages
       - DETACH
    3. Single COMMIT after all merges
    4. Clean up temp files

    Args:
        db_path: Path to main SQLite database
        temp_dir: Directory containing temp databases
        show_progress: Show progress bar
        logger: JsonLogger for logging

    Returns:
        Tuple of (total_done, total_failed)
    """
    if logger is None:
        logger = JsonLogger(Path("imapfilter.log"))

    # Discover all temp databases
    thread_temp_dbs = sorted(temp_dir.glob("thread_*.db"))

    if not thread_temp_dbs:
        logger.log("INFO", "merge_no_databases", console="⚠️ No worker databases to merge")
        return 0, 0

    logger.log(
        "INFO",
        "merge_start",
        {"count": len(thread_temp_dbs)},
        console=f"🔄 Merging {len(thread_temp_dbs)} worker databases...",
    )

    merge_bar = tqdm(
        total=len(thread_temp_dbs),
        desc="🔄 Merging databases",
        position=0,
        unit="db",
        disable=not show_progress,
    )

    # Open main database for merge
    main_db = sqlite3.connect(str(db_path), timeout=30.0)
    main_db.execute("PRAGMA journal_mode=WAL")

    total_done = 0
    total_failed = 0

    for temp_idx, temp_db_path in enumerate(thread_temp_dbs):
        try:
            # Attach temp database with retry for lock issues
            max_attach_retries = 3
            for attach_attempt in range(max_attach_retries):
                try:
                    main_db.execute(f"ATTACH DATABASE '{temp_db_path}' AS temp_{temp_idx}")
                    break
                except sqlite3.OperationalError as lock_error:
                    if "database is locked" in str(lock_error) and attach_attempt < max_attach_retries - 1:
                        time.sleep(0.5)
                        continue
                    raise

            # Count done/failed actions
            cursor = main_db.execute(f"SELECT status, COUNT(*) FROM temp_{temp_idx}.actions GROUP BY status")
            for status, count in cursor.fetchall():
                if status == "done":
                    total_done += count
                elif status == "failed":
                    total_failed += count

            # Update actions table: status → done/failed
            main_db.execute(f"""
                UPDATE actions
                SET status = (
                    SELECT status FROM temp_{temp_idx}.actions WHERE temp_{temp_idx}.actions.id = actions.id
                ),
                executed_at = (
                    SELECT executed_at FROM temp_{temp_idx}.actions WHERE temp_{temp_idx}.actions.id = actions.id
                )
                WHERE id IN (SELECT id FROM temp_{temp_idx}.actions)
            """)

            # DELETE from headers cache (for moved messages)
            main_db.execute(f"""
                DELETE FROM headers
                WHERE (folder, uid) IN (
                    SELECT folder, uid FROM temp_{temp_idx}.deleted_headers
                )
            """)

            # DETACH DATABASE
            main_db.execute(f"DETACH DATABASE temp_{temp_idx}")

            merge_bar.update(1)

        except Exception as merge_error:
            logger.log(
                "ERROR",
                "merge_failed",
                {"thread_db": str(temp_db_path), "error": str(merge_error)},
                console=f"❌ Merge failed for {temp_db_path.name}: {merge_error}",
            )
            merge_bar.update(1)
            # Continue with other databases

    # Single COMMIT after all merges
    main_db.commit()
    main_db.close()

    merge_bar.close()

    # Clean up temp files
    for temp_db_path in thread_temp_dbs:
        try:
            temp_db_path.unlink()
        except Exception:
            pass

    logger.log(
        "INFO",
        "merge_complete",
        {"done": total_done, "failed": total_failed},
        console=f"✅ Merge complete: {total_done} done, {total_failed} failed",
    )

    return total_done, total_failed


def _execute_folder_worker(
    pool: IMAPConnectionPool,
    db_path: Path,
    temp_dir: Path,
    folder: str,
    actions: list[dict],
    worker_id: int,
    dry_run: bool = False,
    strict: bool = False,
    verify_moves: bool = False,
    backup_moved: bool = False,
    backup_all: bool = False,
    backup_dir: Path | None = None,
    logger: JsonLogger | None = None,
) -> tuple[str, int, int]:
    """
    Process all actions for a single source folder (runs in worker thread).

    Each worker:
    1. Creates its own temp SQLite database
    2. Acquires an IMAP connection from the pool
    3. Processes all actions for this folder
    4. Updates temp database with results
    5. Returns success/failure counts

    Args:
        pool: IMAP connection pool
        db_path: Path to main SQLite database
        temp_dir: Directory for temp databases
        folder: Source folder to process
        actions: List of action dicts for this folder
        worker_id: Worker ID for logging
        dry_run: Preview actions without executing
        strict: Abort on first error
        verify_moves: Verify moves after EXPUNGE
        backup_moved: Backup messages before moving
        backup_all: Backup all cached messages
        backup_dir: Directory for backups
        logger: JsonLogger for logging

    Returns:
        Tuple of (folder_name, actions_done, actions_failed)
    """
    if logger is None:
        logger = JsonLogger(Path("imapfilter.log"))

    # Generate thread-based temp DB path
    thread_id = threading.current_thread().ident
    temp_db_path = temp_dir / f"thread_{thread_id}.db"

    # Create temp SQLite DB with schema
    temp_db = None
    client = None
    actions_done = 0
    actions_failed = 0

    try:
        temp_db = sqlite3.connect(str(temp_db_path), timeout=5.0)
        temp_db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY,
                status TEXT,
                executed_at TEXT,
                error_message TEXT
            )
        """)
        temp_db.execute("""
            CREATE TABLE IF NOT EXISTS deleted_headers (
                folder TEXT,
                uid TEXT,
                PRIMARY KEY (folder, uid)
            )
        """)
        temp_db.commit()

        # Acquire IMAP connection from pool
        if not dry_run:
            client = pool.acquire()

        # Group actions by (folder, target) tuple for batch processing
        action_groups: dict[tuple[str, str | None], list[dict]] = {}
        for action in actions:
            key = (action["folder"], action["target"])
            if key not in action_groups:
                action_groups[key] = []
            action_groups[key].append(action)

        # Process each group
        for (grp_folder, target), group_actions in action_groups.items():
            # Handle dry-run mode
            if dry_run:
                for action in group_actions:
                    temp_db.execute(
                        "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                        (action["id"], "done", now_iso(), None),
                    )
                    actions_done += 1
                temp_db.commit()
                continue

            # Real execution
            try:
                # SELECT source folder (read-only=False for modifications)
                sel_typ, sel_resp = client.select(f'"{grp_folder}"', readonly=False)
                if sel_typ != "OK":
                    error_msg = f"Cannot open folder: {_imap_response_text(sel_resp)}"
                    for action in group_actions:
                        temp_db.execute(
                            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                            (action["id"], "failed", now_iso(), error_msg),
                        )
                        actions_failed += 1
                    temp_db.commit()
                    if strict:
                        raise imaplib.IMAP4.error(error_msg)
                    continue

                # Check if server supports UID MOVE
                supports_uid_move = hasattr(client, "capabilities") and b"MOVE" in getattr(client, "capabilities", ())

                # Separate keyword and move actions for batch processing
                keyword_actions_set = [a for a in group_actions if a.get("action_type") == "set_keywords"]
                keyword_actions_remove = [a for a in group_actions if a.get("action_type") == "remove_keywords"]
                move_actions = [a for a in group_actions if a.get("action_type", "move") == "move"]

                # Process keyword actions in batches
                if keyword_actions_set:
                    batch_done, batch_failed = _perform_batch_keyword_operations(
                        client=client,
                        temp_db=temp_db,
                        folder=grp_folder,
                        actions=keyword_actions_set,
                        action_type="set_keywords",
                        logger=logger,
                        verbose=False,
                    )
                    actions_done += batch_done
                    actions_failed += batch_failed

                if keyword_actions_remove:
                    batch_done, batch_failed = _perform_batch_keyword_operations(
                        client=client,
                        temp_db=temp_db,
                        folder=grp_folder,
                        actions=keyword_actions_remove,
                        action_type="remove_keywords",
                        logger=logger,
                        verbose=False,
                    )
                    actions_done += batch_done
                    actions_failed += batch_failed

                # Collect successful move actions for verification
                successful_moves: list[tuple[dict, str | None]] = []

                # Process move actions individually (they need selective logic)
                for action in move_actions:
                    uid = action["uid"]
                    action_type = action.get("action_type", "move")

                    try:
                        status, error_msg = _perform_move_operation(
                            client=client,
                            temp_db=temp_db,
                            action=action,
                            verify_moves=False,  # Verification done after EXPUNGE
                            backup_moved=backup_moved,
                            backup_dir=backup_dir,
                            dry_run=False,  # Real execution
                            supports_uid_move=supports_uid_move,
                            logger=logger,
                            verbose=False,  # Avoid excessive logging in worker
                        )

                        # Update action with INSERT OR REPLACE to ensure it's in temp DB
                        temp_db.execute(
                            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                            (action["id"], status, now_iso(), error_msg if error_msg else None),
                        )

                        if status == "done":
                            actions_done += 1
                            # Track for verification if needed
                            if verify_moves and target:
                                # Get Message-ID from main database if available
                                message_id = None
                                try:
                                    main_db = sqlite3.connect(str(db_path), timeout=5.0)
                                    row = main_db.execute(
                                        "SELECT data FROM headers WHERE folder=? AND uid=?",
                                        (grp_folder, uid)
                                    ).fetchone()
                                    main_db.close()
                                    if row and row[0]:
                                        data = json.loads(row[0])
                                        header_text = data.get("header", "")
                                        # Extract Message-ID with regex
                                        match = re.search(
                                            r'Message-I[Dd]:\s*<?(\[?[^\]>\s]+\]?)>?',
                                            header_text,
                                            re.IGNORECASE | re.MULTILINE
                                        )
                                        if match:
                                            message_id = match.group(1).strip()
                                except Exception:
                                    pass
                                successful_moves.append((action, message_id))
                        else:
                            actions_failed += 1

                    except Exception as exc:
                        error_msg = str(exc)
                        temp_db.execute(
                            "INSERT OR REPLACE INTO actions (id, status, executed_at, error_message) VALUES (?, ?, ?, ?)",
                            (action["id"], "failed", now_iso(), error_msg),
                        )
                        actions_failed += 1
                        if strict:
                            raise

                # EXPUNGE folder (delete flagged messages)
                if not dry_run:
                    try:
                        client.expunge()
                    except Exception:
                        pass  # Best effort

                # Verify successful moves (after EXPUNGE)
                if verify_moves and successful_moves and not dry_run:
                    for action, message_id in successful_moves:
                        try:
                            _verify_move_operation(
                                client=client,
                                temp_db=temp_db,
                                action=action,
                                message_id=message_id,
                                logger=logger,
                                verbose=False,  # Avoid excessive logging in worker
                            )
                        except Exception as verify_exc:
                            # Log verification error but don't fail
                            if logger:
                                logger.log(
                                    "WARN",
                                    "parallel_verify_exception",
                                    {
                                        "folder": action["folder"],
                                        "uid": action["uid"],
                                        "error": str(verify_exc),
                                    },
                                )

                temp_db.commit()

            except Exception as exc:
                logger.log(
                    "ERROR",
                    "execute_folder_group_failed",
                    {"folder": grp_folder, "target": target, "error": str(exc)},
                )
                if strict:
                    raise

        temp_db.commit()

    except Exception as exc:
        logger.log(
            "ERROR",
            "execute_folder_worker_exception",
            {"folder": folder, "worker_id": worker_id, "error": str(exc)},
        )
        if strict:
            raise

    finally:
        if temp_db:
            try:
                temp_db.close()
            except Exception:
                pass
        if client and not dry_run:
            try:
                pool.release(client)
            except Exception:
                pass

    return folder, actions_done, actions_failed


def execute_actions_parallel(
    secrets_path: Path,
    db_path: Path,
    *,
    show_progress: bool,
    dry_run: bool,
    strict: bool,
    logger: JsonLogger,
    verbose: bool = False,
    limit: int | None = None,
    folders: Sequence[str] | None = None,
    verify_moves: bool = False,
    backup_moved: bool = False,
    backup_all: bool = False,
    backup_dir: Path | None = None,
    max_workers: int = 5,
) -> tuple[PhaseTimer, Dict[str, int]]:
    """
    Execute pending actions in parallel (one worker per source folder).

    This is the main entry point for parallel execution. It coordinates:
    1. Pre-creating all target folders (eliminates race conditions)
    2. Grouping actions by source folder
    3. Spawning worker threads to process each folder
    4. Merging per-thread temp databases into the main database

    Args:
        secrets_path: Path to IMAP secrets file
        db_path: Path to SQLite database
        show_progress: Whether to show progress bars
        dry_run: If True, simulate execution without IMAP operations
        strict: If True, abort on first error
        logger: Logger instance for structured logging
        verbose: If True, show detailed per-message progress
        limit: Optional limit on number of actions to process
        folders: Optional list of folders to process
        verify_moves: If True, verify moves with Message-ID searches
        backup_moved: If True, backup messages before moving
        backup_all: If True, backup all messages after execution
        backup_dir: Directory for backups
        max_workers: Number of parallel workers (default: 5)

    Returns:
        Tuple of (timer, stats_dict)
    """
    timer = PhaseTimer("execute_parallel")

    # Validate backup parameters
    if (backup_moved or backup_all) and backup_dir is None:
        raise ValueError("backup_dir must be specified when backup is enabled")

    if backup_moved and backup_all:
        logger.log(
            "WARN",
            "backup_both_enabled",
            console="⚠️  Both --backup-moved and --backup-all specified. Using --backup-all.",
        )
        backup_moved = False

    logger.log(
        "INFO",
        "execute_parallel_start",
        {"log_file": str(logger.log_file)},
        console=f"📝 Detailed logs: {logger.log_file}",
    )

    # Open database to count pending actions
    db = sqlite3.connect(str(db_path), timeout=30.0)

    folder_params = tuple(folders) if folders else ()
    folder_filter = ""
    if folder_params:
        placeholders = ",".join("?" for _ in folder_params)
        folder_filter = f" AND folder IN ({placeholders})"

    # Count pending actions
    pending_cur = db.cursor()
    pending_cur.execute(
        "SELECT COUNT(*) FROM actions WHERE status='pending'" + folder_filter,
        folder_params,
    )
    pending_total = pending_cur.fetchone()[0] or 0

    if pending_total == 0:
        db.close()
        logger.log("INFO", "execute_nothing", {"dry_run": dry_run}, console="ℹ️ No pending actions")
        timer.stop()
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    logger.log(
        "INFO",
        "execute_pending_count",
        {"count": pending_total},
        console=f"📊 Pending actions: {pending_total}",
    )

    # Pre-create all target folders (before parallel execution)
    if not dry_run:
        from core.imap_client import imap_login
        client = imap_login(secrets_path, logger)
        try:
            _precreate_target_folders(
                client=client,
                db_path=db_path,
                strict=strict,
                logger=logger,
            )
        finally:
            client.logout()

    # Group actions by source folder
    query = """
        WITH ranked AS (
            SELECT
                id, uid, folder, target, rule_name, priority, action_type, action_data,
                ROW_NUMBER() OVER (
                    PARTITION BY uid
                    ORDER BY priority DESC, created_at ASC, id ASC
                ) AS rn
            FROM actions
            WHERE status='pending'
    """
    if folder_filter:
        query = query.replace(
            "WHERE status='pending'",
            f"WHERE status='pending'{folder_filter}",
        )
    query += """
        )
        SELECT folder, id, uid, target, rule_name, priority, action_type, action_data
        FROM ranked
        WHERE rn=1
        ORDER BY folder, target, priority DESC, id ASC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"

    cursor = db.cursor()
    cursor.execute(query, folder_params)

    # Group actions by source folder
    folder_actions: dict[str, list[dict]] = {}
    for folder, action_id, uid, target, rule_name, priority, action_type, action_data in cursor.fetchall():
        if folder not in folder_actions:
            folder_actions[folder] = []
        folder_actions[folder].append({
            "id": action_id,
            "uid": uid,
            "folder": folder,
            "target": target,
            "rule_name": rule_name,
            "priority": priority,
            "action_type": action_type,
            "action_data": action_data,
        })

    db.close()

    if not folder_actions:
        logger.log("INFO", "execute_nothing_after_dedup", console="ℹ️ No actions after deduplication")
        timer.stop()
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    # Sort folders by action count (load balance: smallest first)
    sorted_folders = sorted(folder_actions.keys(), key=lambda f: len(folder_actions[f]))

    logger.log(
        "INFO",
        "execute_parallel_folders",
        {"folder_count": len(sorted_folders), "max_workers": max_workers},
        console=f"🔧 Processing {len(sorted_folders)} folders with {max_workers} workers",
    )

    # Create temp directory for worker databases
    temp_dir = Path(tempfile.gettempdir()) / f"imapfilter_actions_{os.getpid()}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Set up progress bar: main folder progress
    folders_bar = tqdm(
        total=len(sorted_folders),
        desc="📦 Processing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    # Create connection pool
    pool = IMAPConnectionPool(secrets_path, max_workers, logger)

    total_done = 0
    total_failed = 0

    try:
        # Create ThreadPoolExecutor and spawn workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for folder in sorted_folders:
                actions = folder_actions[folder]
                worker_id = len(futures) % max_workers
                future = executor.submit(
                    _execute_folder_worker,
                    pool=pool,
                    db_path=db_path,
                    temp_dir=temp_dir,
                    folder=folder,
                    actions=actions,
                    worker_id=worker_id,
                    dry_run=dry_run,
                    strict=strict,
                    verify_moves=verify_moves,
                    backup_moved=backup_moved,
                    backup_all=backup_all,
                    backup_dir=backup_dir,
                    logger=logger,
                )
                futures[future] = folder

            # Track results as they complete
            for future in concurrent.futures.as_completed(futures):
                folder = futures[future]
                try:
                    folder_name, actions_done, actions_failed = future.result()
                    total_done += actions_done
                    total_failed += actions_failed
                    folders_bar.set_postfix_str(f"Last: {folder_name}")
                    folders_bar.update(1)
                except Exception as exc:
                    logger.log(
                        "ERROR",
                        "execute_folder_worker_failed",
                        {"folder": folder, "error": str(exc)},
                        console=f"❌ Worker failed for {folder}: {exc}",
                    )
                    folders_bar.update(1)
                    if strict:
                        # In strict mode, cancel remaining workers
                        for f in futures:
                            f.cancel()
                        raise

        folders_bar.close()

        # Garbage collection + delay
        gc.collect()
        time.sleep(1.0)

        # Merge worker databases
        merge_done, merge_failed = _merge_worker_databases(
            db_path=db_path,
            temp_dir=temp_dir,
            show_progress=show_progress,
            logger=logger,
        )

        # Update totals from merge (in case counts differ)
        total_done = merge_done
        total_failed = merge_failed

    finally:
        # Clean up
        pool.shutdown()
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    timer.stop()
    timer.count = total_done

    # Build stats dict compatible with existing code
    stats = {
        "done": total_done,
        "failed": total_failed,
        "skipped": 0,
        "suppressed": 0,
    }

    logger.log(
        "INFO",
        "execute_parallel_summary",
        {"done": total_done, "failed": total_failed, "elapsed": timer.elapsed},
        console=f"\n✅ Execution complete: {total_done} done, {total_failed} failed ({timer.fmt()})",
    )

    return timer, stats


def should_use_parallel_mode(
    db_path: Path,
    parallel_workers: int | None,
    logger: JsonLogger | None = None,
) -> bool:
    """
    Determine if parallel execution should be used.

    Rules:
    - parallel_workers == 0: Force sequential (return False)
    - parallel_workers > 0: Force parallel (return True)
    - parallel_workers is None: Auto-detect based on folder count
      - ≥5 unique source folders: Use parallel (return True)
      - <5 folders: Use sequential (return False)

    Args:
        db_path: Path to the SQLite database
        parallel_workers: Optional override for worker count (0=sequential, >0=parallel, None=auto)
        logger: Optional logger for diagnostic messages

    Returns:
        True if parallel mode should be used, False otherwise
    """
    if parallel_workers == 0:
        # Force sequential
        if logger:
            logger.log(
                "INFO",
                "parallel_disabled",
                {},
                console="📂 Using sequential execution (--parallel-workers 0)",
            )
        return False

    if parallel_workers and parallel_workers > 0:
        # Force parallel
        if logger:
            logger.log(
                "INFO",
                "parallel_forced",
                {"workers": parallel_workers},
                console=f"🚀 Using parallel execution ({parallel_workers} workers)",
            )
        return True

    # Auto-detect: count unique source folders in pending actions
    count = _count_unique_source_folders(db_path)
    if count >= 5:
        if logger:
            logger.log(
                "INFO",
                "parallel_auto",
                {"folders": count},
                console=f"🚀 Auto-detecting parallel mode: {count} folders found (≥5 threshold)",
            )
        return True
    else:
        if logger:
            logger.log(
                "INFO",
                "sequential_auto",
                {"folders": count},
                console=f"📂 Auto-detecting sequential mode: {count} folders (<5 threshold)",
            )
        return False
