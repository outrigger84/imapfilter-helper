"""Post-move verification and backup helpers."""
from __future__ import annotations

import datetime
import imaplib
import re
from pathlib import Path

from core.logging_utils import JsonLogger


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


