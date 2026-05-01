"""Cache building helpers."""
from __future__ import annotations

import concurrent.futures
import imaplib
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
from core.database import init_db
from core.logging_utils import JsonLogger, PhaseTimer, now_iso
from core.imap_client import safe_search_all


VALID_LIMIT_ORDERS = {"newest", "oldest", "random"}
MEGA_FOLDER_THRESHOLD = 10000  # Messages per folder before splitting
FETCH_BATCH_SIZE = 200  # UIDs per IMAP FETCH call (probe-tuned for high-latency servers)


def split_mega_folders(
    folders: Sequence[str],
    folder_sizes: dict[str, int] | None,
    threshold: int = MEGA_FOLDER_THRESHOLD,
    max_chunks: int = 32,
) -> list[tuple[str, int | None, int | None]]:
    """
    Split large folders into multiple tasks for parallel processing.

    Folders with more messages than the threshold are split into chunks,
    allowing multiple workers to process different parts of the same folder.
    This prevents a single mega-folder from bottlenecking the entire cache build.

    For now, we return (folder, chunk_index, num_chunks) to indicate splits.
    The actual UID splitting will be done at fetch time based on actual UID positions.

    Args:
        folders: List of folder names
        folder_sizes: Dict mapping folder names to message counts
        threshold: Minimum messages per folder to trigger splitting

    Returns:
        List of (folder_name, chunk_idx, num_chunks) tuples.
        For unsplit folders: (folder_name, None, None)
        For split folders: (folder_name, 0, 6), (folder_name, 1, 6), etc.
    """
    if folder_sizes is None:
        folder_sizes = {}

    tasks = []
    for folder in folders:
        size = folder_sizes.get(folder, -1)

        # Don't split if size unknown or below threshold
        if size < threshold:
            tasks.append((folder, None, None))
            continue

        # Aim for ~50k messages per chunk; cap at max_chunks to avoid
        # flooding the IMAP server with hundreds of concurrent SEARCH calls
        # on the same mailbox (e.g. a 1.7M-message INBOX would otherwise
        # produce 173 chunks, each issuing its own full UID SEARCH).
        chunk_target = max(threshold, 50_000)
        num_chunks = max(2, min(max_chunks, (size + chunk_target - 1) // chunk_target))

        # Create a task for each chunk
        # We'll filter UIDs at fetch time based on position in the list
        for chunk_idx in range(num_chunks):
            tasks.append((folder, chunk_idx, num_chunks))

    return tasks


def distribute_folders_for_load_balancing(
    folders: Sequence[str],
    folder_sizes: dict[str, int] | None,
    num_workers: int,
) -> list[tuple[str, int | None, int | None]]:
    """
    Distribute folders across workers using greedy load balancing.

    1. Splits mega-folders (>10k messages) into chunks for parallel processing
    2. Sorts all tasks by folder size (largest first)
    3. The ThreadPoolExecutor will assign the first N tasks to N workers

    This ensures:
    - Large folders (like INBOX) are distributed across multiple workers
    - The first N tasks are the largest, keeping all workers busy initially
    - Smaller tasks are assigned later, minimizing idle time

    Args:
        folders: List of folder names
        folder_sizes: Dict mapping folder names to message counts
        num_workers: Number of parallel workers

    Returns:
        List of (folder_name, uid_start, uid_end) tuples sorted by size
        - For unsplit folders: (folder_name, None, None)
        - For split folders: (folder_name, 1, chunk_size), etc.
    """
    if not folders or num_workers <= 1:
        # Return as (folder, None, None) tuples for consistency
        return [(f, None, None) for f in folders]

    if folder_sizes is None:
        folder_sizes = {}

    # Step 1: Split mega-folders into multiple tasks
    tasks = split_mega_folders(folders, folder_sizes, MEGA_FOLDER_THRESHOLD)

    # Step 2: Sort tasks by folder size (largest first)
    # This ensures the largest folders' chunks are processed first
    sorted_tasks = sorted(
        tasks,
        key=lambda task: folder_sizes.get(task[0], -1),
        reverse=True  # Largest first
    )

    return sorted_tasks


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


def _parse_batch_fetch_response(
    msg_data,
) -> dict[str, tuple[bytes, list[str], str | None]]:
    """Parse a multi-UID IMAP FETCH response into per-UID results.

    imaplib returns a flat list of (envelope_bytes, header_bytes) tuples
    interleaved with b")" separator items. Each tuple represents one message.

    Returns:
        Dict mapping uid_str -> (header_bytes, flags_list, internaldate_string)
    """
    results: dict[str, tuple[bytes, list[str], str | None]] = {}
    if not msg_data:
        return results
    try:
        for item in msg_data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            metadata = item[0] if isinstance(item[0], bytes) else b""
            payload = item[1] if isinstance(item[1], (bytes, bytearray)) else b""

            # UID is present in the envelope when fetched via client.uid()
            uid_match = re.search(rb'\bUID\s+(\d+)\b', metadata)
            if not uid_match:
                continue
            uid_str = uid_match.group(1).decode()

            flags: list[str] = []
            flags_match = re.search(rb'FLAGS \(([^)]*)\)', metadata)
            if flags_match:
                flags_str = flags_match.group(1).decode('ascii', 'ignore').strip()
                flags = [f for f in flags_str.split() if f]

            internaldate: str | None = None
            date_match = re.search(rb'INTERNALDATE "([^"]*)"', metadata)
            if date_match:
                internaldate = date_match.group(1).decode('ascii', 'ignore')

            results[uid_str] = (bytes(payload), flags, internaldate)
    except Exception:
        pass
    return results


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
                total=len(limited_uids),
                desc=f"   ✉️ Fetching {folder}",
                unit="msg",
                dynamic_ncols=True,
                leave=False,
                position=1,
                disable=not show_progress,
            )

            db_insert_count = 0
            consecutive_imap_errors = 0
            for batch_start in range(0, len(limited_uids), FETCH_BATCH_SIZE):
                batch = limited_uids[batch_start : batch_start + FETCH_BATCH_SIZE]
                uid_set = b",".join(
                    uid if isinstance(uid, (bytes, bytearray)) else str(uid).encode()
                    for uid in batch
                )
                # Fetch headers + metadata in one round trip.
                # Folded FETCH responses from Dovecot are handled transparently
                # by _FoldingAwareFile installed in imap_login().
                try:
                    typ, msg_data = client.uid(
                        "FETCH", uid_set, "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)"
                    )
                    consecutive_imap_errors = 0
                except OSError as exc:
                    logger.log(
                        "WARNING",
                        "cache_fetch_socket_error",
                        {"folder": folder, "batch_start": batch_start, "error": str(exc)},
                        console=f"⚠️ {folder} batch@{batch_start}: network error (aborting folder): {exc}",
                    )
                    raise RuntimeError(
                        f"Network/socket error in {folder}, aborting folder"
                    ) from exc
                except imaplib.IMAP4.error as exc:
                    logger.log(
                        "WARNING",
                        "cache_fetch_imap_error",
                        {"folder": folder, "batch_start": batch_start, "error": str(exc)},
                        console=f"⚠️ {folder} batch@{batch_start}: IMAP error (skipping): {exc}",
                    )
                    msgs_bar.update(len(batch))
                    consecutive_imap_errors += 1
                    if consecutive_imap_errors >= 5:
                        raise RuntimeError(
                            f"Too many consecutive IMAP errors in {folder}, aborting folder"
                        ) from exc
                    try:
                        client.select(f'"{folder}"', readonly=True)
                    except Exception:
                        pass
                    continue
                if typ != "OK":
                    logger.log(
                        "WARNING",
                        "cache_fetch_failed",
                        {"folder": folder, "batch_start": batch_start},
                    )
                    msgs_bar.update(len(batch))
                    continue

                parsed = _parse_batch_fetch_response(msg_data)
                for uid_str, (raw_hdr, flags, internaldate) in parsed.items():
                    if not raw_hdr:
                        logger.log(
                            "WARNING",
                            "cache_parse_failed",
                            {"folder": folder, "uid": uid_str},
                        )
                        continue

                    cache_entry: dict = {"header": raw_hdr.decode(errors="ignore")}
                    if flags:
                        cache_entry["flags"] = flags
                    if internaldate:
                        cache_entry["internaldate"] = internaldate

                    db.execute(
                        "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
                        "VALUES(?,?,?,?)",
                        (folder, uid_str, json.dumps(cache_entry), now_iso()),
                    )
                    db_insert_count += 1
                    msgs_bar.update(1)

                # Commit periodically to reduce transaction scope and lock contention
                if db_insert_count % 50 == 0:
                    db.commit()

            msgs_bar.close()

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
    folders: Sequence[tuple[str, int | None, int | None]],
    *,
    show_progress: bool,
    logger: JsonLogger,
    limit: int | None,
    order: str,
    max_workers: int,
    folder_sizes: dict[str, int] | None = None,
    temp_dir: Path | None = None,
) -> tuple[PhaseTimer, int, int]:
    """
    Build cache with parallel folder processing.

    Uses ThreadPoolExecutor with IMAP connection pool for concurrent task processing.
    Supports mega-folder splitting: folders with >10k messages are split into
    multiple chunk tasks for parallel processing across workers.
    Each worker writes to its own temporary database to eliminate contention.
    Databases are merged at the end.
    Thread-safe progress tracking with locks.
    Soft error handling: continues on task failures, with automatic retry.

    Args:
        secrets_path: Path to IMAP secrets file
        db_path: Path to cache database
        folders: List of (folder_name, chunk_idx, num_chunks) tuples.
                 For unsplit folders: (folder_name, None, None)
                 For split folders: (folder_name, 0, 6), (folder_name, 1, 6), etc.
                 Chunk filtering divides the UID list by position (not UID value)
        show_progress: Whether to show progress bars
        logger: JsonLogger for logging
        limit: Limit messages per folder
        order: Message order (newest/oldest/random)
        max_workers: Number of parallel IMAP connections
        folder_sizes: Optional dict of folder sizes for progress display
        temp_dir: Base directory for intermediate worker databases (default: system temp dir)

    Returns:
        Tuple of (timer, folder_count, message_count)
    """
    timer = PhaseTimer("cache")

    # Create temporary directory for thread-based databases
    # Each thread gets its own database file to avoid SQLite locking issues
    base_temp = temp_dir if temp_dir is not None else Path(tempfile.gettempdir())
    temp_dir = base_temp / f"imapfilter_cache_{os.getpid()}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Create connection pool with specified max workers
    pool = IMAPConnectionPool(secrets_path, max_workers, logger)

    # Shared UID cache so mega-folder chunks share one UID SEARCH result.
    # Without this, a 1.7M-message INBOX split into 32 chunks would issue
    # 32 separate "UID SEARCH UNDELETED" commands, flooding the IMAP server.
    _uid_cache: dict[str, list[bytes]] = {}
    _uid_cache_locks: dict[str, threading.Lock] = {}
    _uid_cache_meta_lock = threading.Lock()

    def _get_uids_cached(folder: str, client) -> list[bytes]:
        """Return cached UIDs for folder, fetching via SEARCH only on first call."""
        if folder in _uid_cache:
            return _uid_cache[folder]
        with _uid_cache_meta_lock:
            if folder not in _uid_cache_locks:
                _uid_cache_locks[folder] = threading.Lock()
        with _uid_cache_locks[folder]:
            if folder in _uid_cache:
                return _uid_cache[folder]
            uids = safe_search_all(client, undeleted_only=True)
            _uid_cache[folder] = list(uids)
            return _uid_cache[folder]

    # Thread-safe counters
    total_msgs_lock = threading.Lock()
    total_msgs_count = 0

    # Track failed tasks for retry logic (folder, uid_start, uid_end, error)
    failed_folders: list[tuple[str, int | None, int | None, Exception]] = []
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

    def process_single_folder(folder: str, chunk_idx: int | None, num_chunks: int | None, worker_id: int) -> tuple[str, int, Exception | None]:
        """
        Process a single folder or chunk of a mega-folder (runs in worker thread).

        Args:
            folder: Folder name to process
            chunk_idx: Chunk index (0, 1, 2, ...) for mega-folder splitting (None = process all)
            num_chunks: Total number of chunks for this folder (None = no splitting)
            worker_id: Worker ID for progress bar display

        For mega-folders:
        - chunk_idx and num_chunks divide the UID list into equal parts by position
        - Example: chunk_idx=0, num_chunks=6 means process first 1/6 of UIDs

        Each actual thread gets its own temporary database file (true isolation, no contention).
        worker_id is only used for progress bar display.

        Returns tuple of (folder_name, message_count, error_or_none)
        """
        client = None
        db = None
        connection_ok = True  # Track whether the connection is still healthy
        try:
            # Acquire connection from pool
            client = pool.acquire()

            # Generate temp database path based on actual thread ID (not worker_id)
            # This ensures true per-thread isolation regardless of task scheduling
            thread_id = threading.current_thread().ident
            temp_db_path = temp_dir / f"thread_{thread_id}.db"

            # Each thread writes to its own temporary database (true isolation)
            db = sqlite3.connect(
                str(temp_db_path),
                timeout=5.0,            # Short timeout OK - no contention
                check_same_thread=False # Allow thread-safe usage
            )
            # Initialize schema to match main database
            db.execute(
                "CREATE TABLE IF NOT EXISTS headers "
                "(folder TEXT, uid TEXT, data TEXT, updated_at TEXT, "
                "PRIMARY KEY (folder, uid))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS folders "
                "(id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)"
            )

            # Select folder in read-only mode
            sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                logger.log("INFO", "cache_folder_skipped", {"folder": folder}, console=f"⚠️ Skipped {folder}")
                folders_bar.update(1)
                return folder, 0, None

            # Get undeleted messages — use shared cache so chunk workers for the
            # same mega-folder don't each issue a full UID SEARCH.
            try:
                uids = _get_uids_cached(folder, client)
            except OSError as exc:
                connection_ok = False
                raise RuntimeError(f"UID SEARCH failed (network error) in {folder}: {exc}") from exc

            # Filter by chunk if processing a mega-folder split
            if chunk_idx is not None and num_chunks is not None and num_chunks > 1:
                # Split UID list into equal chunks based on position
                chunk_size = (len(uids) + num_chunks - 1) // num_chunks  # Ceiling division
                chunk_start = chunk_idx * chunk_size
                chunk_end = min((chunk_idx + 1) * chunk_size, len(uids))
                uids = uids[chunk_start:chunk_end]

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
                # db.close() will be called in finally block
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
            # Show chunk info in description if processing a split mega-folder
            desc = f"   Worker {worker_id}: {folder[:40]}"
            if chunk_idx is not None and num_chunks is not None and num_chunks > 1:
                desc += f" [chunk {chunk_idx + 1}/{num_chunks}]"
            worker_bar.set_description(desc)

            # Fetch and cache headers in batches; commit every ~50 inserts
            db_insert_count = 0
            consecutive_imap_errors = 0
            for batch_start in range(0, len(limited_uids), FETCH_BATCH_SIZE):
                batch = limited_uids[batch_start : batch_start + FETCH_BATCH_SIZE]
                uid_set = b",".join(
                    uid if isinstance(uid, (bytes, bytearray)) else str(uid).encode()
                    for uid in batch
                )
                # Folded FETCH responses from Dovecot are handled transparently
                # by _FoldingAwareFile installed in imap_login().
                try:
                    typ, msg_data = client.uid(
                        "FETCH", uid_set, "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)"
                    )
                    consecutive_imap_errors = 0
                except OSError as exc:
                    connection_ok = False
                    logger.log(
                        "WARNING",
                        "cache_fetch_socket_error",
                        {"folder": folder, "batch_start": batch_start, "error": str(exc)},
                        console=f"⚠️ {folder} batch@{batch_start}: network error (aborting folder): {exc}",
                    )
                    raise RuntimeError(
                        f"Network/socket error in {folder}, aborting folder"
                    ) from exc
                except imaplib.IMAP4.error as exc:
                    logger.log(
                        "WARNING",
                        "cache_fetch_imap_error",
                        {"folder": folder, "batch_start": batch_start, "error": str(exc)},
                        console=f"⚠️ {folder} batch@{batch_start}: IMAP error (skipping): {exc}",
                    )
                    worker_bar.update(len(batch))
                    consecutive_imap_errors += 1
                    if consecutive_imap_errors >= 5:
                        raise RuntimeError(
                            f"Too many consecutive IMAP errors in {folder}, aborting folder"
                        ) from exc
                    try:
                        client.select(f'"{folder}"', readonly=True)
                    except Exception:
                        pass
                    continue
                if typ != "OK":
                    logger.log(
                        "WARNING",
                        "cache_fetch_failed",
                        {"folder": folder, "batch_start": batch_start},
                    )
                    worker_bar.update(len(batch))
                    continue

                parsed = _parse_batch_fetch_response(msg_data)
                for uid_str, (raw_hdr, flags, internaldate) in parsed.items():
                    if not raw_hdr:
                        logger.log(
                            "WARNING",
                            "cache_parse_failed",
                            {"folder": folder, "uid": uid_str},
                        )
                        continue

                    cache_entry: dict = {"header": raw_hdr.decode(errors="ignore")}
                    if flags:
                        cache_entry["flags"] = flags
                    if internaldate:
                        cache_entry["internaldate"] = internaldate

                    db.execute(
                        "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
                        "VALUES(?,?,?,?)",
                        (folder, uid_str, json.dumps(cache_entry), now_iso()),
                    )
                    worker_bar.update(1)
                    db_insert_count += 1

                # Commit periodically to reduce transaction scope and lock contention
                if db_insert_count % 50 == 0:
                    db.commit()

            # Commit folder's cached messages
            db.execute(
                "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                (folder, "/".join(folder.split("/")[:-1]), now_iso()),
            )
            db.commit()
            # db.close() will be called in finally block

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
            # Always close database connection to prevent locks during merge
            if db:
                try:
                    db.close()
                except Exception:
                    pass
            if client:
                if connection_ok:
                    pool.release(client)
                else:
                    # Connection is in a bad/unknown state after a network error;
                    # discard it so the pool can create a fresh one next time.
                    pool.discard(client)

    # Execute parallel processing with ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all folder processing tasks with round-robin worker_id assignment (for display only)
        # Each thread will generate its own temp database path based on thread ID
        # Note: folders is now a list of (folder, chunk_idx, num_chunks) tuples
        futures = {}
        for idx, task in enumerate(folders):
            folder, chunk_idx, num_chunks = task
            worker_id = idx % max_workers  # Round-robin assignment for progress bar display only
            future = executor.submit(process_single_folder, folder, chunk_idx, num_chunks, worker_id)
            futures[future] = folder

        # Process results as they complete
        for future in concurrent.futures.as_completed(futures):
            folder, msg_count, error = future.result()
            with total_msgs_lock:
                total_msgs_count += msg_count
            # Track failures for retry logic
            if error is not None:
                with failed_folders_lock:
                    # Find the task to get chunk info
                    task = next((t for t in folders if t[0] == folder), (folder, None, None))
                    failed_folders.append((*task, error))

    # Clean up connection pool and worker progress bars
    pool.shutdown()
    for bar in worker_bars.values():
        bar.close()
    folders_bar.close()

    # Force garbage collection to clean up closed database connections
    import gc
    gc.collect()

    # Aggressive cleanup of stale lock files before merge
    # This prevents SQLite from trying to use old WAL/SHM files
    # IMPORTANT: Only delete .db-wal and .db-shm files, NOT the .db files themselves!
    logger.log("INFO", "cleanup_locks", {}, console="🧹 Cleaning up database locks...")
    for _ in range(2):
        time.sleep(0.5)
        # Only delete WAL and SHM files, preserve the actual .db files
        for temp_db_path in temp_dir.glob("thread_*.db-wal"):
            try:
                temp_db_path.unlink()
            except Exception:
                pass  # Files may be in use
        for temp_db_path in temp_dir.glob("thread_*.db-shm"):
            try:
                temp_db_path.unlink()
            except Exception:
                pass  # Files may be in use

    # Glob the main database files (WAL cleanup above removed thread -wal and -shm, .db files remain)
    thread_temp_dbs = sorted(temp_dir.glob("thread_*.db"))

    # Give filesystem time to settle
    time.sleep(1.0)

    # Ensure main database is initialized with correct schema
    init_conn = init_db(db_path, logger=logger)
    init_conn.close()  # Close the initialization connection to release locks

    # Give additional time to fully release locks
    time.sleep(0.5)

    # Merge temp databases into main database
    logger.log("INFO", "merge_start", {}, console="🔄 Merging worker databases...")

    merge_bar = tqdm(
        total=len(thread_temp_dbs),
        desc="🔄 Merging databases",
        position=0,
        unit="db",
        disable=not show_progress,
    )

    # Open main database for merge with retry logic
    main_db = None
    max_open_retries = 5
    pragma_lock_issue = False
    for open_attempt in range(max_open_retries):
        try:
            main_db = sqlite3.connect(str(db_path), timeout=60.0, check_same_thread=False)
            # Try to set PRAGMAs, but don't fail if database is locked
            # The important thing is to open it, not to optimize its settings
            try:
                main_db.execute("PRAGMA busy_timeout=60000")  # 60 second busy timeout
                main_db.execute("PRAGMA synchronous=NORMAL")  # Balance speed vs safety
                # Try journal mode change but don't fail if locked
                try:
                    main_db.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.OperationalError as pragma_error:
                    if "database is locked" in str(pragma_error):
                        pragma_lock_issue = True
                        logger.log("INFO", "pragma_lock_skip", {}, console="⚠️  Database locked for PRAGMA changes - proceeding anyway")
                    else:
                        raise
            except sqlite3.OperationalError as pragma_error:
                if "database is locked" not in str(pragma_error):
                    raise
                pragma_lock_issue = True
                logger.log("INFO", "pragma_lock_skip", {}, console="⚠️  Database locked for PRAGMA changes - proceeding anyway")
            break
        except sqlite3.OperationalError as open_error:
            if "database is locked" in str(open_error) and open_attempt < max_open_retries - 1:
                delay = 0.5 * (2 ** open_attempt)
                logger.log("INFO", "db_lock_retry", {"attempt": open_attempt + 1}, console=f"⏳ Database locked, retrying in {delay}s...")
                time.sleep(delay)
                continue
            logger.log("ERROR", "db_open_failed", {"error": str(open_error)}, console=f"❌ Failed to open main database: {open_error}")
            # Save temp database location for manual recovery
            logger.log("ERROR", "merge_recovery_info", {"temp_dir": str(temp_dir)}, console=f"⚠️  Temp databases preserved at: {temp_dir}")
            raise

    if main_db is None:
        raise Exception("Failed to open main database after retries")

    merge_successful = True
    failed_merges = []

    for temp_idx, temp_db_path in enumerate(thread_temp_dbs):
        try:
            # Open temp database separately with generous timeout (avoid ATTACH issues)
            temp_db = None
            try:
                temp_db = sqlite3.connect(str(temp_db_path), timeout=30.0)
                temp_db.execute("PRAGMA query_only=TRUE")  # Open read-only

                # Fetch all headers from temp database
                headers_cursor = temp_db.execute("SELECT folder, uid, data, updated_at FROM headers")
                headers_rows = headers_cursor.fetchall()

                # Fetch all folders from temp database
                folders_cursor = temp_db.execute("SELECT name, parent, updated_at FROM folders")
                folders_rows = folders_cursor.fetchall()

                temp_db.close()
                temp_db = None

                # Insert headers into main database with retries
                max_insert_retries = 5
                for insert_attempt in range(max_insert_retries):
                    try:
                        main_db.executemany(
                            "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
                            headers_rows
                        )
                        break
                    except sqlite3.OperationalError as lock_error:
                        if "database is locked" in str(lock_error) and insert_attempt < max_insert_retries - 1:
                            delay = 0.2 * (2 ** insert_attempt)
                            time.sleep(delay)
                            continue
                        raise

                # Insert folders into main database with retries
                for insert_attempt in range(max_insert_retries):
                    try:
                        main_db.executemany(
                            "INSERT OR REPLACE INTO folders (name, parent, updated_at) VALUES (?, ?, ?)",
                            folders_rows
                        )
                        break
                    except sqlite3.OperationalError as lock_error:
                        if "database is locked" in str(lock_error) and insert_attempt < max_insert_retries - 1:
                            delay = 0.2 * (2 ** insert_attempt)
                            time.sleep(delay)
                            continue
                        raise

                logger.log(
                    "INFO",
                    "merge_successful",
                    {"thread_db": str(temp_db_path), "headers": len(headers_rows), "folders": len(folders_rows)},
                )

            finally:
                if temp_db:
                    try:
                        temp_db.close()
                    except Exception:
                        pass

        except Exception as merge_error:
            merge_successful = False
            failed_merges.append((temp_db_path.name, str(merge_error)))
            logger.log(
                "ERROR",
                "merge_failed",
                {"thread_db": str(temp_db_path), "error": str(merge_error)},
                console=f"⚠️  Merge error for {temp_db_path.name}: {merge_error}",
            )

        merge_bar.update(1)

    # Commit all changes before closing
    try:
        main_db.commit()
    except Exception as commit_error:
        logger.log(
            "ERROR",
            "merge_commit_failed",
            {"error": str(commit_error)},
            console=f"❌ Failed to commit merged data: {commit_error}",
        )
        merge_successful = False

    main_db.close()
    merge_bar.close()

    # Report merge results
    if not merge_successful:
        if failed_merges:
            logger.log(
                "ERROR",
                "merge_partial",
                {"failed_count": len(failed_merges), "failed_dbs": failed_merges},
                console=f"❌ Merge partially failed - {len(failed_merges)} database(s) not merged",
            )
        logger.log(
            "WARNING",
            "merge_incomplete",
            {},
            console="⚠️  Cache merge incomplete - some data may be missing",
        )
    else:
        logger.log("INFO", "merge_complete", {}, console="✅ Merge complete")

    # Cleanup temp files and directory (but preserve if merge failed for recovery)
    if merge_successful:
        for temp_db_path in thread_temp_dbs:
            if temp_db_path.exists():
                try:
                    temp_db_path.unlink()
                except Exception as cleanup_error:
                    logger.log(
                        "WARNING",
                        "cleanup_failed",
                        {"path": str(temp_db_path), "error": str(cleanup_error)},
                    )
    else:
        logger.log(
            "INFO",
            "temp_db_preserved",
            {"temp_dir": str(temp_dir)},
            console=f"ℹ️  Temporary databases preserved at: {temp_dir}"
        )
        logger.log(
            "INFO",
            "manual_merge_instructions",
            {},
            console="📋 To manually merge databases, run:\n"
                   f"   python3 /root/imapfilter/merge_worker_dbs.py --temp-dir {temp_dir} --output <merged_db_path>"
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
            for idx, task in enumerate(to_retry):
                folder, chunk_idx, num_chunks, _ = task  # Unpack task, ignore old error
                worker_id = idx % max_workers  # For progress bar display only
                retry_future = retry_executor.submit(process_single_folder, folder, chunk_idx, num_chunks, worker_id)
                retry_futures[retry_future] = folder

            # Process retry results
            for future in concurrent.futures.as_completed(retry_futures):
                folder, msg_count, error = future.result()
                with total_msgs_lock:
                    total_msgs_count += msg_count
                if error is not None:
                    with failed_folders_lock:
                        # Find the task to get chunk info
                        task = next((t for t in to_retry if t[0] == folder), (folder, None, None, error))
                        failed_folders.append(task)
                retry_bar.update(1)

        # Merge retry results
        logger.log("INFO", "retry_merge_start", {}, console="🔄 Merging retry results...")

        # Discover all thread-based temp databases from retry phase
        retry_thread_temp_dbs = sorted(temp_dir.glob("thread_*.db"))

        retry_merge_bar = tqdm(
            total=len(retry_thread_temp_dbs),
            desc="🔄 Merging retry databases",
            position=0,
            unit="db",
            disable=not show_progress,
        )

        # Open main database for retry merge with retry logic
        main_db = None
        max_open_retries = 5
        for open_attempt in range(max_open_retries):
            try:
                main_db = sqlite3.connect(str(db_path), timeout=60.0, check_same_thread=False)
                # Set PRAGMAs with retries in case of locking
                for pragma_attempt in range(3):
                    try:
                        main_db.execute("PRAGMA journal_mode=DELETE")
                        main_db.execute("PRAGMA busy_timeout=60000")
                        main_db.execute("PRAGMA synchronous=NORMAL")
                        break
                    except sqlite3.OperationalError as pragma_error:
                        if "database is locked" in str(pragma_error) and pragma_attempt < 2:
                            time.sleep(0.5)
                            continue
                        raise
                break
            except sqlite3.OperationalError as open_error:
                if "database is locked" in str(open_error) and open_attempt < max_open_retries - 1:
                    delay = 0.5 * (2 ** open_attempt)
                    logger.log("INFO", "retry_db_lock_retry", {"attempt": open_attempt + 1}, console=f"⏳ Database locked, retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                logger.log("ERROR", "retry_db_open_failed", {"error": str(open_error)}, console=f"❌ Failed to open main database for retry: {open_error}")
                raise

        if main_db is None:
            raise Exception("Failed to open main database for retry merge after retries")

        retry_merge_successful = True
        for temp_idx, temp_db_path in enumerate(retry_thread_temp_dbs):
            if not temp_db_path.exists():
                retry_merge_bar.update(1)
                continue

            try:
                # Open temp database separately (avoid ATTACH issues)
                temp_db = None
                try:
                    temp_db = sqlite3.connect(str(temp_db_path), timeout=30.0)
                    temp_db.execute("PRAGMA query_only=TRUE")

                    # Fetch all headers from temp database
                    headers_cursor = temp_db.execute("SELECT folder, uid, data, updated_at FROM headers")
                    headers_rows = headers_cursor.fetchall()

                    # Fetch all folders from temp database
                    folders_cursor = temp_db.execute("SELECT name, parent, updated_at FROM folders")
                    folders_rows = folders_cursor.fetchall()

                    temp_db.close()
                    temp_db = None

                    # Insert headers into main database with retries
                    max_insert_retries = 5
                    for insert_attempt in range(max_insert_retries):
                        try:
                            main_db.executemany(
                                "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) VALUES (?, ?, ?, ?)",
                                headers_rows
                            )
                            break
                        except sqlite3.OperationalError as lock_error:
                            if "database is locked" in str(lock_error) and insert_attempt < max_insert_retries - 1:
                                delay = 0.2 * (2 ** insert_attempt)
                                time.sleep(delay)
                                continue
                            raise

                    # Insert folders into main database with retries
                    for insert_attempt in range(max_insert_retries):
                        try:
                            main_db.executemany(
                                "INSERT OR REPLACE INTO folders (name, parent, updated_at) VALUES (?, ?, ?)",
                                folders_rows
                            )
                            break
                        except sqlite3.OperationalError as lock_error:
                            if "database is locked" in str(lock_error) and insert_attempt < max_insert_retries - 1:
                                delay = 0.2 * (2 ** insert_attempt)
                                time.sleep(delay)
                                continue
                            raise

                    logger.log(
                        "INFO",
                        "retry_merge_successful",
                        {"thread_db": str(temp_db_path), "headers": len(headers_rows), "folders": len(folders_rows)},
                    )
                finally:
                    if temp_db:
                        try:
                            temp_db.close()
                        except Exception:
                            pass

            except Exception as merge_error:
                retry_merge_successful = False
                logger.log(
                    "WARNING",
                    "retry_merge_failed",
                    {"thread_db": str(temp_db_path), "error": str(merge_error)},
                    console=f"⚠️  Retry merge error for {temp_db_path.name}: {merge_error}",
                )

            retry_merge_bar.update(1)

        try:
            main_db.commit()
        except Exception as commit_error:
            logger.log(
                "ERROR",
                "retry_merge_commit_failed",
                {"error": str(commit_error)},
                console=f"❌ Failed to commit retry merged data: {commit_error}",
            )
            retry_merge_successful = False

        main_db.close()
        retry_merge_bar.close()

        if not retry_merge_successful:
            logger.log(
                "WARNING",
                "retry_merge_incomplete",
                {},
                console="⚠️  Retry merge incomplete - some data may be missing",
            )
        retry_bar.close()

        # Cleanup temp DBs from retry phase
        for temp_db_path in retry_thread_temp_dbs:
            if temp_db_path.exists():
                try:
                    temp_db_path.unlink()
                except Exception:
                    pass

        retry_pool.shutdown()

    # Report permanently failed folders
    if failed_folders:
        failed_folder_names = [f[0] for f in failed_folders]
        logger.log(
            "WARNING",
            "permanent_failures",
            {"count": len(failed_folders), "folders": failed_folder_names},
            console=f"\n⚠️  {len(failed_folders)} tasks failed after {MAX_RETRIES} retry attempts:",
        )
        for folder, chunk_idx, num_chunks, error in failed_folders:
            task_desc = folder
            if chunk_idx is not None and num_chunks is not None and num_chunks > 1:
                task_desc += f" [chunk {chunk_idx + 1}/{num_chunks}]"
            logger.log(
                "WARNING",
                "failed_folder",
                {"folder": folder, "chunk": (chunk_idx, num_chunks), "error": str(error)},
                console=f"   ❌ {task_desc}: {error}",
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
