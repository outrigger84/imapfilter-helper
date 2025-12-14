"""Command-line interface helpers for the IMAPFilter helper."""
from __future__ import annotations

import argparse
import imaplib
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

from core.cache_builder import build_cache, build_cache_parallel, compact_cache
from core.config import AppConfig, build_default_config
from core.database import init_db
from core.executor import execute_actions, execute_actions_parallel, should_use_parallel_mode
from core.imap_client import imap_login, list_all_folders, get_folder_sizes
from core.keywords import KeywordManager
from core.logging_utils import JsonLogger, PhaseTimer
from core.rule_engine import evaluate_rules, load_rules
from core.stream_processor import stream_messages
from core.stream_executor import stream_execute
from core.stream_resume import create_resume_log


Handler = Callable[[argparse.Namespace, AppConfig, sqlite3.Connection, JsonLogger], int]
DEFAULT_INBOX = "INBOX"


def build_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(description="IMAPFilter Helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build-cache", help="Build local message cache")
    build_scope = p_build.add_mutually_exclusive_group()
    build_scope.add_argument("--all-folders", action="store_true", help="Scan all folders")
    build_scope.add_argument(
        "--folder",
        action="append",
        help="Scan only the specified folder (can be repeated)",
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

    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    run_scope = p_run.add_mutually_exclusive_group()
    run_scope.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    run_scope.add_argument(
        "--folder",
        action="append",
        help="Process only the specified folder(s) during cache build",
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


def handle_build_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``build-cache`` command."""
    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        cfg.cache.limit = args.limit
        if args.order:
            cfg.cache.order = args.order

        # Get folders and sizes
        folder_sizes = None
        if args.all_folders:
            folders = list_all_folders(client)
            # Get folder sizes for sorting and progress display
            folder_sizes = get_folder_sizes(client, folders)
            # Sort folders by message count (smallest to largest)
            folders = sorted(folders, key=lambda f: folder_sizes.get(f, -1))
            logger.log(
                "INFO",
                "folders_sorted_by_size",
                {"total": len(folders), "method": "IMAP STATUS"},
                console=f"📂 Sorted {len(folders)} folders by size (smallest to largest)"
            )
        else:
            selected = _normalize_folder_list(args.folder)
            folders = selected if selected else [DEFAULT_INBOX]

        # Smart auto-detection: parallelize if 5+ folders
        # User can override with --parallel-workers
        parallel_workers = args.parallel_workers
        if parallel_workers is None:
            # Auto-detect: use parallelization for 5+ folders
            parallel_workers = 5 if len(folders) >= 5 else 1

        # Choose implementation based on worker count
        if parallel_workers > 1:
            logger.log(
                "INFO",
                "cache_parallel_enabled",
                {"workers": parallel_workers, "folders": len(folders)},
                console=f"🚀 Parallel cache building: {parallel_workers} workers for {len(folders)} folders",
            )
            build_cache_parallel(
                cfg.paths.secrets_file,
                cfg.paths.cache_db,
                folders,
                show_progress=cfg.logging.show_progress,
                logger=logger,
                limit=cfg.cache.limit,
                order=cfg.cache.order,
                max_workers=parallel_workers,
                folder_sizes=folder_sizes,
            )
        else:
            logger.log(
                "INFO",
                "cache_sequential",
                {"folders": len(folders)},
                console=f"📂 Sequential cache building: {len(folders)} folders",
            )
            build_cache(
                client,
                db,
                folders,
                show_progress=cfg.logging.show_progress,
                logger=logger,
                limit=cfg.cache.limit,
                order=cfg.cache.order,
                folder_sizes=folder_sizes,
            )
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.abort, OSError, EOFError):
            # Server may have closed connection or lost socket during long caching
            # This is safe to ignore since we're exiting anyway
            pass
    return 0


def handle_evaluate(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``evaluate`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.logging.verbose = args.verbose
    selected = _normalize_folder_list(args.folder)
    eval_folders, scope = _resolve_scope_selection(
        all_folders=args.all_folders,
        folders=selected,
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
    )
    return 0


def handle_execute(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``execute`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.limit
    cfg.executor.verify_moves = getattr(args, "verify_moves", False)
    cfg.logging.verbose = args.verbose
    selected = _normalize_folder_list(args.folder)
    exec_folders, _ = _resolve_scope_selection(
        all_folders=args.all_folders,
        folders=selected,
        default_scope=cfg.executor.default_run_scope,
    )

    # Get parallel_workers setting from CLI args
    parallel_workers = getattr(args, "parallel_workers", None)

    # Determine which implementation to use
    if should_use_parallel_mode(cfg.paths.db_file, parallel_workers, logger):
        # Use parallel implementation
        execute_actions_parallel(
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
        )
    else:
        # Use existing sequential implementation
        client = None if args.dry_run else imap_login(cfg.paths.secrets_file, logger)
        try:
            execute_actions(
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
        if args.all_folders and client is not None:
            folders = list_all_folders(client)
        else:
            folders = selected if selected else [DEFAULT_INBOX]
        eval_folders, scope = _resolve_scope_selection(
            all_folders=args.all_folders,
            folders=selected,
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


def handle_stream(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``stream`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.logging.verbose = args.verbose

    stream_timer = PhaseTimer("stream")

    # Determine which folders to process
    selected = _normalize_folder_list(args.folder)
    if args.all_folders:
        client_init = imap_login(cfg.paths.secrets_file, logger)
        try:
            folders = list_all_folders(client_init)
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

        # Stream messages and execute
        messages = stream_messages(
            client,
            folders,
            logger=logger,
            limit=args.limit if hasattr(args, 'limit') else None,
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


COMMAND_HANDLERS: dict[str, Handler] = {
    "build-cache": handle_build_cache,
    "evaluate": handle_evaluate,
    "execute": handle_execute,
    "run-all": handle_run_all,
    "stream": handle_stream,
    "clear-pending": handle_clear_pending,
    "clear-cache": handle_clear_cache,
    "compact-cache": handle_compact_cache,
    "keywords": handle_keywords,
    "view-cache": handle_view_cache,
}


def main(argv: Sequence[str] | None = None, *, base_dir: Path | None = None) -> int:
    """Entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = build_default_config(base_dir)
    _ensure_layout(cfg)

    logger = JsonLogger(cfg.paths.log_file)
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
    "handle_view_cache",
]
