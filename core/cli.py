"""Command-line interface helpers for the IMAPFilter helper."""
from __future__ import annotations

import argparse
import imaplib
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

from core.cache_builder import build_cache, build_cache_parallel, compact_cache, distribute_folders_for_load_balancing
from core.config import AppConfig, build_default_config
from core.database import init_db
from core.executor import execute_actions, execute_actions_parallel, should_use_parallel_mode
from core.imap_client import imap_login, list_all_folders, get_folder_sizes, expand_folders_recursive
from core.keywords import KeywordManager
from core.logging_utils import JsonLogger, PhaseTimer
from core.rule_engine import evaluate_rules, load_rules
from core.stream_processor import count_stream_messages, stream_messages
from core.stream_executor import stream_execute
from core.stream_resume import create_resume_log


Handler = Callable[[argparse.Namespace, AppConfig, sqlite3.Connection, JsonLogger], int]
DEFAULT_INBOX = "INBOX"


def _validate_cache_access(
    cache_path: Path,
    require_exists: bool,
    logger: JsonLogger
) -> None:
    """
    Validate cache file accessibility.

    Args:
        cache_path: Path to cache database
        require_exists: True if cache must exist, False if creating new
        logger: Logger for error messages

    Raises:
        FileNotFoundError: If require_exists=True and cache doesn't exist
    """
    if require_exists and not cache_path.exists():
        logger.log("ERROR", "cache_not_found", {"path": str(cache_path)})
        raise FileNotFoundError(
            f"Cache not found: {cache_path}. Run build-cache first."
        )

    if not require_exists:
        # Ensure parent directory exists for write operations
        cache_path.parent.mkdir(parents=True, exist_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(description="IMAPFilter Helper")
    parser.add_argument(
        "--cache-file",
        type=Path,
        help="Path to cache database (default: data/cache.db)",
    )
    parser.add_argument(
        "--no-gotify",
        action="store_true",
        help="Disable Gotify notifications for this run",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build-cache", help="Build local message cache")
    build_scope = p_build.add_mutually_exclusive_group()
    build_scope.add_argument("--all-folders", action="store_true", help="Scan all folders")
    build_scope.add_argument(
        "--folder",
        action="append",
        help="Scan only the specified folder (can be repeated)",
    )
    build_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Scan the specified folder and all its subfolders recursively (can be repeated)",
    )
    p_build.add_argument(
        "--limit",
        type=int,
        help="Cache at most this many messages per folder",
    )
    p_build.add_argument(
        "--order",
        choices=["newest", "oldest", "random"],
        help="When limiting, choose which messages to keep (default: newest)",
    )
    p_build.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help="Number of parallel IMAP connections (default: auto-detect based on folder count, use 1 to force sequential)",
    )
    p_eval = sub.add_parser("evaluate", help="Evaluate rules against cache")
    p_eval.add_argument("--dry-run", action="store_true", help="Simulate rule matches only")
    p_eval.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed match information during evaluation",
    )
    p_eval.add_argument(
        "--debug-headers",
        action="store_true",
        help="Log message headers for troubleshooting",
    )
    p_eval.add_argument(
        "--limit",
        type=int,
        help="Stop after this many matched emails have been found",
    )
    eval_scope = p_eval.add_mutually_exclusive_group()
    eval_scope.add_argument(
        "--all-folders",
        action="store_true",
        help="Evaluate rules against every cached folder",
    )
    eval_scope.add_argument(
        "--folder",
        action="append",
        help="Evaluate rules only for the specified folder(s)",
    )
    eval_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Evaluate rules for the specified folder and all its subfolders recursively",
    )

    p_exec = sub.add_parser("execute", help="Execute queued actions")
    p_exec.add_argument("--dry-run", action="store_true", help="Simulate execution only (no IMAP writes)")
    p_exec.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops")
    p_exec.add_argument(
        "--verify-moves",
        action="store_true",
        help="Confirm successful moves by searching for Message-ID in source and destination mailboxes",
    )
    p_exec.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-message progress and log IMAP server replies",
    )
    p_exec.add_argument(
        "--limit",
        type=int,
        help="Process at most this many pending actions during execution",
    )
    exec_scope = p_exec.add_mutually_exclusive_group()
    exec_scope.add_argument(
        "--all-folders",
        action="store_true",
        help="Execute pending actions for every folder",
    )
    exec_scope.add_argument(
        "--folder",
        action="append",
        help="Execute only the pending actions for the specified folder(s)",
    )
    exec_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Execute pending actions for the specified folder and all its subfolders recursively",
    )
    p_exec.add_argument(
        "--backup-moved",
        action="store_true",
        help="Backup messages before moving them (recommended for safety)",
    )
    p_exec.add_argument(
        "--backup-all",
        action="store_true",
        help="Backup all cached messages after execution completes (creates full archive)",
    )
    p_exec.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help=(
            "Number of parallel workers for execute phase. "
            "0=force sequential, N>0=force N workers, None=auto-detect "
            "(parallel if ≥5 folders, otherwise sequential). Default: None (auto-detect)"
        ),
    )
    p_exec.add_argument(
        "--no-move",
        action="store_true",
        help="Skip all move actions during execution",
    )
    p_exec.add_argument(
        "--no-keyword",
        action="store_true",
        help="Skip all keyword (set/remove flags) actions during execution",
    )
    p_exec.add_argument(
        "--folder-order",
        dest="folder_order",
        choices=["most-first", "least-first", "alpha"],
        default="alpha",
        help="Order folders by destination match count: most-first (most matches first), least-first, or alpha (default)",
    )

    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    run_scope = p_run.add_mutually_exclusive_group()
    run_scope.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    run_scope.add_argument(
        "--folder",
        action="append",
        help="Process only the specified folder(s) during cache build",
    )
    run_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Process the specified folder and all its subfolders recursively",
    )
    p_run.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops during execute")
    p_run.add_argument(
        "--verify-moves",
        action="store_true",
        help="Confirm successful moves by searching for Message-ID in source and destination mailboxes",
    )
    p_run.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress and log IMAP replies during evaluate/execute",
    )
    p_run.add_argument(
        "--debug-headers",
        action="store_true",
        help="Log message headers while evaluating rules",
    )
    p_run.add_argument(
        "--action-limit",
        "--execute-limit",
        dest="action_limit",
        type=int,
        help="Process at most this many pending actions during the execute phase",
    )
    p_run.add_argument(
        "--limit",
        "--cache-limit",
        dest="cache_limit",
        type=int,
        help="Cache at most this many messages per folder during the cache phase",
    )
    p_run.add_argument(
        "--order",
        "--cache-order",
        dest="cache_order",
        choices=["newest", "oldest", "random"],
        help="When limiting cache, choose which messages to keep (default: newest)",
    )
    p_run.add_argument(
        "--backup-moved",
        action="store_true",
        help="Backup messages before moving them during the execute phase (recommended)",
    )
    p_run.add_argument(
        "--backup-all",
        action="store_true",
        help="Backup all cached messages after execution completes (creates full archive)",
    )
    p_run.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help=(
            "Number of parallel workers for execute phase. "
            "0=force sequential, N>0=force N workers, None=auto-detect "
            "(parallel if ≥5 folders, otherwise sequential). Default: None (auto-detect)"
        ),
    )
    p_run.add_argument(
        "--no-move",
        action="store_true",
        help="Skip all move actions during the execute phase",
    )
    p_run.add_argument(
        "--no-keyword",
        action="store_true",
        help="Skip all keyword (set/remove flags) actions during the execute phase",
    )
    p_run.add_argument(
        "--folder-order",
        dest="folder_order",
        choices=["most-first", "least-first", "alpha"],
        default="alpha",
        help="Order folders by destination match count during execute: most-first (most matches first), least-first, or alpha (default)",
    )

    p_eval_exec = sub.add_parser("eval-execute", help="Evaluate rules and execute actions (requires existing cache)")
    p_eval_exec.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    eval_exec_scope = p_eval_exec.add_mutually_exclusive_group()
    eval_exec_scope.add_argument("--all-folders", action="store_true", help="Process all cached folders")
    eval_exec_scope.add_argument(
        "--folder",
        action="append",
        help="Process only the specified folder(s)",
    )
    eval_exec_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Process the specified folder and all its subfolders recursively",
    )
    p_eval_exec.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops during execute")
    p_eval_exec.add_argument(
        "--verify-moves",
        action="store_true",
        help="Confirm successful moves by searching for Message-ID in source and destination mailboxes",
    )
    p_eval_exec.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress and log IMAP replies during evaluate/execute",
    )
    p_eval_exec.add_argument(
        "--debug-headers",
        action="store_true",
        help="Log message headers while evaluating rules",
    )
    p_eval_exec.add_argument(
        "--limit",
        type=int,
        help="Process at most this many pending actions during the execute phase",
    )
    p_eval_exec.add_argument(
        "--backup-moved",
        action="store_true",
        help="Backup messages before moving them during the execute phase (recommended)",
    )
    p_eval_exec.add_argument(
        "--backup-all",
        action="store_true",
        help="Backup all cached messages after execution completes (creates full archive)",
    )
    p_eval_exec.add_argument(
        "--parallel-workers",
        type=int,
        default=None,
        help=(
            "Number of parallel workers for execute phase. "
            "0=force sequential, N>0=force N workers, None=auto-detect "
            "(parallel if ≥5 folders, otherwise sequential). Default: None (auto-detect)"
        ),
    )
    p_eval_exec.add_argument(
        "--no-move",
        action="store_true",
        help="Skip all move actions during execution",
    )
    p_eval_exec.add_argument(
        "--no-keyword",
        action="store_true",
        help="Skip all keyword (set/remove flags) actions during execution",
    )
    p_eval_exec.add_argument(
        "--folder-order",
        dest="folder_order",
        choices=["most-first", "least-first", "alpha"],
        default="alpha",
        help="Order folders by destination match count during execute: most-first (most matches first), least-first, or alpha (default)",
    )

    p_stream = sub.add_parser(
        "stream",
        help="Stream-process: read message → evaluate rules → execute action (no cache needed)"
    )
    p_stream.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    stream_scope = p_stream.add_mutually_exclusive_group()
    stream_scope.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    stream_scope.add_argument(
        "--folder",
        action="append",
        help="Process only the specified folder(s)",
    )
    stream_scope.add_argument(
        "--folder-recursive",
        action="append",
        help="Process the specified folder and all its subfolders recursively",
    )
    p_stream.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress and log message matches",
    )
    p_stream.add_argument(
        "--limit",
        type=int,
        help="Process at most this many messages per folder",
    )
    p_stream.add_argument(
        "--backup-moved",
        action="store_true",
        help="Backup messages before moving them (recommended for safety)",
    )
    p_stream.add_argument(
        "--fresh",
        action="store_true",
        help="Clear the resume log before running so all emails are re-evaluated from scratch",
    )

    p_clear = sub.add_parser("clear-pending", help="Remove all pending actions without executing them")

    p_clear_cache = sub.add_parser(
        "clear-cache",
        help="Remove cached message headers and pending actions",
    )

    p_compact_cache = sub.add_parser(
        "compact-cache",
        help="Prune cached headers for messages that have already been handled",
    )

    p_keywords = sub.add_parser("keywords", help="Manage predefined keywords")
    kw_sub = p_keywords.add_subparsers(dest="kw_cmd", required=True)

    kw_sub.add_parser("list", help="List all predefined keywords")

    p_kw_add = kw_sub.add_parser("add", help="Add a new predefined keyword")
    p_kw_add.add_argument("keyword", help="Keyword to add")

    p_kw_remove = kw_sub.add_parser("remove", help="Remove a predefined keyword")
    p_kw_remove.add_argument("keyword", help="Keyword to remove")

    kw_sub.add_parser("edit", help="Edit keywords in default editor")

    p_conflicts = sub.add_parser("check-conflicts", help="Detect and resolve rule conflicts")
    p_conflicts.add_argument(
        "--validation-mode",
        choices=["cache", "static", "prompt"],
        default="prompt",
        help=(
            "Validation approach: "
            "'cache' uses real message counts (requires cache), "
            "'static' uses pattern analysis only, "
            "'prompt' asks user to choose (default)"
        ),
    )
    p_conflicts.add_argument(
        "--output",
        choices=["detailed", "summary", "json"],
        default="detailed",
        help="Output format (default: detailed)",
    )
    p_conflicts.add_argument(
        "--conflict-types",
        choices=["priority", "unreachable", "redundant", "all"],
        default="all",
        help="Types of conflicts to detect (default: all)",
    )
    p_conflicts.add_argument(
        "--severity",
        choices=["high", "medium", "low", "all"],
        default="all",
        help="Minimum severity to report (default: all)",
    )
    p_conflicts.add_argument(
        "--auto-fix",
        action="store_true",
        help="Enable interactive resolution workflow",
    )
    p_conflicts.add_argument(
        "--export",
        type=Path,
        help="Export conflict report to JSON file",
    )

    p_view_cache = sub.add_parser("view-cache", help="View email cache in interactive table")
    p_view_cache.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum emails to load (default: 1000)",
    )
    p_view_cache.add_argument(
        "--folder",
        type=str,
        help="Filter by folder name",
    )

    p_mbox = sub.add_parser(
        "mbox-import",
        help="Upload an MBOX file to IMAP, routing each message directly to its target folder via rules",
    )
    p_mbox.add_argument(
        "mbox_file",
        type=Path,
        help="Path to the MBOX file to import",
    )
    p_mbox.add_argument(
        "--default-folder",
        default="INBOX",
        help="Destination folder for messages that match no rule (default: INBOX)",
    )
    p_mbox.add_argument(
        "--dry-run",
        action="store_true",
        help="Show per-folder upload plan without uploading anything",
    )
    p_mbox.add_argument(
        "--verbose",
        action="store_true",
        help="Log per-message rule matching decisions",
    )
    p_mbox.add_argument(
        "--limit",
        type=int,
        help="Process only the first N messages from the MBOX file",
    )
    p_mbox.add_argument(
        "--no-preserve-flags",
        action="store_true",
        help="Upload all messages without flags (ignore mbox Status/X-Status headers)",
    )
    p_mbox.add_argument(
        "--error-mbox",
        type=Path,
        metavar="PATH",
        help="Append failed messages to this MBOX file for later retry",
    )
    p_mbox.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="Number of parallel IMAP connections for uploading (default: 1 = sequential)",
    )
    p_mbox.add_argument(
        "--no-move",
        action="store_true",
        help="Skip rule-based folder routing — upload all messages to the default folder",
    )
    p_mbox.add_argument(
        "--no-keyword",
        action="store_true",
        help="Accepted for consistency; mbox-import does not apply keyword actions",
    )
    p_mbox.add_argument(
        "--folder-order",
        dest="folder_order",
        choices=["most-first", "least-first", "alpha"],
        default="alpha",
        help="Order in which destination folders are uploaded: most-first (most messages first), least-first, or alpha (default)",
    )

    return parser


def _ensure_layout(cfg: AppConfig) -> None:
    """Ensure the filesystem layout required for the CLI exists."""
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.backup_dir.mkdir(parents=True, exist_ok=True)


def _normalize_folder_list(
    folders: Sequence[str] | str | None,
) -> list[str] | None:
    """Return a cleaned list of folder names or ``None`` if empty."""

    if folders is None:
        return None
    if isinstance(folders, str):
        value = folders.strip()
        return [value] if value else None

    cleaned = [folder for folder in (item.strip() for item in folders) if folder]
    return cleaned or None


def _resolve_scope_selection(
    *,
    all_folders: bool,
    folders: list[str] | None,
    default_scope: str,
) -> tuple[list[str] | None, str]:
    """Determine the folder filter and evaluation scope to use."""

    if all_folders:
        return None, "all"

    if folders:
        return folders, "all"

    normalized = (default_scope or "all").lower()
    if normalized not in {"all", "inbox"}:
        normalized = "all"
    return None, normalized


def _resolve_folders_with_recursion(
    *,
    client: imaplib.IMAP4,
    all_folders: bool,
    folders: list[str] | None,
    folders_recursive: list[str] | None,
    logger: JsonLogger,
) -> list[str] | None:
    """
    Resolve folder selection with recursive expansion support.

    Args:
        client: IMAP connection
        all_folders: If True, return None (use all folders from IMAP)
        folders: List of specific folder names
        folders_recursive: List of folder names to expand recursively
        logger: Logger for progress messages

    Returns:
        List of resolved folder names, or None if all_folders is True
    """
    if all_folders:
        return None

    # Start with explicitly specified folders
    resolved = set()
    if folders:
        resolved.update(folders)

    # Expand recursive folders
    if folders_recursive:
        expanded = expand_folders_recursive(client, folders_recursive, show_progress=True)
        resolved.update(expanded)
        if folders_recursive:
            logger.log(
                "INFO",
                "folders_expanded_recursively",
                {"requested": len(folders_recursive), "expanded": len(expanded)},
                console=f"📂 Expanded {len(folders_recursive)} folder(s) recursively to {len(expanded)} total folder(s)",
            )

    return sorted(list(resolved)) if resolved else None


def handle_build_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``build-cache`` command."""
    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        cfg.cache.limit = args.limit
        if args.order:
            cfg.cache.order = args.order

        # Get folders and sizes
        folder_sizes = None

        # Resolve folder selection (handles --all-folders, --folder, and --folder-recursive)
        selected = _normalize_folder_list(args.folder)
        recursive = _normalize_folder_list(args.folder_recursive)
        resolved_folders = _resolve_folders_with_recursion(
            client=client,
            all_folders=args.all_folders,
            folders=selected,
            folders_recursive=recursive,
            logger=logger,
        )

        if args.all_folders:
            folders = list_all_folders(client)
            # Get folder sizes for sorting and progress display
            folder_sizes = get_folder_sizes(client, folders)
        elif resolved_folders:
            folders = resolved_folders
            folder_sizes = None
        else:
            folders = [DEFAULT_INBOX]
            folder_sizes = None

        # Smart auto-detection: parallelize if 5+ folders
        # User can override with --parallel-workers
        parallel_workers = args.parallel_workers
        if parallel_workers is None:
            # Auto-detect: use parallelization for 5+ folders
            parallel_workers = 8 if len(folders) >= 5 else 1

        # Choose implementation based on worker count
        if parallel_workers > 1:
            # Distribute folders using load balancing for optimal worker utilization
            # This also splits mega-folders (>10k messages) across workers
            tasks = distribute_folders_for_load_balancing(
                folders,
                folder_sizes,
                parallel_workers
            )

            # Count how many tasks are splits vs regular
            num_splits = sum(1 for t in tasks if t[1] is not None)

            logger.log(
                "INFO",
                "folders_distributed_for_load_balancing",
                {"total_tasks": len(tasks), "mega_splits": num_splits, "workers": parallel_workers},
                console=f"📂 Distributed {len(folders)} folders into {len(tasks)} tasks (with {num_splits} mega-folder splits) across {parallel_workers} workers",
            )
            logger.log(
                "INFO",
                "cache_parallel_enabled",
                {"workers": parallel_workers, "original_folders": len(folders), "total_tasks": len(tasks)},
                console=f"🚀 Parallel cache building: {parallel_workers} workers for {len(tasks)} tasks",
            )
            timer, folders_count, msg_count = build_cache_parallel(
                cfg.paths.secrets_file,
                cfg.paths.cache_db,
                tasks,
                show_progress=cfg.logging.show_progress,
                logger=logger,
                limit=cfg.cache.limit,
                order=cfg.cache.order,
                max_workers=parallel_workers,
                folder_sizes=folder_sizes,
            )
            # Log cache completion summary notification
            logger.log(
                "INFO",
                "cache_summary",
                {
                    "folders": folders_count,
                    "messages": msg_count,
                    "elapsed_sec": timer.elapsed,
                },
            )
        else:
            logger.log(
                "INFO",
                "cache_sequential",
                {"folders": len(folders)},
                console=f"📂 Sequential cache building: {len(folders)} folders",
            )
            timer, folders_count, msg_count = build_cache(
                client,
                db,
                folders,
                show_progress=cfg.logging.show_progress,
                logger=logger,
                limit=cfg.cache.limit,
                order=cfg.cache.order,
                folder_sizes=folder_sizes,
            )
            # Log cache completion summary notification
            logger.log(
                "INFO",
                "cache_summary",
                {
                    "folders": folders_count,
                    "messages": msg_count,
                    "elapsed_sec": timer.elapsed,
                },
            )
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.abort, OSError, EOFError):
            # Server may have closed connection or lost socket during long caching
            # This is safe to ignore since we're exiting anyway
            pass
    return 0


def _expand_folders_from_db(db: sqlite3.Connection, recursive_folders: list[str]) -> list[str]:
    """
    Expand recursive folder patterns using the database.

    Args:
        db: Database connection
        recursive_folders: List of folder patterns to expand

    Returns:
        List of all matching folders
    """
    if not recursive_folders:
        return []

    cursor = db.cursor()
    expanded = set()

    for pattern in recursive_folders:
        # Find all folders that match exactly or start with pattern/
        cursor.execute(
            "SELECT DISTINCT name FROM folders WHERE name = ? OR name LIKE ? ESCAPE '\\'",
            (pattern, f"{pattern}/%"),
        )
        for (folder_name,) in cursor.fetchall():
            expanded.add(folder_name)

    return sorted(list(expanded))


def handle_evaluate(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``evaluate`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.logging.verbose = args.verbose
    selected = _normalize_folder_list(args.folder)
    recursive = _normalize_folder_list(args.folder_recursive)

    # Expand recursive folders using database
    expanded_recursive = _expand_folders_from_db(db, recursive) if recursive else []

    # Combine exact and recursive folders
    final_folders = None
    if selected or expanded_recursive:
        final_folders = list(set((selected or []) + expanded_recursive))

    eval_folders, scope = _resolve_scope_selection(
        all_folders=args.all_folders,
        folders=final_folders,
        default_scope=cfg.executor.default_run_scope,
    )
    rules = load_rules(cfg.paths.rules_dir, logger)
    evaluate_rules(
        db,
        rules,
        scope=scope,
        dry_run=cfg.executor.dry_run,
        show_progress=cfg.logging.show_progress,
        logger=logger,
        verbose=cfg.logging.verbose,
        debug_headers=args.debug_headers,
        folders=eval_folders,
        limit=getattr(args, "limit", None),
    )
    return 0


def _build_disabled_action_types(args: argparse.Namespace) -> set[str]:
    """Build the set of action types to skip based on --no-* flags."""
    disabled: set[str] = set()
    if getattr(args, "no_move", False):
        disabled.add("move")
    if getattr(args, "no_keyword", False):
        disabled.update({"set_keywords", "remove_keywords"})
    return disabled


def handle_execute(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``execute`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.limit
    cfg.executor.verify_moves = getattr(args, "verify_moves", False)
    cfg.logging.verbose = args.verbose
    selected = _normalize_folder_list(args.folder)
    recursive = _normalize_folder_list(args.folder_recursive)

    # Expand recursive folders using database
    expanded_recursive = _expand_folders_from_db(db, recursive) if recursive else []

    # Combine exact and recursive folders
    final_folders = None
    if selected or expanded_recursive:
        final_folders = list(set((selected or []) + expanded_recursive))

    exec_folders, _ = _resolve_scope_selection(
        all_folders=args.all_folders,
        folders=final_folders,
        default_scope=cfg.executor.default_run_scope,
    )

    # Get parallel_workers setting from CLI args
    parallel_workers = getattr(args, "parallel_workers", None)
    disabled_action_types = _build_disabled_action_types(args)
    folder_order = getattr(args, "folder_order", "alpha")

    # Determine which implementation to use
    if should_use_parallel_mode(cfg.paths.db_file, parallel_workers, logger):
        # Use parallel implementation
        timer, stats = execute_actions_parallel(
            secrets_path=cfg.paths.secrets_file,
            db_path=cfg.paths.db_file,
            show_progress=cfg.logging.show_progress,
            dry_run=cfg.executor.dry_run,
            strict=cfg.executor.strict,
            logger=logger,
            verbose=cfg.logging.verbose,
            limit=cfg.executor.limit,
            folders=exec_folders,
            verify_moves=cfg.executor.verify_moves,
            backup_moved=getattr(args, "backup_moved", False),
            backup_all=getattr(args, "backup_all", False),
            backup_dir=cfg.paths.backup_dir,
            max_workers=parallel_workers if parallel_workers and parallel_workers > 0 else 5,
            disabled_action_types=disabled_action_types,
            folder_order=folder_order,
        )
        # Log execute completion summary notification
        logger.log(
            "INFO",
            "execute_summary",
            {
                "done": stats.get("done", 0),
                "failed": stats.get("failed", 0),
                "skipped": stats.get("skipped", 0),
            },
        )
    else:
        # Use existing sequential implementation
        client = None if args.dry_run else imap_login(cfg.paths.secrets_file, logger)
        try:
            timer, stats = execute_actions(
                client,
                db,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
                verbose=cfg.logging.verbose,
                limit=cfg.executor.limit,
                folders=exec_folders,
                verify_moves=cfg.executor.verify_moves,
                backup_moved=getattr(args, "backup_moved", False),
                backup_all=getattr(args, "backup_all", False),
                backup_dir=cfg.paths.backup_dir,
                disabled_action_types=disabled_action_types,
                folder_order=folder_order,
            )
            # Log execute completion summary notification
            logger.log(
                "INFO",
                "execute_summary",
                {
                    "done": stats.get("done", 0),
                    "failed": stats.get("failed", 0),
                    "skipped": stats.get("skipped", 0),
                },
            )
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.abort, OSError, EOFError):
                    # Server may have closed connection or lost socket
                    # Safe to ignore during exit
                    pass
    return 0


def handle_run_all(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``run-all`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.action_limit
    cfg.executor.verify_moves = getattr(args, "verify_moves", False)
    cfg.logging.verbose = args.verbose
    cfg.cache.limit = args.cache_limit
    if args.cache_order:
        cfg.cache.order = args.cache_order
    run_timer = PhaseTimer("run-all")
    client = None if args.dry_run else imap_login(cfg.paths.secrets_file, logger)
    try:
        selected = _normalize_folder_list(args.folder)
        recursive = _normalize_folder_list(args.folder_recursive)

        # For build-cache phase, expand folders using IMAP
        if args.all_folders and client is not None:
            folders = list_all_folders(client)
        elif recursive and client is not None:
            expanded_recursive = expand_folders_recursive(client, recursive, show_progress=True)
            folders = list(set((selected or []) + expanded_recursive))
        else:
            folders = selected if selected else [DEFAULT_INBOX]

        # For evaluation phase, combine exact and recursive
        expanded_recursive_db = _expand_folders_from_db(db, recursive) if recursive else []
        final_eval_folders = None
        if selected or expanded_recursive_db:
            final_eval_folders = list(set((selected or []) + expanded_recursive_db))

        eval_folders, scope = _resolve_scope_selection(
            all_folders=args.all_folders,
            folders=final_eval_folders,
            default_scope=cfg.executor.default_run_scope,
        )
        _cache_timer, folders_count, msg_count = build_cache(
            client,
            db,
            folders,
            show_progress=cfg.logging.show_progress,
            logger=logger,
            limit=cfg.cache.limit,
            order=cfg.cache.order,
        )
        rules = load_rules(cfg.paths.rules_dir, logger)
        _eval_timer, rules_count, matches = evaluate_rules(
            db,
            rules,
            scope=scope,
            dry_run=cfg.executor.dry_run,
            show_progress=cfg.logging.show_progress,
            logger=logger,
            verbose=cfg.logging.verbose,
            debug_headers=args.debug_headers,
            folders=eval_folders,
        )
        # Get parallel_workers setting from CLI args
        parallel_workers = getattr(args, "parallel_workers", None)
        disabled_action_types = _build_disabled_action_types(args)
        folder_order = getattr(args, "folder_order", "alpha")

        # Determine which implementation to use for execute phase
        if should_use_parallel_mode(cfg.paths.db_file, parallel_workers, logger):
            # Use parallel implementation
            _exec_timer, stats = execute_actions_parallel(
                secrets_path=cfg.paths.secrets_file,
                db_path=cfg.paths.db_file,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
                verbose=cfg.logging.verbose,
                limit=cfg.executor.limit,
                folders=eval_folders,
                verify_moves=cfg.executor.verify_moves,
                backup_moved=getattr(args, "backup_moved", False),
                backup_all=getattr(args, "backup_all", False),
                backup_dir=cfg.paths.backup_dir,
                max_workers=parallel_workers if parallel_workers and parallel_workers > 0 else 5,
                disabled_action_types=disabled_action_types,
                folder_order=folder_order,
            )
        else:
            # Use existing sequential implementation
            _exec_timer, stats = execute_actions(
                client,
                db,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
                verbose=cfg.logging.verbose,
                limit=cfg.executor.limit,
                folders=eval_folders,
                verify_moves=cfg.executor.verify_moves,
                backup_moved=getattr(args, "backup_moved", False),
                backup_all=getattr(args, "backup_all", False),
                backup_dir=cfg.paths.backup_dir,
                disabled_action_types=disabled_action_types,
                folder_order=folder_order,
            )
        run_timer.stop()
        summary_context: dict[str, object] = {
            "duration_sec": run_timer.elapsed,
            "folders": folders_count,
            "messages": msg_count,
            "rules": rules_count,
            "matches": matches,
            **{f"exec_{key}": value for key, value in stats.items()},
            "strict": cfg.executor.strict,
            "dry_run": cfg.executor.dry_run,
        }
        logger.log(
            "INFO",
            "run_summary",
            summary_context,
            console=(
                "\n🏁 Run Summary\n"
                f"   🕒  Total runtime: {run_timer.fmt()}\n"
                f"   🗂️  Folders: {folders_count}\n"
                f"   ✉️  Messages: {msg_count}\n"
                f"   🧩  Rules: {rules_count}\n"
                f"   🎯  Matches: {matches}\n"
                f"   📦  Executed: {stats.get('done', 0)}  |  ⚠️ Skipped: {stats.get('skipped', 0)}  |  🚫 Suppressed: {stats.get('suppressed', 0)}  |  💥 Failed: {stats.get('failed', 0)}\n"
                f"   {'🔒 STRICT' if cfg.executor.strict else '✅ Completed'} {'(dry-run)' if cfg.executor.dry_run else ''}\n"
            ),
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.abort, OSError, EOFError):
                # Server may have closed connection or lost socket
                # Safe to ignore during exit
                pass
    return 0


def handle_eval_execute(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``eval-execute`` command (evaluate rules and execute actions without building cache)."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.limit
    cfg.executor.verify_moves = getattr(args, "verify_moves", False)
    cfg.logging.verbose = args.verbose
    run_timer = PhaseTimer("eval-execute")
    client = None if args.dry_run else imap_login(cfg.paths.secrets_file, logger)
    try:
        selected = _normalize_folder_list(args.folder)
        recursive = _normalize_folder_list(args.folder_recursive)

        # Expand recursive folders using database
        expanded_recursive = _expand_folders_from_db(db, recursive) if recursive else []

        # Combine exact and recursive folders
        final_folders = None
        if selected or expanded_recursive:
            final_folders = list(set((selected or []) + expanded_recursive))

        eval_folders, scope = _resolve_scope_selection(
            all_folders=args.all_folders,
            folders=final_folders,
            default_scope=cfg.executor.default_run_scope,
        )

        # Phase 1: Evaluate rules
        rules = load_rules(cfg.paths.rules_dir, logger)
        _eval_timer, rules_count, matches = evaluate_rules(
            db,
            rules,
            scope=scope,
            dry_run=cfg.executor.dry_run,
            show_progress=cfg.logging.show_progress,
            logger=logger,
            verbose=cfg.logging.verbose,
            debug_headers=args.debug_headers,
            folders=eval_folders,
        )

        # Phase 2: Execute actions
        parallel_workers = getattr(args, "parallel_workers", None)
        disabled_action_types = _build_disabled_action_types(args)
        folder_order = getattr(args, "folder_order", "alpha")

        # Determine which implementation to use for execute phase
        if should_use_parallel_mode(cfg.paths.db_file, parallel_workers, logger):
            # Use parallel implementation
            _exec_timer, stats = execute_actions_parallel(
                secrets_path=cfg.paths.secrets_file,
                db_path=cfg.paths.db_file,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
                verbose=cfg.logging.verbose,
                limit=cfg.executor.limit,
                folders=eval_folders,
                verify_moves=cfg.executor.verify_moves,
                backup_moved=getattr(args, "backup_moved", False),
                backup_all=getattr(args, "backup_all", False),
                backup_dir=cfg.paths.backup_dir,
                max_workers=parallel_workers if parallel_workers and parallel_workers > 0 else 5,
                disabled_action_types=disabled_action_types,
                folder_order=folder_order,
            )
        else:
            # Use existing sequential implementation
            _exec_timer, stats = execute_actions(
                client,
                db,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
                verbose=cfg.logging.verbose,
                limit=cfg.executor.limit,
                folders=eval_folders,
                verify_moves=cfg.executor.verify_moves,
                backup_moved=getattr(args, "backup_moved", False),
                backup_all=getattr(args, "backup_all", False),
                backup_dir=cfg.paths.backup_dir,
                disabled_action_types=disabled_action_types,
                folder_order=folder_order,
            )

        run_timer.stop()
        summary_context: dict[str, object] = {
            "duration_sec": run_timer.elapsed,
            "rules": rules_count,
            "matches": matches,
            **{f"exec_{key}": value for key, value in stats.items()},
            "strict": cfg.executor.strict,
            "dry_run": cfg.executor.dry_run,
        }
        logger.log(
            "INFO",
            "eval_execute_summary",
            summary_context,
            console=(
                "\n🔄 Eval-Execute Summary\n"
                f"   🕒  Total runtime: {run_timer.fmt()}\n"
                f"   🧩  Rules: {rules_count}\n"
                f"   🎯  Matches: {matches}\n"
                f"   📦  Executed: {stats.get('done', 0)}  |  ⚠️ Skipped: {stats.get('skipped', 0)}  |  🚫 Suppressed: {stats.get('suppressed', 0)}  |  💥 Failed: {stats.get('failed', 0)}\n"
                f"   {'🔒 STRICT' if cfg.executor.strict else '✅ Completed'} {'(dry-run)' if cfg.executor.dry_run else ''}\n"
            ),
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.abort, OSError, EOFError):
                # Server may have closed connection or lost socket
                # Safe to ignore during exit
                pass
    return 0


def handle_stream(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``stream`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.logging.verbose = args.verbose

    stream_timer = PhaseTimer("stream")

    # Determine which folders to process
    selected = _normalize_folder_list(args.folder)
    recursive = _normalize_folder_list(args.folder_recursive)

    if args.all_folders:
        client_init = imap_login(cfg.paths.secrets_file, logger)
        try:
            folders = list_all_folders(client_init)
        finally:
            client_init.logout()
    elif recursive:
        client_init = imap_login(cfg.paths.secrets_file, logger)
        try:
            expanded_recursive = expand_folders_recursive(client_init, recursive, show_progress=True)
            folders = list(set((selected or []) + expanded_recursive))
        finally:
            client_init.logout()
    elif selected:
        folders = selected
    else:
        folders = [DEFAULT_INBOX]

    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        # Load rules
        rules = load_rules(cfg.paths.rules_dir, logger)

        # Create resume log for tracking progress
        resume_log = create_resume_log(cfg.paths.log_file, logger=logger, session_id="default")

        if getattr(args, "fresh", False):
            resume_log.clear()
            logger.log(
                "INFO",
                "stream_resume_cleared",
                {},
                console="📋 Resume log cleared (--fresh) — re-evaluating all emails",
            )

        _limit = args.limit if hasattr(args, 'limit') else None

        # Pre-count messages for progress bar total (SELECT+SEARCH only, no header fetching)
        total_messages = count_stream_messages(client, folders, limit=_limit, resume_log=resume_log)

        # Stream messages and execute
        messages = stream_messages(
            client,
            folders,
            logger=logger,
            limit=_limit,
            resume_log=resume_log,
        )

        _exec_timer, stats = stream_execute(
            client,
            rules,
            messages,
            show_progress=cfg.logging.show_progress,
            dry_run=cfg.executor.dry_run,
            verbose=cfg.logging.verbose,
            backup_moved=getattr(args, "backup_moved", False),
            backup_dir=cfg.paths.backup_dir,
            resume_log=resume_log,
            logger=logger,
            total=total_messages,
        )

        stream_timer.stop()
        summary_context: dict[str, object] = {
            "duration_sec": stream_timer.elapsed,
            "folders": len(folders),
            "total_messages": stats.get("matched", 0) + stats.get("skipped", 0),
            "rules": len(rules),
            "matched": stats.get("matched", 0),
            **{f"stream_{key}": value for key, value in stats.items()},
            "dry_run": cfg.executor.dry_run,
        }
        status_text = (
            "✅ Completed"
            if stats.get("failed", 0) == 0
            else f"⚠️ {stats.get('failed', 0)} failed"
        )
        dry_run_text = "(dry-run)" if cfg.executor.dry_run else ""
        logger.log(
            "INFO",
            "stream_summary",
            summary_context,
            console=(
                "\n🏁 Stream Summary\n"
                f"   🕒  Total runtime: {stream_timer.fmt()}\n"
                f"   🗂️  Folders: {len(folders)}\n"
                f"   ✉️  Total messages: {stats.get('matched', 0) + stats.get('skipped', 0)}\n"
                f"   🧩  Rules: {len(rules)}\n"
                f"   🎯  Matched: {stats.get('matched', 0)}\n"
                f"   ✅ Moved: {stats.get('done', 0)}  |  ⊘ Skipped: {stats.get('skipped', 0)}  |  ❌ Failed: {stats.get('failed', 0)}\n"
                f"   {status_text} {dry_run_text}\n"
            ),
        )
    finally:
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.abort, OSError, EOFError):
                # Server may have closed connection or lost socket
                # Safe to ignore during exit
                pass
    return 0


def handle_clear_pending(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``clear-pending`` command."""

    del args, cfg  # Unused for now – kept for consistent handler signature

    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM actions WHERE status='pending'")
    (pending_count,) = cursor.fetchone()

    if pending_count == 0:
        logger.log(
            "INFO",
            "clear_pending_empty",
            {"removed": 0},
            console="ℹ️ No pending actions to clear",
        )
        return 0

    db.execute("DELETE FROM actions WHERE status='pending'")
    db.commit()

    logger.log(
        "INFO",
        "clear_pending_removed",
        {"removed": pending_count},
        console=f"🧹 Cleared {pending_count} pending action{'s' if pending_count != 1 else ''}",
    )

    return 0


def handle_clear_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``clear-cache`` command."""

    del args, cfg  # Unused – kept for consistent handler signature

    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM headers")
    headers_count = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COUNT(*) FROM actions")
    actions_count = cursor.fetchone()[0] or 0
    cursor.execute("SELECT COUNT(*) FROM folders")
    folders_count = cursor.fetchone()[0] or 0

    with db:
        db.execute("DELETE FROM headers")
        db.execute("DELETE FROM actions")
        db.execute("DELETE FROM folders")

    logger.log(
        "INFO",
        "cache_cleared",
        {
            "headers": headers_count,
            "actions": actions_count,
            "folders": folders_count,
        },
        console=(
            "🗑️ Cleared cache"
            f" — headers: {headers_count}, actions: {actions_count}, folders: {folders_count}"
        ),
    )

    return 0


def handle_compact_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``compact-cache`` command."""

    del args, cfg  # Unused – kept for consistent handler signature

    compact_cache(db, logger=logger)

    return 0


def handle_keywords(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``keywords`` command for managing predefined keywords."""

    del db, logger  # Unused – kept for consistent handler signature

    keyword_manager = KeywordManager(cfg.paths.data_dir)

    if args.kw_cmd == "list":
        keywords = keyword_manager.get_keywords()
        if not keywords:
            print("No predefined keywords found.")
        else:
            print("Predefined Keywords:")
            for i, kw in enumerate(keywords, 1):
                print(f"  {i}. {kw}")
        return 0

    elif args.kw_cmd == "add":
        keyword = args.keyword.strip()
        if not keyword:
            print("Error: Keyword cannot be empty")
            return 1

        if keyword_manager.add_keyword(keyword):
            print(f"✓ Added keyword: {keyword}")
            return 0
        else:
            print(f"⚠️  Keyword already exists: {keyword}")
            return 1

    elif args.kw_cmd == "remove":
        keyword = args.keyword.strip()
        if not keyword:
            print("Error: Keyword cannot be empty")
            return 1

        if keyword_manager.remove_keyword(keyword):
            print(f"✓ Removed keyword: {keyword}")
            return 0
        else:
            print(f"⚠️  Keyword not found: {keyword}")
            return 1

    elif args.kw_cmd == "edit":
        import subprocess
        import os

        # Try to open the config file with the default editor
        config_file = cfg.paths.data_dir / "keywords.json"
        editor = None

        # Check environment variables first
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")

        # Fallback to common editors if no env var set
        if not editor:
            for cmd in ["nano", "vi", "vim", "gedit", "code"]:
                try:
                    result = subprocess.run(["which", cmd], capture_output=True, check=False)
                    if result.returncode == 0:
                        editor = cmd
                        break
                except (OSError, subprocess.CalledProcessError):
                    pass

        if not editor:
            print(f"Could not find an editor. Please edit manually: {config_file}")
            print("Format:")
            print('  {"predefined_keywords": ["keyword1", "keyword2", ...]}\n')
            return 1

        try:
            subprocess.run([editor, str(config_file)], check=False)
            return 0
        except (OSError, subprocess.CalledProcessError) as e:
            print(f"Error opening editor: {e}")
            return 1

    return 1


def handle_view_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``view-cache`` command for interactive cache viewing."""

    del db, logger  # Unused – kept for consistent handler signature

    from core.tools.cache_viewer import launch_cache_viewer

    return launch_cache_viewer(cfg, limit=args.limit, folder=args.folder)


def handle_mbox_import(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``mbox-import`` command."""
    from core.mbox_importer import run_mbox_import

    del db  # mbox-import does not use the local cache

    mbox_path = Path(args.mbox_file)
    if not mbox_path.exists():
        logger.log(
            "ERROR",
            "mbox_file_not_found",
            {"path": str(mbox_path)},
            console=f"❌ MBOX file not found: {mbox_path}",
        )
        return 1

    return run_mbox_import(
        mbox_path=mbox_path,
        rules_dir=cfg.paths.rules_dir,
        secrets_path=cfg.paths.secrets_file,
        default_folder=args.default_folder,
        dry_run=args.dry_run,
        verbose=args.verbose,
        limit=args.limit,
        preserve_flags=not args.no_preserve_flags,
        error_mbox_path=getattr(args, "error_mbox", None),
        logger=logger,
        parallel_workers=getattr(args, "parallel_workers", 1),
        no_move=getattr(args, "no_move", False),
        folder_order=getattr(args, "folder_order", "alpha"),
    )


def handle_check_conflicts(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``check-conflicts`` command for conflict detection and resolution."""

    del db  # Unused – kept for consistent handler signature

    import json

    from core.conflict_detector import ConflictDetector, ConflictSeverity, ConflictType

    # Load rules
    rules = load_rules(cfg.paths.rules_dir, logger)

    if not rules:
        logger.log(
            "WARN",
            "no_rules_found",
            {},
            console="⚠️  No rules found to check",
        )
        return 1

    # Determine validation mode
    validation_mode = args.validation_mode
    if validation_mode == "prompt":
        cache_exists = cfg.paths.db_file.exists()
        if cache_exists:
            use_cache = input(
                "\n📦 Cache database found. Use real message counts for validation? [Y/n]: "
            ).lower()
            use_cache = use_cache != "n" and use_cache != "no"
            validation_mode = "cache" if use_cache else "static"
        else:
            logger.log(
                "INFO",
                "cache_not_found",
                {},
                console="ℹ️  Cache not found. Using static analysis.",
            )
            validation_mode = "static"

    # Initialize detector
    cache_db = cfg.paths.db_file if validation_mode == "cache" else None
    detector = ConflictDetector(rules, cache_db, logger)

    # Detect conflicts
    logger.log(
        "INFO",
        "conflict_detection_start",
        {"rule_count": len(rules), "mode": validation_mode},
        console=f"🔍 Analyzing {len(rules)} rules (mode: {validation_mode})...",
    )

    all_conflicts = detector.detect_all_conflicts()

    # Filter by conflict type
    if args.conflict_types != "all":
        type_map = {
            "priority": ConflictType.PRIORITY_CONFLICT,
            "unreachable": ConflictType.UNREACHABLE,
            "redundant": ConflictType.REDUNDANT,
        }
        target_type = type_map.get(args.conflict_types)
        conflicts = [c for c in all_conflicts if c.type == target_type]
    else:
        conflicts = all_conflicts

    # Filter by severity
    if args.severity != "all":
        severity_map = {
            "high": ConflictSeverity.HIGH,
            "medium": ConflictSeverity.MEDIUM,
            "low": ConflictSeverity.LOW,
        }
        min_severity = severity_map.get(args.severity, ConflictSeverity.LOW)
        severity_order = {ConflictSeverity.HIGH: 0, ConflictSeverity.MEDIUM: 1, ConflictSeverity.LOW: 2}
        conflicts = [c for c in conflicts if severity_order[c.severity] <= severity_order[min_severity]]

    # Display results
    if not conflicts:
        logger.log(
            "INFO",
            "no_conflicts_found",
            {},
            console="✅ No conflicts detected! Rules look good.",
        )
        return 0

    # Output based on format
    if args.output == "json":
        json_data = [c.to_dict() for c in conflicts]
        output = json.dumps(json_data, indent=2)

        if args.export:
            args.export.write_text(output)
            logger.log(
                "INFO",
                "conflicts_exported",
                {"file": str(args.export), "count": len(conflicts)},
                console=f"✓ Exported {len(conflicts)} conflicts to {args.export}",
            )
        else:
            print(output)

    elif args.output == "summary":
        print(f"\n📊 Conflict Summary ({len(conflicts)} found):")
        by_type = {}
        for c in conflicts:
            type_name = c.type.value
            if type_name not in by_type:
                by_type[type_name] = []
            by_type[type_name].append(c)

        for type_name, type_conflicts in by_type.items():
            print(f"\n  {type_name.replace('_', ' ').title()} ({len(type_conflicts)}):")
            for c in type_conflicts:
                severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}[c.severity.value]
                print(f"    {severity_icon} {c.rule1_name} ↔ {c.rule2_name}")
                if c.reason:
                    print(f"       → {c.reason}")

    else:  # detailed
        _output_detailed_conflicts(conflicts, validation_mode == "cache")

    # Interactive resolution
    if args.auto_fix and conflicts:
        from core.conflict_detector import ConflictResolver

        resolver = ConflictResolver(conflicts, cfg.paths.rules_dir, logger)
        resolver.interactive_resolve()

        # Log applied fixes
        if resolver.applied_fixes:
            logger.log(
                "INFO",
                "conflicts_resolved",
                {"count": len(resolver.applied_fixes)},
                console=f"✓ Applied {len(resolver.applied_fixes)} fix{'es' if len(resolver.applied_fixes) != 1 else ''}",
            )

    logger.log(
        "INFO",
        "conflict_detection_complete",
        {"count": len(conflicts), "mode": validation_mode},
        console=f"⚠️  Found {len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''}",
    )

    return 0 if not conflicts else 1


def _output_detailed_conflicts(conflicts, cache_available):
    """Output detailed conflict report.

    Args:
        conflicts: List of ConflictResult objects
        cache_available: Whether cache data was used
    """
    print("\n" + "=" * 80)
    print("RULE CONFLICT ANALYSIS REPORT")
    print("=" * 80)

    # Count by severity
    high = sum(1 for c in conflicts if c.severity.value == "high")
    medium = sum(1 for c in conflicts if c.severity.value == "medium")
    low = sum(1 for c in conflicts if c.severity.value == "low")

    print(f"\n📊 Summary:")
    print(f"   Total Conflicts: {len(conflicts)}")
    print(f"   Validation: {'cache (real message counts)' if cache_available else 'static analysis'}")
    if high > 0:
        print(f"   🔴 High Severity: {high}")
    if medium > 0:
        print(f"   🟡 Medium Severity: {medium}")
    if low > 0:
        print(f"   🟢 Low Severity: {low}")

    # Display each conflict
    for i, conflict in enumerate(conflicts, 1):
        severity_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}[conflict.severity.value]
        type_display = conflict.type.value.replace("_", " ").title()

        print(f"\n{severity_icon} CONFLICT #{i} — {type_display} ({conflict.severity.value.upper()})")
        print(f"\n   Rules:")
        print(f"      [{conflict.rule1_priority}] {conflict.rule1_name}")
        print(f"      [{conflict.rule2_priority}] {conflict.rule2_name}")
        print(f"\n   Overlap: {conflict.overlap_percent:.0%} ({conflict.overlap_relationship.value})")

        if conflict.affected_count is not None:
            print(f"   Affected Messages: {conflict.affected_count}")

        if conflict.reason:
            print(f"\n   Why: {conflict.reason}")

        print(f"\n   Issue: {conflict.explanation}")
        print(f"\n   💡 Suggestion: {conflict.suggestion}")

    print("\n" + "=" * 80)


COMMAND_HANDLERS: dict[str, Handler] = {
    "build-cache": handle_build_cache,
    "evaluate": handle_evaluate,
    "execute": handle_execute,
    "run-all": handle_run_all,
    "eval-execute": handle_eval_execute,
    "stream": handle_stream,
    "clear-pending": handle_clear_pending,
    "clear-cache": handle_clear_cache,
    "compact-cache": handle_compact_cache,
    "keywords": handle_keywords,
    "check-conflicts": handle_check_conflicts,
    "view-cache": handle_view_cache,
    "mbox-import": handle_mbox_import,
}


def main(argv: Sequence[str] | None = None, *, base_dir: Path | None = None) -> int:
    """Entry point for the CLI."""
    import json
    from core.notifications import GotifyNotifier, NotificationDispatcher

    parser = build_parser()
    args = parser.parse_args(argv)

    # Build config with optional cache override
    cfg = build_default_config(
        base_dir=base_dir,
        cache_override=getattr(args, "cache_file", None)
    )
    _ensure_layout(cfg)

    # Initialize notification dispatcher with GOTIFY if configured
    notifier = None
    if getattr(args, "no_gotify", False):
        print("ℹ️  GOTIFY notifications disabled via --no-gotify")
    else:
        try:
            secrets_path = cfg.paths.secrets_file
            if secrets_path.exists():
                with open(secrets_path, encoding="utf-8") as f:
                    secrets = json.load(f)
                    gotify_cfg = secrets.get("notifications", {}).get("gotify", {})
                    if gotify_cfg.get("enabled"):
                        base_url = gotify_cfg.get("base_url", "")
                        token = gotify_cfg.get("token", "")
                        if not base_url or not token:
                            print("⚠️  GOTIFY configured but missing base_url or token")
                        else:
                            gotify = GotifyNotifier(
                                base_url=base_url,
                                token=token,
                                max_timeout_failures=gotify_cfg.get("max_timeout_failures", 3),
                            )
                            notifier = NotificationDispatcher(gotify_notifier=gotify)
                            print(f"✅ GOTIFY initialized: {base_url}")
                    else:
                        print("ℹ️  GOTIFY is disabled in configuration")
            else:
                print(f"ℹ️  Secrets file not found: {secrets_path}")
        except Exception as e:
            # Log notification setup errors but don't let them break mail processing
            print(f"⚠️  GOTIFY setup error: {e}")

    logger = JsonLogger(cfg.paths.log_file, notifier=notifier)

    # Validate cache access based on command
    read_only_commands = {"evaluate", "execute", "eval-execute", "check-conflicts", "view-cache"}
    write_commands = {"build-cache", "run-all", "stream"}

    try:
        if args.cmd in read_only_commands:
            _validate_cache_access(cfg.paths.db_file, require_exists=True, logger=logger)
        elif args.cmd in write_commands:
            _validate_cache_access(cfg.paths.db_file, require_exists=False, logger=logger)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1

    db = init_db(cfg.paths.db_file, logger=logger)

    try:
        handler = COMMAND_HANDLERS.get(args.cmd)
        if handler is None:
            parser.error(f"Unknown command: {args.cmd}")
            return 2
        return handler(args, cfg, db, logger)
    finally:
        db.close()


__all__ = [
    "DEFAULT_INBOX",
    "build_parser",
    "main",
    "handle_build_cache",
    "handle_evaluate",
    "handle_execute",
    "handle_run_all",
    "handle_stream",
    "handle_clear_pending",
    "handle_clear_cache",
    "handle_compact_cache",
    "handle_keywords",
    "handle_check_conflicts",
    "handle_view_cache",
    "handle_mbox_import",
]
