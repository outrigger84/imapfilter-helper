"""Per-message and batched IMAP move/keyword operations (worker layer)."""
from __future__ import annotations

import datetime
import imaplib
import json
import sqlite3
from pathlib import Path
from typing import Callable

from core.logging_utils import JsonLogger, now_iso

from core.executor.helpers import (
    _encode_mailbox_utf7,
    _format_imap_details,
    _imap_response_text,
    _should_try_create_folder,
)

from core.executor.verification import _backup_message, _extract_message_id, _verify_move


# ============================================================================
# Phase 2: Action Type Support - Helper Functions for Parallel Execution
# ============================================================================


def _perform_move_operation(
    client: imaplib.IMAP4,
    main_db: sqlite3.Connection,
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
        main_db: Main database connection with 30-second busy timeout
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
    target = _encode_mailbox_utf7(action['target']) if action.get('target') else None

    # Guard: Skip if email is already in target folder
    if target and folder == target:
        main_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('skipped', now_iso(), 'Already in target folder', action_id)
        )
        main_db.commit()
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
        main_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('done', now_iso(), action_id)
        )
        main_db.commit()
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
                main_db.execute(
                    "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                    ('failed', now_iso(), error_msg, action_id)
                )
                main_db.commit()
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
                    main_db.execute(
                        "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
                        ('done', now_iso(), action_id)
                    )
                    main_db.execute(
                        "DELETE FROM headers WHERE folder = ? AND uid = ?",
                        (folder, uid)
                    )
                    main_db.commit()

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
                            main_db.execute(
                                "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                                ('failed', verify_error, action_id)
                            )
                            main_db.commit()
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
            main_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            main_db.commit()
            # Log failure for GOTIFY notification
            if logger:
                logger.log(
                    "WARN",
                    "imap_move_failed",
                    {"folder": folder, "target": target, "uid": uid, "error": error_msg}
                )
            return ('failed', error_msg)

        # Mark as deleted
        store_typ, store_resp = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        if store_typ != "OK":
            error_msg = f"STORE failed: {_format_imap_details(store_resp)}"
            main_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), error_msg, action_id)
            )
            main_db.commit()
            # Log failure for GOTIFY notification
            if logger:
                logger.log(
                    "WARN",
                    "imap_move_failed",
                    {"folder": folder, "target": target, "uid": uid, "error": error_msg}
                )
            return ('failed', error_msg)

        # Success - mark as done and track deleted message
        main_db.execute(
            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
            ('done', now_iso(), action_id)
        )
        main_db.execute(
            "DELETE FROM headers WHERE folder = ? AND uid = ?",
            (folder, uid)
        )
        main_db.commit()

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
                main_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', verify_error, action_id)
                )
                main_db.commit()
                if logger:
                    logger.log(
                        "ERROR",
                        "parallel_verify_failed",
                        {"folder": folder, "uid": uid, "target": target, "error": verify_error},
                        console=f"      ⚠️  Verification failed: {folder}/{uid} → {target}",
                    )
                    # Log failure for GOTIFY notification
                    logger.log(
                        "WARN",
                        "imap_move_failed",
                        {"folder": folder, "target": target, "uid": uid, "error": verify_error}
                    )
                return ('failed', verify_error or 'Verification failed')

        return ('done', '')

    except imaplib.IMAP4.error as exc:
        message = str(exc).lower()
        # Check for missing message errors
        if any(keyword in message for keyword in ["no such message", "uid command error", "failed"]):
            # Message likely already moved or doesn't exist - skip it
            main_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('skipped', now_iso(), str(exc), action_id)
            )
            main_db.execute(
                "DELETE FROM headers WHERE folder = ? AND uid = ?",
                (folder, uid)
            )
            main_db.commit()
            if logger:
                logger.log(
                    "WARN",
                    "parallel_message_missing",
                    {"folder": folder, "uid": uid, "error": str(exc)},
                )
            return ('skipped', str(exc))
        else:
            # Other IMAP error - mark as failed
            main_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ('failed', now_iso(), str(exc), action_id)
            )
            main_db.commit()
            return ('failed', str(exc))

    except Exception as exc:
        # Unexpected error - mark as failed
        error_msg = str(exc)
        main_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ('failed', now_iso(), error_msg, action_id)
        )
        main_db.commit()
        return ('failed', error_msg)


def _perform_batch_keyword_operations(
    client: imaplib.IMAP4,
    main_db: sqlite3.Connection,
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
        main_db: Main database connection with 30-second busy timeout
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
                main_db.execute(
                    "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                    ("failed", now_iso(), error_msg, action["id"]),
                )
                actions_failed += 1
            main_db.commit()
            return actions_done, actions_failed
    except Exception as exc:
        error_msg = str(exc)
        for action in actions:
            main_db.execute(
                "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                ("failed", now_iso(), error_msg, action["id"]),
            )
            actions_failed += 1
        main_db.commit()
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
        main_db.execute(
            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
            ("skipped", now_iso(), reason, action_id),
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
                        main_db.execute(
                            "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
                            ("done", now_iso(), action_id),
                        )
                        actions_done += 1
                    else:
                        error_msg = f"STORE {action_type} failed: {_format_imap_details(resp)}"
                        main_db.execute(
                            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                            ("failed", now_iso(), error_msg, action_id),
                        )
                        actions_failed += 1

            except Exception as exc:
                error_msg = str(exc)
                for uid, action_id in uid_action_pairs:
                    main_db.execute(
                        "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                        ("failed", now_iso(), error_msg, action_id),
                    )
                    actions_failed += 1

    main_db.commit()
    return actions_done, actions_failed


def _perform_batch_move_operations(
    client: imaplib.IMAP4,
    main_db: sqlite3.Connection,
    folder: str,
    target: str | None,
    actions: list[dict],
    *,
    supports_uid_move: bool,
    backup_moved: bool = False,
    backup_dir: Path | None = None,
    logger: JsonLogger | None = None,
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[int, int, list[tuple[dict, str | None]]]:
    """
    Move all UIDs in *actions* from *folder* to *target* with O(1) IMAP round-trips.

    The caller must have already SELECT-ed *folder*.  Returns
    (done_count, failed_count, successful_moves) where each successful_moves entry
    is (action, None) — message_id is omitted because _verify_move_operation skips
    verification when message_id is None, which is acceptable for batch moves.

    Falls back to per-message _perform_move_operation when backup is enabled or
    when the batch IMAP command fails.
    """
    _BATCH_SIZE = 500  # stay within ~8 KB IMAP command line limit

    if not actions:
        return 0, 0, []

    # When there is no target or backup is needed, fall back to per-message so
    # _perform_move_operation can fetch and save each body before deletion.
    if not target or (backup_moved and backup_dir is not None):
        done = failed = 0
        successful_moves: list[tuple[dict, str | None]] = []
        for action in actions:
            status, _ = _perform_move_operation(
                client=client,
                main_db=main_db,
                action=action,
                verify_moves=False,
                backup_moved=backup_moved,
                backup_dir=backup_dir,
                dry_run=False,
                supports_uid_move=supports_uid_move,
                logger=logger,
                verbose=False,
            )
            if status == "done":
                done += 1
                successful_moves.append((action, None))
            else:
                failed += 1
        return done, failed, successful_moves

    encoded_target = _encode_mailbox_utf7(target)

    # Filter out same-folder no-ops before building the UID set.
    to_move: list[dict] = []
    for action in actions:
        if action["folder"] == target:
            main_db.execute(
                "UPDATE actions SET status='skipped', executed_at=?, error_message=? WHERE id=?",
                (now_iso(), "Already in target folder", action["id"]),
            )
        else:
            to_move.append(action)
    if not to_move:
        main_db.commit()
        return 0, 0, []

    done = failed = 0
    successful_moves = []
    fallback_actions: list[dict] = []
    store_retry_actions: list[dict] = []
    chunks = [to_move[i : i + _BATCH_SIZE] for i in range(0, len(to_move), _BATCH_SIZE)]
    total = len(to_move)
    _LOG_INTERVAL = 5000

    for chunk in chunks:
        uid_set = ",".join(a["uid"] for a in chunk if a.get("uid"))
        if not uid_set:
            continue

        chunk_ok = False
        chunk_copied = False  # COPY landed in target; only the STORE may still be owed
        try:
            if supports_uid_move:
                b_typ, _ = client.uid("MOVE", uid_set, f'"{encoded_target}"')
                chunk_ok = b_typ == "OK"
            else:
                b_typ, _ = client.uid("COPY", uid_set, f'"{encoded_target}"')
                if b_typ == "OK":
                    chunk_copied = True
                    s_typ, _ = client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                    chunk_ok = s_typ == "OK"
        except imaplib.IMAP4.error as exc:
            if logger:
                logger.log(
                    "WARN",
                    "worker_batch_move_failed",
                    {"folder": folder, "target": target, "uid_count": len(chunk), "error": str(exc)},
                )

        chunk_size = len(chunk)
        if chunk_ok:
            ts = now_iso()
            for action in chunk:
                main_db.execute(
                    "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                    (ts, action["id"]),
                )
                main_db.execute(
                    "DELETE FROM headers WHERE folder=? AND uid=?",
                    (folder, action["uid"]),
                )
                successful_moves.append((action, None))
            main_db.commit()
            prev_done = done
            done += chunk_size
            if logger and done // _LOG_INTERVAL != prev_done // _LOG_INTERVAL:
                logger.log(
                    "INFO",
                    "worker_batch_move_progress",
                    {"folder": folder, "target": target, "done": done, "total": total},
                    console=f"  ↳ {folder} → {target}: {done:,}/{total:,} moved",
                )
        elif chunk_copied:
            # COPY succeeded but STORE +\Deleted failed. Re-running the full
            # move would COPY again and duplicate the messages in the target,
            # so these only get the STORE retried below.
            store_retry_actions.extend(chunk)
        else:
            fallback_actions.extend(chunk)

        if progress_callback is not None:
            progress_callback(chunk_size)

    # Per-message STORE retry for chunks that were copied but not flagged.
    for action in store_retry_actions:
        store_ok = False
        store_error = ""
        try:
            s_typ, s_resp = client.uid("STORE", action["uid"], "+FLAGS", "(\\Deleted)")
            store_ok = s_typ == "OK"
            if not store_ok:
                store_error = _imap_response_text(s_resp)
        except imaplib.IMAP4.error as exc:
            store_error = str(exc)

        if store_ok:
            main_db.execute(
                "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                (now_iso(), action["id"]),
            )
            main_db.execute(
                "DELETE FROM headers WHERE folder=? AND uid=?",
                (folder, action["uid"]),
            )
            successful_moves.append((action, None))
            done += 1
        else:
            error_msg = (
                f"Copied to {target} but STORE \\Deleted failed"
                f"{': ' + store_error if store_error else ''} — "
                "message now exists in both source and target"
            )
            main_db.execute(
                "UPDATE actions SET status='failed', executed_at=?, error_message=? WHERE id=?",
                (now_iso(), error_msg, action["id"]),
            )
            failed += 1
            if logger:
                logger.log(
                    "ERROR",
                    "worker_move_store_retry_failed",
                    {"folder": folder, "target": target, "uid": action["uid"], "error": store_error},
                )
    if store_retry_actions:
        main_db.commit()

    # Per-message fallback for any chunks the batch command rejected.
    for action in fallback_actions:
        status, _ = _perform_move_operation(
            client=client,
            main_db=main_db,
            action=action,
            verify_moves=False,
            backup_moved=False,
            backup_dir=None,
            dry_run=False,
            supports_uid_move=supports_uid_move,
            logger=logger,
            verbose=False,
        )
        if status == "done":
            done += 1
            successful_moves.append((action, None))
        else:
            failed += 1
        if progress_callback is not None:
            progress_callback(1)

    return done, failed, successful_moves


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
    main_db: sqlite3.Connection,
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
        main_db: Main database connection with 30-second busy timeout
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
    target = _encode_mailbox_utf7(action['target']) if action.get('target') else None

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
                main_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', 'Verification failed: message still in source', action_id)
                )
                main_db.commit()
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
                main_db.execute(
                    "UPDATE actions SET status = ?, error_message = ? WHERE id = ?",
                    ('failed', 'Verification failed: message not in target', action_id)
                )
                main_db.commit()
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


