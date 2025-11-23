"""Command-line interface helpers for the IMAPFilter helper."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

from core.cache_builder import build_cache, compact_cache
from core.config import AppConfig, build_default_config
from core.database import init_db
from core.executor import execute_actions
from core.imap_client import imap_login, list_all_folders
from core.logging_utils import JsonLogger, PhaseTimer
from core.rule_engine import evaluate_rules, load_rules


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
        "--backup",
        action="store_true",
        help="Also export cached messages as mbox backups",
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
        "--limit",
        type=int,
        help="Process at most this many pending actions during the execute phase",
    )
    p_run.add_argument(
        "--cache-limit",
        type=int,
        help="Cache at most this many messages per folder during the cache phase",
    )
    p_run.add_argument(
        "--cache-order",
        choices=["newest", "oldest", "random"],
        help="When limiting cache, choose which messages to keep (default: newest)",
    )
    p_run.add_argument(
        "--backup",
        action="store_true",
        help="Export cached messages as mbox backups during the cache phase",
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
        cfg.cache.backup_enabled = bool(args.backup)
        if args.all_folders:
            folders = list_all_folders(client)
        else:
            selected = _normalize_folder_list(args.folder)
            folders = selected if selected else [DEFAULT_INBOX]
        build_cache(
            client,
            db,
            folders,
            show_progress=cfg.logging.show_progress,
            logger=logger,
            limit=cfg.cache.limit,
            order=cfg.cache.order,
            backup_enabled=cfg.cache.backup_enabled,
            backup_dir=cfg.paths.backup_dir,
        )
    finally:
        client.logout()
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
        )
    finally:
        if client is not None:
            client.logout()
    return 0


def handle_run_all(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``run-all`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.limit
    cfg.executor.verify_moves = getattr(args, "verify_moves", False)
    cfg.logging.verbose = args.verbose
    cfg.cache.limit = args.cache_limit
    if args.cache_order:
        cfg.cache.order = args.cache_order
    cfg.cache.backup_enabled = bool(args.backup)
    run_timer = PhaseTimer("run-all")
    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        selected = _normalize_folder_list(args.folder)
        if args.all_folders:
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
            backup_enabled=cfg.cache.backup_enabled,
            backup_dir=cfg.paths.backup_dir,
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
        client.logout()
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


COMMAND_HANDLERS: dict[str, Handler] = {
    "build-cache": handle_build_cache,
    "evaluate": handle_evaluate,
    "execute": handle_execute,
    "run-all": handle_run_all,
    "clear-pending": handle_clear_pending,
    "clear-cache": handle_clear_cache,
    "compact-cache": handle_compact_cache,
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
    "handle_clear_pending",
    "handle_clear_cache",
    "handle_compact_cache",
]
