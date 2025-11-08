"""Command-line interface helpers for the IMAPFilter helper."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Callable, Sequence

from core.cache_builder import build_cache
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
    p_build.add_argument("--all-folders", action="store_true", help="Scan all folders")

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

    p_exec = sub.add_parser("execute", help="Execute queued actions")
    p_exec.add_argument("--dry-run", action="store_true", help="Simulate execution only (no IMAP writes)")
    p_exec.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops")
    p_exec.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress for each moved message",
    )
    p_exec.add_argument(
        "--limit",
        type=int,
        help="Process at most this many pending actions during execution",
    )

    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    p_run.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    p_run.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops during execute")
    p_run.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress for evaluate/execute phases",
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

    p_clear = sub.add_parser("clear-pending", help="Remove all pending actions without executing them")

    return parser


def _ensure_layout(cfg: AppConfig) -> None:
    """Ensure the filesystem layout required for the CLI exists."""
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.log_file.parent.mkdir(parents=True, exist_ok=True)


def handle_build_cache(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``build-cache`` command."""
    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        folders = list_all_folders(client) if args.all_folders else [DEFAULT_INBOX]
        build_cache(
            client,
            db,
            folders,
            show_progress=cfg.logging.show_progress,
            logger=logger,
        )
    finally:
        client.logout()
    return 0


def handle_evaluate(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``evaluate`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.logging.verbose = args.verbose
    rules = load_rules(cfg.paths.rules_dir, logger)
    evaluate_rules(
        db,
        rules,
        scope=cfg.executor.default_run_scope,
        dry_run=cfg.executor.dry_run,
        show_progress=cfg.logging.show_progress,
        logger=logger,
        verbose=cfg.logging.verbose,
        debug_headers=args.debug_headers,
    )
    return 0


def handle_execute(args: argparse.Namespace, cfg: AppConfig, db, logger: JsonLogger) -> int:
    """Handle the ``execute`` command."""
    cfg.executor.dry_run = args.dry_run
    cfg.executor.strict = args.strict
    cfg.executor.limit = args.limit
    cfg.logging.verbose = args.verbose
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
    cfg.logging.verbose = args.verbose
    run_timer = PhaseTimer("run-all")
    client = imap_login(cfg.paths.secrets_file, logger)
    try:
        folders = list_all_folders(client) if args.all_folders else [DEFAULT_INBOX]
        _cache_timer, folders_count, msg_count = build_cache(
            client,
            db,
            folders,
            show_progress=cfg.logging.show_progress,
            logger=logger,
        )
        rules = load_rules(cfg.paths.rules_dir, logger)
        _eval_timer, rules_count, matches = evaluate_rules(
            db,
            rules,
            scope=cfg.executor.default_run_scope,
            dry_run=cfg.executor.dry_run,
            show_progress=cfg.logging.show_progress,
            logger=logger,
            verbose=cfg.logging.verbose,
            debug_headers=args.debug_headers,
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


COMMAND_HANDLERS: dict[str, Handler] = {
    "build-cache": handle_build_cache,
    "evaluate": handle_evaluate,
    "execute": handle_execute,
    "run-all": handle_run_all,
    "clear-pending": handle_clear_pending,
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
]
