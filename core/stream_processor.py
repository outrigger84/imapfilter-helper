"""Stream-based message processing for IMAPFilter."""
from __future__ import annotations

import imaplib
from typing import Generator, NamedTuple

from core.logging_utils import JsonLogger
from core.imap_client import safe_search_all
from core.stream_resume import ResumeLog


class StreamMessage(NamedTuple):
    """A single message from IMAP stream."""
    folder: str
    uid: str
    header_text: str


def _coalesce_fetch_payload(msg_data) -> bytes:
    """Extract header bytes from FETCH response."""
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


def stream_messages(
    client: imaplib.IMAP4,
    folders: list[str],
    *,
    logger: JsonLogger,
    limit: int | None = None,
    resume_log: ResumeLog | None = None,
) -> Generator[StreamMessage, None, None]:
    """
    Yield messages one at a time from IMAP folders.

    Args:
        client: IMAP connection
        folders: List of folder names to process
        logger: JsonLogger for logging
        limit: Maximum messages to process per folder (None = all)
        resume_log: ResumeLog instance for skipping already-processed messages

    Yields:
        StreamMessage tuples of (folder, uid, header_text)
    """
    msg_count = 0
    skipped_count = 0

    for folder in folders:
        try:
            logger.log("INFO", "stream_folder_start", {"folder": folder})

            # Select folder in read-only mode
            sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                logger.log(
                    "INFO",
                    "stream_folder_skipped",
                    {"folder": folder},
                    console=f"⚠️ Skipped {folder}",
                )
                continue

            # Get all non-deleted UIDs in folder
            uids = safe_search_all(client, undeleted_only=True)
            if not uids:
                logger.log(
                    "INFO",
                    "stream_folder_empty",
                    {"folder": folder},
                    console=f"📂 {folder}: empty",
                )
                continue

            # Apply limit if specified
            uids_to_process = list(uids)
            if limit is not None and limit > 0 and len(uids_to_process) > limit:
                uids_to_process = uids_to_process[-limit:]  # Most recent messages
                logger.log(
                    "INFO",
                    "stream_folder_limited",
                    {
                        "folder": folder,
                        "total": len(uids),
                        "limited": len(uids_to_process),
                    },
                    console=f"⚖️ {folder}: limited to {len(uids_to_process)} of {len(uids)} messages",
                )

            # Filter out already-processed messages if resuming
            if resume_log:
                uids_to_process = [
                    uid for uid in uids_to_process
                    if not resume_log.is_processed(folder, uid)
                ]
                if resume_log.is_processed(folder, uids_to_process[0] if uids_to_process else ""):
                    skipped_count += len(uids) - len(uids_to_process)
                    if skipped_count > 0 and len(uids_to_process) > 0:
                        logger.log(
                            "INFO",
                            "stream_folder_resumed",
                            {
                                "folder": folder,
                                "skipped": skipped_count,
                                "remaining": len(uids_to_process),
                            },
                            console=f"⏭️ {folder}: skipping {skipped_count} already-processed messages",
                        )

            logger.log(
                "INFO",
                "stream_folder_processing",
                {"folder": folder, "messages": len(uids_to_process)},
                console=f"📂 {folder}: processing {len(uids_to_process)} messages",
            )

            # Stream each message
            for uid in uids_to_process:
                uid_value = (
                    uid.decode("ascii", "ignore")
                    if isinstance(uid, (bytes, bytearray))
                    else str(uid)
                )
                if not uid_value:
                    continue

                try:
                    typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER])")
                    if typ != "OK":
                        logger.log(
                            "WARN",
                            "stream_message_fetch_failed",
                            {"folder": folder, "uid": uid_value},
                        )
                        continue

                    raw_hdr = _coalesce_fetch_payload(msg_data)
                    if not raw_hdr:
                        logger.log(
                            "WARN",
                            "stream_message_empty_header",
                            {"folder": folder, "uid": uid_value},
                        )
                        continue

                    header_text = raw_hdr.decode(errors="ignore")
                    yield StreamMessage(folder=folder, uid=uid_value, header_text=header_text)
                    msg_count += 1

                except Exception as exc:
                    logger.log(
                        "WARN",
                        "stream_message_error",
                        {"folder": folder, "uid": uid_value, "error": str(exc)},
                    )
                    continue

            logger.log(
                "INFO",
                "stream_folder_done",
                {"folder": folder, "messages": len(uids_to_process)},
                console=f"✅ {folder}: {len(uids_to_process)} messages processed",
            )

        except Exception as exc:
            logger.log(
                "ERROR",
                "stream_folder_exception",
                {"folder": folder, "error": str(exc)},
                console=f"❌ {folder}: {exc}",
            )
            continue

    logger.log(
        "INFO",
        "stream_complete",
        {"total_messages": msg_count},
        console=f"🏁 Streaming complete: {msg_count} messages",
    )
