"""Cache building helpers."""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from core.connection_pool import IMAPConnectionPool
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
    folder_sizes: dict[str, int] | None = None,
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
        # Display folder size in progress bar if available
        postfix = folder
        if folder_sizes and folder in folder_sizes:
            size = folder_sizes[folder]
            if size >= 0:
                postfix = f"{folder} ({size} msgs)"
        folders_bar.set_postfix_str(postfix)
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


def build_cache_parallel(
    secrets_path: Path,
    db_path: Path,
    folders: Sequence[str],
    *,
    show_progress: bool,
    logger: JsonLogger,
    limit: int | None,
    order: str,
    max_workers: int,
    folder_sizes: dict[str, int] | None = None,
) -> tuple[PhaseTimer, int, int]:
    """
    Build cache with parallel folder processing.

    Uses ThreadPoolExecutor with IMAP connection pool for concurrent folder processing.
    Each worker writes to its own temporary database to eliminate contention.
    Databases are merged at the end.
    Thread-safe progress tracking with locks.
    Soft error handling: continues on folder failures, with automatic retry.

    Args:
        secrets_path: Path to IMAP secrets file
        db_path: Path to cache database
        folders: List of folders to process (should be sorted by size)
        show_progress: Whether to show progress bars
        logger: JsonLogger for logging
        limit: Limit messages per folder
        order: Message order (newest/oldest/random)
        max_workers: Number of parallel IMAP connections
        folder_sizes: Optional dict of folder sizes for progress display

    Returns:
        Tuple of (timer, folder_count, message_count)
    """
    timer = PhaseTimer("cache")

    # Create temporary directory for worker databases
    temp_dir = Path(tempfile.gettempdir()) / f"imapfilter_cache_{os.getpid()}"
    temp_dir.mkdir(exist_ok=True)

    # Map worker_id → temp_db_path for per-worker isolation (no contention)
    worker_db_paths = {}
    for worker_id in range(max_workers):
        worker_db_paths[worker_id] = temp_dir / f"worker_{worker_id}.db"

    # Create connection pool with specified max workers
    pool = IMAPConnectionPool(secrets_path, max_workers, logger)

    # Thread-safe counters
    total_msgs_lock = threading.Lock()
    total_msgs_count = 0

    # Track failed folders for retry logic
    failed_folders: list[tuple[str, Exception]] = []
    failed_folders_lock = threading.Lock()

    # Thread-safe progress bar (tqdm is thread-safe for updates)
    folders_bar = tqdm(
        total=len(folders),
        desc="📂 Caching folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    # Per-worker progress bars for message-level visibility
    worker_bars = {}
    bars_lock = threading.Lock()

    def get_worker_bar(worker_id: int) -> tqdm:
        """Get or create a progress bar for a specific worker thread."""
        with bars_lock:
            if worker_id not in worker_bars:
                worker_bars[worker_id] = tqdm(
                    total=0,
                    desc=f"   Worker {worker_id}: Idle",
                    position=worker_id + 1,
                    leave=False,
                    unit="msg",
                    dynamic_ncols=True,
                    disable=not show_progress,
                )
            return worker_bars[worker_id]

    def process_single_folder(folder: str, worker_id: int, temp_db_path: Path) -> tuple[str, int, Exception | None]:
        """
        Process a single folder (runs in worker thread).

        Writes to worker's isolated temporary database (no contention).

        Returns tuple of (folder_name, message_count, error_or_none)
        """
        client = None
        try:
            # Acquire connection from pool
            client = pool.acquire()

            # Each worker writes to its own temporary database (no contention)
            db = sqlite3.connect(
                str(temp_db_path),
                timeout=5.0,            # Short timeout OK - no contention
                check_same_thread=False # Allow thread-safe usage
            )
            # Initialize schema if needed
            db.execute("CREATE TABLE IF NOT EXISTS headers (folder TEXT, uid TEXT, data TEXT, updated_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY, folder TEXT, parent TEXT, updated_at TEXT)")

            # Select folder in read-only mode
            sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                logger.log("INFO", "cache_folder_skipped", {"folder": folder}, console=f"⚠️ Skipped {folder}")
                folders_bar.update(1)
                return folder, 0, None

            # Get undeleted messages
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
                db.close()
                folders_bar.update(1)
                return folder, 0, None

            # Apply limit and order
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

            # Set up worker progress bar for this folder
            worker_bar = get_worker_bar(worker_id)
            worker_bar.reset(total=len(limited_uids))
            worker_bar.set_description(f"   Worker {worker_id}: {folder[:40]}")

            # Fetch and cache headers for each message
            # Batch commits every 50 messages to reduce transaction scope and lock contention
            batch_size = 50
            for idx, uid in enumerate(limited_uids):
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
                worker_bar.update(1)

                # Commit in batches to reduce transaction scope and lock contention
                if (idx + 1) % batch_size == 0:
                    db.commit()

            # Commit folder's cached messages
            db.execute(
                "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                (folder, "/".join(folder.split("/")[:-1]), now_iso()),
            )
            db.commit()
            db.close()

            # Mark worker as idle
            worker_bar.reset(0)
            worker_bar.set_description(f"   Worker {worker_id}: Idle")

            # Update progress bar (thread-safe)
            postfix = folder
            if folder_sizes and folder in folder_sizes:
                size = folder_sizes[folder]
                if size >= 0:
                    postfix = f"{folder} ({size} msgs)"
            folders_bar.set_postfix_str(postfix)
            folders_bar.update(1)

            logger.log(
                "INFO",
                "cache_folder_done",
                {"folder": folder, "messages": len(limited_uids)},
                console=f"✅ {folder}: {len(limited_uids)} messages cached",
            )

            return folder, len(limited_uids), None

        except Exception as exc:
            logger.log(
                "ERROR",
                "cache_folder_failed",
                {"folder": folder, "error": str(exc)},
                console=f"❌ {folder}: {exc}",
            )
            folders_bar.update(1)
            return folder, 0, exc

        finally:
            if client:
                pool.release(client)

    # Execute parallel processing with ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all folder processing tasks with round-robin worker assignment
        futures = {}
        for idx, folder in enumerate(folders):
            worker_id = idx % max_workers  # Round-robin assignment
            temp_db_path = worker_db_paths[worker_id]
            future = executor.submit(process_single_folder, folder, worker_id, temp_db_path)
            futures[future] = folder

        # Process results as they complete
        for future in concurrent.futures.as_completed(futures):
            folder, msg_count, error = future.result()
            with total_msgs_lock:
                total_msgs_count += msg_count
            # Track failures for retry logic
            if error is not None:
                with failed_folders_lock:
                    failed_folders.append((folder, error))

    # Clean up connection pool and worker progress bars
    pool.shutdown()
    for bar in worker_bars.values():
        bar.close()
    folders_bar.close()

    # Merge temp databases into main database
    logger.log("INFO", "merge_start", {}, console="🔄 Merging worker databases...")

    merge_bar = tqdm(
        total=max_workers,
        desc="🔄 Merging databases",
        position=0,
        unit="db",
        disable=not show_progress,
    )

    # Open main database for merge
    main_db = sqlite3.connect(str(db_path), timeout=5.0)
    main_db.execute("PRAGMA journal_mode=WAL")

    for worker_id, temp_db_path in worker_db_paths.items():
        if not temp_db_path.exists():
            merge_bar.update(1)
            continue

        try:
            # Attach temp database and copy data
            main_db.execute(f"ATTACH DATABASE '{temp_db_path}' AS worker_{worker_id}")

            # Copy headers
            main_db.execute(f"""
                INSERT OR REPLACE INTO headers (folder, uid, data, updated_at)
                SELECT folder, uid, data, updated_at FROM worker_{worker_id}.headers
            """)

            # Copy folders
            main_db.execute(f"""
                INSERT OR REPLACE INTO folders (folder, parent, updated_at)
                SELECT folder, parent, updated_at FROM worker_{worker_id}.folders
            """)

            main_db.execute(f"DETACH DATABASE worker_{worker_id}")
        except Exception as merge_error:
            logger.log(
                "ERROR",
                "merge_failed",
                {"worker_id": worker_id, "error": str(merge_error)},
                console=f"⚠️  Merge error for worker {worker_id}: {merge_error}",
            )

        merge_bar.update(1)

    main_db.commit()
    main_db.close()
    merge_bar.close()

    # Cleanup temp files and directory
    for temp_db_path in worker_db_paths.values():
        if temp_db_path.exists():
            try:
                temp_db_path.unlink()
            except Exception as cleanup_error:
                logger.log(
                    "WARNING",
                    "cleanup_failed",
                    {"path": str(temp_db_path), "error": str(cleanup_error)},
                )

    try:
        temp_dir.rmdir()
    except Exception:
        pass  # Directory may not be empty if other processes use temp dir

    logger.log("INFO", "merge_complete", {}, console="✅ Merge complete")

    # Retry logic for failed folders
    MAX_RETRIES = 2
    retry_delay = 5.0  # Start with 5 second delay

    for retry_attempt in range(MAX_RETRIES):
        if not failed_folders:
            break

        logger.log(
            "INFO",
            "retry_attempt",
            {"attempt": retry_attempt + 1, "folders": len(failed_folders)},
            console=f"🔄 Retry attempt {retry_attempt + 1}/{MAX_RETRIES} for {len(failed_folders)} failed folders",
        )

        # Wait before retrying (exponential backoff)
        if retry_attempt > 0:
            time.sleep(retry_delay)
            retry_delay *= 2  # Double delay for next attempt

        # Copy and clear failed list
        to_retry = failed_folders.copy()
        failed_folders.clear()

        # Create new progress bar for retries
        retry_bar = tqdm(
            total=len(to_retry),
            desc=f"🔄 Retry attempt {retry_attempt + 1}",
            position=0,
            unit="folder",
            disable=not show_progress,
        )

        # Reuse connection pool (may be closed, so create new one)
        retry_pool = IMAPConnectionPool(secrets_path, max_workers, logger)

        # Submit retry tasks
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as retry_executor:
            retry_futures = {}
            for idx, (folder, _) in enumerate(to_retry):
                worker_id = idx % max_workers
                temp_db_path = worker_db_paths[worker_id]
                retry_future = retry_executor.submit(process_single_folder, folder, worker_id, temp_db_path)
                retry_futures[retry_future] = folder

            # Process retry results
            for future in concurrent.futures.as_completed(retry_futures):
                folder, msg_count, error = future.result()
                with total_msgs_lock:
                    total_msgs_count += msg_count
                if error is not None:
                    with failed_folders_lock:
                        failed_folders.append((folder, error))
                retry_bar.update(1)

        # Merge retry results
        logger.log("INFO", "retry_merge_start", {}, console="🔄 Merging retry results...")

        retry_merge_bar = tqdm(
            total=max_workers,
            desc="🔄 Merging retry databases",
            position=0,
            unit="db",
            disable=not show_progress,
        )

        main_db = sqlite3.connect(str(db_path), timeout=5.0)
        for worker_id, temp_db_path in worker_db_paths.items():
            if not temp_db_path.exists():
                retry_merge_bar.update(1)
                continue

            try:
                main_db.execute(f"ATTACH DATABASE '{temp_db_path}' AS worker_{worker_id}")

                main_db.execute(f"""
                    INSERT OR REPLACE INTO headers (folder, uid, data, updated_at)
                    SELECT folder, uid, data, updated_at FROM worker_{worker_id}.headers
                """)

                main_db.execute(f"""
                    INSERT OR REPLACE INTO folders (folder, parent, updated_at)
                    SELECT folder, parent, updated_at FROM worker_{worker_id}.folders
                """)

                main_db.execute(f"DETACH DATABASE worker_{worker_id}")
            except Exception:
                pass

            retry_merge_bar.update(1)

        main_db.commit()
        main_db.close()
        retry_merge_bar.close()
        retry_bar.close()

        # Cleanup temp DBs for this retry
        for temp_db_path in worker_db_paths.values():
            if temp_db_path.exists():
                try:
                    temp_db_path.unlink()
                except Exception:
                    pass

        retry_pool.shutdown()

    # Report permanently failed folders
    if failed_folders:
        logger.log(
            "WARNING",
            "permanent_failures",
            {"count": len(failed_folders), "folders": [f for f, _ in failed_folders]},
            console=f"\n⚠️  {len(failed_folders)} folders failed after {MAX_RETRIES} retry attempts:",
        )
        for folder, error in failed_folders:
            logger.log(
                "WARNING",
                "failed_folder",
                {"folder": folder, "error": str(error)},
                console=f"   ❌ {folder}: {error}",
            )

    # Log summary
    timer.stop()
    timer.count = total_msgs_count
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "cache",
            "folders": len(folders),
            "messages": total_msgs_count,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
            "workers": max_workers,
            "mode": "parallel",
        },
        console=(
            "\n📊 Summary — Build Cache (Parallel)\n"
            f"   🗂️  Folders processed: {len(folders)}\n"
            f"   ✉️  Messages cached: {total_msgs_count}\n"
            f"   🚀 Workers used: {max_workers}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )

    return timer, len(folders), total_msgs_count
