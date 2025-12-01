"""Cache building helpers."""
from __future__ import annotations

import json
import re
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso
from core.imap_client import safe_search_all


VALID_LIMIT_ORDERS = {"newest", "oldest", "random"}


def _select_uids(
    uids: Sequence[bytes], limit: int | None, order: str
) -> tuple[list[bytes], str]:
    """Return the UIDs that should be cached based on the requested limit."""

    items = list(uids)
    if limit is None or limit <= 0 or limit >= len(items):
        return items, "newest" if order not in VALID_LIMIT_ORDERS else order

    normalized = order if order in VALID_LIMIT_ORDERS else "newest"

    if normalized == "oldest":
        return items[:limit], normalized
    if normalized == "random":
        return random.sample(items, k=limit), normalized
    return items[-limit:], normalized


def _coalesce_fetch_payload(msg_data) -> bytes:
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


def _parse_fetch_response(msg_data) -> tuple[bytes, list[str], str | None]:
    """
    Parse IMAP FETCH response to extract BODY[HEADER], FLAGS, and INTERNALDATE.

    Args:
        msg_data: Response from IMAP FETCH command

    Returns:
        Tuple of (header_bytes, flags_list, internaldate_string)
        - header_bytes: Raw email headers as bytes
        - flags_list: List of flag strings (e.g., ["\\Seen", "custom"])
        - internaldate_string: Date string or None if not found

    Example:
        >>> msg_data = [(b'1 (FLAGS (\\Seen) INTERNALDATE "28-Oct-2025 07:30:19 +0000"', b'headers...')]
        >>> headers, flags, date = _parse_fetch_response(msg_data)
        >>> flags
        ['\\Seen']
        >>> date
        '28-Oct-2025 07:30:19 +0000'
    """
    if not msg_data:
        return b"", [], None

    header_bytes = b""
    flags = []
    internaldate = None

    try:
        # IMAP FETCH response structure: list of tuples
        # First element: metadata (FLAGS, INTERNALDATE, etc.)
        # Second element: actual BODY[HEADER] data
        for item in msg_data:
            if isinstance(item, tuple) and len(item) >= 2:
                metadata = item[0] if isinstance(item[0], bytes) else b""
                payload = item[1] if isinstance(item[1], (bytes, bytearray)) else b""

                # Extract FLAGS from metadata
                flags_match = re.search(rb'FLAGS \(([^)]*)\)', metadata)
                if flags_match:
                    flags_str = flags_match.group(1).decode('ascii', 'ignore').strip()
                    if flags_str:
                        # Split by whitespace and filter empty strings
                        flags = [f for f in flags_str.split() if f]

                # Extract INTERNALDATE from metadata
                date_match = re.search(rb'INTERNALDATE "([^"]*)"', metadata)
                if date_match:
                    internaldate = date_match.group(1).decode('ascii', 'ignore')

                # Extract header payload
                if payload:
                    header_bytes = bytes(payload)

            elif isinstance(item, (bytes, bytearray)):
                # Sometimes metadata is a separate item
                metadata = bytes(item)

                flags_match = re.search(rb'FLAGS \(([^)]*)\)', metadata)
                if flags_match:
                    flags_str = flags_match.group(1).decode('ascii', 'ignore').strip()
                    if flags_str:
                        flags = [f for f in flags_str.split() if f]

                date_match = re.search(rb'INTERNALDATE "([^"]*)"', metadata)
                if date_match:
                    internaldate = date_match.group(1).decode('ascii', 'ignore')

    except Exception:
        # If parsing fails, return what we have (at minimum, header_bytes)
        # Don't raise - graceful degradation
        pass

    return header_bytes, flags, internaldate


def build_cache(
    client,
    db,
    folders: Sequence[str],
    *,
    show_progress: bool,
    logger: JsonLogger,
    limit: int | None,
    order: str,
) -> tuple[PhaseTimer, int, int]:
    timer = PhaseTimer("cache")

    if client is None:
        logger.log(
            "INFO",
            "cache_skipped",
            {"folders": len(folders), "reason": "dry-run"},
            console="ℹ️ Skipping cache build in dry-run mode",
        )
        timer.stop()
        timer.count = 0
        return timer, len(folders), 0

    folders_bar = tqdm(
        folders,
        desc="📂 Caching folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )
    total_msgs = 0

    for folder in folders_bar:
        folders_bar.set_postfix_str(folder)
        logger.log("INFO", "cache_folder_start", {"folder": folder})
        try:
            sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                logger.log("INFO", "cache_folder_skipped", {"folder": folder}, console=f"⚠️ Skipped {folder}")
                continue

            uids = safe_search_all(client, undeleted_only=True)
            if not uids:
                logger.log(
                    "INFO",
                    "cache_folder_empty",
                    {"folder": folder},
                    console=f"📂 {folder}: empty",
                )
                db.execute(
                    "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                    (folder, "/".join(folder.split("/")[:-1]), now_iso()),
                )
                db.commit()
                continue

            limited_uids, applied_order = _select_uids(uids, limit, order)
            if limit is not None and limit > 0 and len(limited_uids) < len(uids):
                logger.log(
                    "INFO",
                    "cache_folder_limited",
                    {
                        "folder": folder,
                        "requested_limit": limit,
                        "order": applied_order,
                        "total": len(uids),
                        "cached": len(limited_uids),
                    },
                    console=(
                        f"⚖️ {folder}: limited to {len(limited_uids)}"
                        f" of {len(uids)} messages"
                    ),
                )

            msgs_bar = tqdm(
                limited_uids,
                desc=f"   ✉️ Fetching {folder}",
                unit="msg",
                dynamic_ncols=True,
                leave=False,
                position=1,
                disable=not show_progress,
            )

            for uid in msgs_bar:
                uid_value = (
                    uid.decode("ascii", "ignore") if isinstance(uid, (bytes, bytearray)) else str(uid)
                )
                if not uid_value:
                    continue

                # Fetch BODY[HEADER], FLAGS, and INTERNALDATE
                typ, msg_data = client.uid("FETCH", uid_value, "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)")
                if typ != "OK":
                    logger.log(
                        "WARNING",
                        "cache_fetch_failed",
                        {"folder": folder, "uid": uid_value},
                    )
                    continue

                # Parse the FETCH response
                raw_hdr, flags, internaldate = _parse_fetch_response(msg_data)
                if not raw_hdr:
                    logger.log(
                        "WARNING",
                        "cache_parse_failed",
                        {"folder": folder, "uid": uid_value},
                    )
                    continue

                hdr_str = raw_hdr.decode(errors="ignore")

                # Build cache entry with FLAGS and INTERNALDATE
                cache_entry = {"header": hdr_str}
                if flags:
                    cache_entry["flags"] = flags
                if internaldate:
                    cache_entry["internaldate"] = internaldate

                db.execute(
                    "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
                    "VALUES(?,?,?,?)",
                    (
                        folder,
                        uid_value,
                        json.dumps(cache_entry),
                        now_iso(),
                    ),
                )

            db.execute(
                "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                (folder, "/".join(folder.split("/")[:-1]), now_iso()),
            )
            db.commit()
            total_msgs += len(limited_uids)
            logger.log(
                "INFO",
                "cache_folder_done",
                {"folder": folder, "messages": len(limited_uids)},
                console=f"✅ {folder}: {len(limited_uids)} messages cached",
            )

        except Exception as exc:  # pragma: no cover - defensive logging
            logger.log(
                "ERROR",
                "cache_folder_failed",
                {"folder": folder, "error": str(exc)},
                console=f"❌ {folder}: {exc}",
            )

    timer.stop()
    timer.count = total_msgs
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "cache",
            "folders": len(folders),
            "messages": total_msgs,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Build Cache\n"
            f"   🗂️  Folders processed: {len(folders)}\n"
            f"   ✉️  Messages cached: {total_msgs}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )
    return timer, len(folders), total_msgs


def compact_cache(db, *, logger: JsonLogger) -> tuple[PhaseTimer, int, int]:
    """Remove cached headers for messages that have already been handled."""

    timer = PhaseTimer("compact-cache")
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT DISTINCT h.folder, h.uid
        FROM headers h
        JOIN actions a ON a.folder=h.folder AND a.uid=h.uid
        WHERE a.status NOT IN ('pending', 'simulated')
        ORDER BY h.folder, h.uid
        """
    )
    stale_rows = cursor.fetchall()

    removed = 0
    with db:
        for folder, uid in stale_rows:
            removed += db.execute(
                "DELETE FROM headers WHERE folder=? AND uid=?",
                (folder, uid),
            ).rowcount

    timer.stop()
    timer.count = removed
    logger.log(
        "INFO",
        "cache_compacted",
        {"checked": len(stale_rows), "removed": removed},
        console=(
            "🧹 Compacted cache"
            f" — removed {removed} cached header{'s' if removed != 1 else ''}"
        ),
    )
    return timer, removed, len(stale_rows)
