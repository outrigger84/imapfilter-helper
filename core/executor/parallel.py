"""Multi-worker parallel action execution."""
from __future__ import annotations

import imaplib
import queue as _queue
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, Sequence

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso

from core.executor.helpers import _imap_response_text, _quote_mailbox, _uidvalidity_mismatch

from core.executor.operations import (
    _perform_batch_keyword_operations,
    _perform_batch_move_operations,
    _verify_move_operation,
)


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


def _pending_folder_stats(db_path: Path) -> tuple[int, int]:
    """Return (folder_count, max_actions_in_any_folder) for pending actions."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(DISTINCT folder), MAX(cnt) "
            "FROM (SELECT folder, COUNT(*) AS cnt FROM actions "
            "WHERE status='pending' GROUP BY folder)"
        )
        row = cur.fetchone()
        if row and row[0]:
            return int(row[0]), int(row[1] or 0)
        return 0, 0
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


def _execute_folder_worker(
    conn: imaplib.IMAP4 | None,
    db_path: Path,
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
    progress_callback: Callable[[int], None] | None = None,
) -> tuple[str, int, int]:
    """
    Process all actions for a single source folder (runs in worker thread).

    The caller owns the IMAP connection lifetime — this function never
    creates or closes connections.  Connection errors (IMAP4.abort, OSError)
    are re-raised so the caller can reconnect and retry the folder.

    Args:
        conn: IMAP connection owned by the caller (None in dry-run mode)
        db_path: Path to main SQLite database
        folder: Source folder to process
        actions: List of action dicts for this folder
        worker_id: Worker ID for logging
        dry_run: Preview actions without executing
        strict: Abort on first error
        verify_moves: Verify moves with Message-ID searches
        backup_moved: Backup messages before moving
        backup_all: Backup all cached messages (unused in worker; handled by caller)
        backup_dir: Directory for backups
        logger: JsonLogger for logging

    Returns:
        Tuple of (folder_name, actions_done, actions_failed)
    """
    if logger is None:
        logger = JsonLogger(Path("imapfilter.log"))

    main_db = None
    actions_done = 0
    actions_failed = 0

    try:
        logger.log("DEBUG", "worker_start", {"worker_id": worker_id, "folder": folder})
        main_db = sqlite3.connect(str(db_path), timeout=60.0)
        logger.log("DEBUG", "worker_db_opened", {"worker_id": worker_id, "folder": folder})

        client = conn

        # Group actions by (folder, target) tuple for batch processing
        logger.log("DEBUG", "worker_grouping_actions", {"worker_id": worker_id, "folder": folder})
        action_groups: dict[tuple[str, str | None], list[dict]] = {}
        for action in actions:
            key = (action["folder"], action["target"])
            if key not in action_groups:
                action_groups[key] = []
            action_groups[key].append(action)

        logger.log("DEBUG", "worker_actions_grouped", {"worker_id": worker_id, "folder": folder, "group_count": len(action_groups)})

        for (grp_folder, target), group_actions in action_groups.items():
            logger.log(
                "DEBUG",
                "worker_processing_group",
                {"worker_id": worker_id, "folder": grp_folder, "target": target, "action_count": len(group_actions)},
                console=f"  ▶ Worker {worker_id}: {grp_folder} → {target or '(no target)'} ({len(group_actions):,} messages)",
            )
            if dry_run:
                for action in group_actions:
                    main_db.execute(
                        "UPDATE actions SET status = ?, executed_at = ? WHERE id = ?",
                        ("done", now_iso(), action["id"]),
                    )
                    actions_done += 1
                    if progress_callback is not None:
                        progress_callback(1)
                main_db.commit()
                continue

            # Real execution — IMAP4.abort/OSError bubble up for caller to reconnect
            try:
                logger.log("DEBUG", "worker_selecting_folder", {"worker_id": worker_id, "folder": grp_folder})
                sel_typ, sel_resp = client.select(f'"{grp_folder}"', readonly=False)
                logger.log("DEBUG", "worker_folder_selected", {"worker_id": worker_id, "folder": grp_folder, "status": sel_typ})
                if sel_typ != "OK":
                    error_msg = f"Cannot open folder: {_imap_response_text(sel_resp)}"
                    for action in group_actions:
                        main_db.execute(
                            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                            ("failed", now_iso(), error_msg, action["id"]),
                        )
                        actions_failed += 1
                    main_db.commit()
                    if strict:
                        raise imaplib.IMAP4.error(error_msg)
                    continue

                mismatch = _uidvalidity_mismatch(main_db, client, grp_folder)
                if mismatch is not None:
                    cached_uv, live_uv = mismatch
                    error_msg = (
                        f"UIDVALIDITY changed for {grp_folder} "
                        f"(cached {cached_uv}, server {live_uv}); "
                        "cached UIDs are stale — rebuild the cache"
                    )
                    for action in group_actions:
                        main_db.execute(
                            "UPDATE actions SET status = ?, executed_at = ?, error_message = ? WHERE id = ?",
                            ("failed", now_iso(), error_msg, action["id"]),
                        )
                        actions_failed += 1
                    main_db.commit()
                    logger.log(
                        "ERROR",
                        "execute_uidvalidity_mismatch",
                        {
                            "worker_id": worker_id,
                            "folder": grp_folder,
                            "cached": cached_uv,
                            "live": live_uv,
                            "count": len(group_actions),
                        },
                        console=(
                            f"🛑 {grp_folder}: UIDVALIDITY changed (cached {cached_uv}, "
                            f"server {live_uv}) — {len(group_actions)} actions failed; rebuild cache"
                        ),
                    )
                    if strict:
                        raise imaplib.IMAP4.error(error_msg)
                    continue

                supports_uid_move = hasattr(client, "capabilities") and b"MOVE" in getattr(client, "capabilities", ())

                keyword_actions_set = [a for a in group_actions if a.get("action_type") == "set_keywords"]
                keyword_actions_remove = [a for a in group_actions if a.get("action_type") == "remove_keywords"]
                move_actions = [a for a in group_actions if a.get("action_type", "move") == "move"]

                logger.log("DEBUG", "worker_actions_separated", {
                    "worker_id": worker_id,
                    "folder": grp_folder,
                    "set_keywords": len(keyword_actions_set),
                    "remove_keywords": len(keyword_actions_remove),
                    "moves": len(move_actions),
                })

                if keyword_actions_set:
                    logger.log("DEBUG", "worker_processing_keywords_set", {"worker_id": worker_id, "folder": grp_folder, "count": len(keyword_actions_set)})
                    batch_done, batch_failed = _perform_batch_keyword_operations(
                        client=client,
                        main_db=main_db,
                        folder=grp_folder,
                        actions=keyword_actions_set,
                        action_type="set_keywords",
                        logger=logger,
                        verbose=False,
                    )
                    logger.log("DEBUG", "worker_keywords_set_done", {"worker_id": worker_id, "folder": grp_folder, "done": batch_done, "failed": batch_failed})
                    actions_done += batch_done
                    actions_failed += batch_failed

                if keyword_actions_remove:
                    logger.log("DEBUG", "worker_processing_keywords_remove", {"worker_id": worker_id, "folder": grp_folder, "count": len(keyword_actions_remove)})
                    batch_done, batch_failed = _perform_batch_keyword_operations(
                        client=client,
                        main_db=main_db,
                        folder=grp_folder,
                        actions=keyword_actions_remove,
                        action_type="remove_keywords",
                        logger=logger,
                        verbose=False,
                    )
                    logger.log("DEBUG", "worker_keywords_remove_done", {"worker_id": worker_id, "folder": grp_folder, "done": batch_done, "failed": batch_failed})
                    actions_done += batch_done
                    actions_failed += batch_failed

                successful_moves: list[tuple[dict, str | None]] = []

                logger.log("DEBUG", "worker_processing_moves_start", {"worker_id": worker_id, "folder": grp_folder, "count": len(move_actions)})
                if move_actions:
                    batch_done, batch_failed, batch_successful = _perform_batch_move_operations(
                        client=client,
                        main_db=main_db,
                        folder=grp_folder,
                        target=target,
                        actions=move_actions,
                        supports_uid_move=supports_uid_move,
                        backup_moved=backup_moved,
                        backup_dir=backup_dir,
                        logger=logger,
                        progress_callback=progress_callback,
                    )
                    actions_done += batch_done
                    actions_failed += batch_failed
                    if verify_moves and target:
                        successful_moves.extend(batch_successful)

                try:
                    client.expunge()
                except Exception:
                    pass  # Best effort

                if verify_moves and successful_moves:
                    for action, message_id in successful_moves:
                        try:
                            _verify_move_operation(
                                client=client,
                                main_db=main_db,
                                action=action,
                                message_id=message_id,
                                logger=logger,
                                verbose=False,
                            )
                        except Exception as verify_exc:
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

                main_db.commit()

            except (imaplib.IMAP4.abort, OSError):
                raise  # Connection error — let the worker loop reconnect
            except Exception as exc:
                logger.log(
                    "ERROR",
                    "execute_folder_group_failed",
                    {"folder": grp_folder, "target": target, "error": str(exc)},
                )
                if strict:
                    raise

    except (imaplib.IMAP4.abort, OSError):
        raise  # Propagate to worker loop for reconnection
    except Exception as exc:
        logger.log(
            "ERROR",
            "execute_folder_worker_exception",
            {"folder": folder, "worker_id": worker_id, "error": str(exc)},
        )
        if strict:
            raise

    finally:
        if main_db:
            try:
                main_db.close()
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
    disabled_action_types: set[str] | None = None,
    folder_order: str = "alpha",
) -> tuple[PhaseTimer, Dict[str, int]]:
    """
    Execute pending actions in parallel (one worker per source folder).

    This is the main entry point for parallel execution. It coordinates:
    1. Pre-creating all target folders (eliminates race conditions)
    2. Grouping actions by source folder
    3. Spawning worker threads to process each folder
    4. Workers update the main database directly with 30-second busy timeout

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
    # Set WAL mode once in main thread to avoid contention with worker threads
    db.execute("PRAGMA journal_mode=WAL")

    folder_params = tuple(folders) if folders else ()
    folder_filter = ""
    if folder_params:
        placeholders = ",".join("?" for _ in folder_params)
        folder_filter = f" AND folder IN ({placeholders})"

    disabled_types = disabled_action_types or set()
    action_type_params: tuple[str, ...] = ()
    action_type_filter = ""
    if disabled_types:
        placeholders = ",".join("?" for _ in disabled_types)
        action_type_filter = f" AND action_type NOT IN ({placeholders})"
        action_type_params = tuple(disabled_types)
        logger.log(
            "INFO",
            "execute_action_types_disabled",
            {"disabled": sorted(disabled_types)},
            console=f"⚙️  Skipping action types: {', '.join(sorted(disabled_types))}",
        )

    where_params = folder_params + action_type_params
    combined_filter = folder_filter + action_type_filter

    # Count pending actions
    pending_cur = db.cursor()
    pending_cur.execute(
        "SELECT COUNT(*) FROM actions WHERE status='pending'" + combined_filter,
        where_params,
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
            try:
                client.logout()
            except Exception:
                pass

    # Group actions by source folder
    query = """
        WITH ranked AS (
            SELECT
                id, uid, folder, target, rule_name, priority, action_type, action_data,
                ROW_NUMBER() OVER (
                    PARTITION BY uid, folder, rule_name, action_type, target, action_data
                    ORDER BY priority DESC, created_at ASC, id ASC
                ) AS rn
            FROM actions
            WHERE status='pending'
    """
    if combined_filter:
        query = query.replace(
            "WHERE status='pending'",
            f"WHERE status='pending'{combined_filter}",
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
    cursor.execute(query, where_params)

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

    # Sort folders by action count based on folder_order preference
    if folder_order == "most-first":
        sorted_folders = sorted(folder_actions.keys(), key=lambda f: len(folder_actions[f]), reverse=True)
    elif folder_order == "least-first":
        sorted_folders = sorted(folder_actions.keys(), key=lambda f: len(folder_actions[f]))
    else:
        sorted_folders = sorted(folder_actions.keys())

    actual_total = sum(len(acts) for acts in folder_actions.values())

    # Log verbose execution overview (similar to sequential executor)
    if verbose:
        # Calculate group totals (folder → target)
        group_totals = {}
        for actions in folder_actions.values():
            for action in actions:
                key = (action["folder"], action["target"])
                group_totals[key] = group_totals.get(key, 0) + 1

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

    from core.imap_client import imap_login as _imap_login

    logger.log(
        "INFO",
        "execute_parallel_folders",
        {"folder_count": len(sorted_folders), "max_workers": max_workers},
        console=f"🔧 Processing {len(sorted_folders)} folders with {max_workers} workers",
    )

    # One task per folder — no chunking.  Each worker owns its connection for its
    # entire lifetime, eliminating the shared-pool acquire/release cycle that caused
    # the previous freeze (NOOP blocking in release() while holding the last slot).
    work_q: _queue.Queue[tuple[str, list[dict]]] = _queue.Queue()
    for f in sorted_folders:
        work_q.put((f, folder_actions[f]))

    actions_bar = tqdm(
        total=actual_total,
        desc="⚙️  Executing actions",
        unit="action",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    folders_bar = tqdm(
        total=len(sorted_folders),
        desc="📦 Processing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=1,
        disable=not show_progress,
    )


    total_done = 0
    total_failed = 0
    _stats_lock = threading.Lock()
    _first_error: list[Exception] = []  # captures first fatal error for strict mode

    def _worker(worker_id: int) -> None:
        nonlocal total_done, total_failed
        conn: imaplib.IMAP4 | None = None
        if not dry_run:
            conn = _imap_login(secrets_path, logger)
        try:
            while True:
                if strict and _first_error:
                    break
                try:
                    wfolder, wactions = work_q.get_nowait()
                except _queue.Empty:
                    break
                done = failed = 0
                reported_via_cb = [0]

                def _progress_cb(n: int, _reported=reported_via_cb) -> None:
                    _reported[0] += n
                    actions_bar.update(n)

                try:
                    _, done, failed = _execute_folder_worker(
                        conn=conn,
                        db_path=db_path,
                        folder=wfolder,
                        actions=wactions,
                        worker_id=worker_id,
                        dry_run=dry_run,
                        strict=strict,
                        verify_moves=verify_moves,
                        backup_moved=backup_moved,
                        backup_all=backup_all,
                        backup_dir=backup_dir,
                        logger=logger,
                        progress_callback=_progress_cb,
                    )
                except (imaplib.IMAP4.abort, OSError) as conn_exc:
                    logger.log(
                        "WARN",
                        "worker_reconnect",
                        {"worker_id": worker_id, "folder": wfolder, "error": str(conn_exc)},
                        console=f"🔄 Worker {worker_id} reconnecting after connection error",
                    )
                    if conn is not None:
                        try:
                            conn.shutdown()
                        except Exception:
                            pass
                    conn = None
                    if not dry_run:
                        try:
                            conn = _imap_login(secrets_path, logger)
                            _, done, failed = _execute_folder_worker(
                                conn=conn,
                                db_path=db_path,
                                folder=wfolder,
                                actions=wactions,
                                worker_id=worker_id,
                                dry_run=dry_run,
                                strict=strict,
                                verify_moves=verify_moves,
                                backup_moved=backup_moved,
                                backup_all=backup_all,
                                backup_dir=backup_dir,
                                logger=logger,
                                progress_callback=_progress_cb,
                            )
                        except Exception as retry_exc:
                            logger.log(
                                "ERROR",
                                "worker_retry_failed",
                                {"worker_id": worker_id, "folder": wfolder, "error": str(retry_exc)},
                                console=f"❌ Worker {worker_id} retry failed for {wfolder}: {retry_exc}",
                            )
                            failed = len(wactions)
                            if strict:
                                _first_error.append(retry_exc)
                except Exception as exc:
                    logger.log(
                        "ERROR",
                        "worker_folder_error",
                        {"worker_id": worker_id, "folder": wfolder, "error": str(exc)},
                    )
                    failed = len(wactions)
                    if strict:
                        _first_error.append(exc)
                with _stats_lock:
                    total_done += done
                    total_failed += failed
                    _td, _tf = total_done, total_failed
                # Catch-all: update bar for any actions not already reported via callback
                # (e.g. verification steps, failed actions mid-exception, etc.)
                remainder = (done + failed) - reported_via_cb[0]
                if remainder > 0:
                    actions_bar.update(remainder)
                folders_bar.set_postfix(done=_td, fail=_tf)
                folders_bar.update(1)
                logger.log(
                    "INFO",
                    "worker_folder_done",
                    {"worker_id": worker_id, "folder": wfolder, "done": done, "failed": failed},
                    console=f"  ✓ Worker {worker_id}: {wfolder} — {done:,} done, {failed} failed",
                )
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass

    threads = [
        threading.Thread(target=_worker, args=(i,), daemon=True)
        for i in range(max_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    actions_bar.close()
    folders_bar.close()

    if strict and _first_error:
        raise _first_error[0]

    timer.stop()
    timer.count = total_done

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
    _min_chunk_size: int = 10,
) -> bool:
    """
    Determine if parallel execution should be used.

    Rules:
    - parallel_workers == 0: Force sequential (return False)
    - parallel_workers > 0: Force parallel (return True)
    - parallel_workers is None: Auto-detect:
      - ≥5 unique source folders → parallel
      - single folder with enough actions to chunk across workers → parallel
      - otherwise → sequential

    Args:
        db_path: Path to the SQLite database
        parallel_workers: Override (0=sequential, >0=parallel, None=auto)
        logger: Optional logger for diagnostic messages
        _min_chunk_size: Minimum actions per chunk (must match execute_actions_parallel)

    Returns:
        True if parallel mode should be used, False otherwise
    """
    if parallel_workers == 0:
        if logger:
            logger.log("INFO", "sequential_forced", {}, console="📂 Sequential mode (--parallel-workers 0)")
        return False

    if parallel_workers is not None and parallel_workers > 0:
        if logger:
            logger.log(
                "INFO", "parallel_forced",
                {"workers": parallel_workers},
                console=f"🚀 Parallel mode ({parallel_workers} workers)",
            )
        return True

    # Auto-detect: use parallel if there are enough folders or one very large folder
    folder_count, max_folder_actions = _pending_folder_stats(db_path)

    if folder_count >= 5:
        if logger:
            logger.log(
                "INFO", "parallel_auto",
                {"folders": folder_count},
                console=f"🚀 Auto-parallel: {folder_count} source folders (≥5 threshold)",
            )
        return True

    # A single large folder can be chunked across workers
    if max_folder_actions >= 2 * _min_chunk_size:
        if logger:
            logger.log(
                "INFO", "parallel_auto_large_folder",
                {"folders": folder_count, "max_actions": max_folder_actions},
                console=f"🚀 Auto-parallel: folder with {max_folder_actions} actions (large enough to chunk)",
            )
        return True

    if logger:
        logger.log(
            "INFO", "sequential_auto",
            {"folders": folder_count, "max_actions": max_folder_actions},
            console=f"📂 Sequential mode: {folder_count} folder(s), {max_folder_actions} max actions",
        )
    return False
