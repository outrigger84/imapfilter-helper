#!/usr/bin/env python3
"""Command-line entry point for the IMAPFilter helper."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from core.cache_builder import build_cache
from core.config import build_default_config
from core.database import init_db
from core.executor import execute_actions
from core.imap_client import imap_login, list_all_folders
from core.logging_utils import JsonLogger, PhaseTimer
from core.rule_engine import evaluate_rules, load_rules


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IMAPFilter Helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build-cache", help="Build local message cache")
    p_build.add_argument("--all-folders", action="store_true", help="Scan all folders")

    p_eval = sub.add_parser("evaluate", help="Evaluate rules against cache")
    p_eval.add_argument("--dry-run", action="store_true", help="Simulate rule matches only")

    p_exec = sub.add_parser("execute", help="Execute queued actions")
    p_exec.add_argument("--dry-run", action="store_true", help="Simulate execution only (no IMAP writes)")
    p_exec.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops")

    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    p_run.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    p_run.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops during execute")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    base_dir = Path(__file__).parent.resolve()
    cfg = build_default_config(base_dir)
    logger = JsonLogger(cfg.paths.log_file)
    db = init_db(cfg.paths.db_file, logger=logger)

    if args.cmd == "build-cache":
        client = imap_login(cfg.paths.secrets_file, logger)
        try:
            folders = list_all_folders(client) if args.all_folders else ["INBOX"]
            build_cache(client, db, folders, show_progress=cfg.logging.show_progress, logger=logger)
        finally:
            client.logout()
        return 0

    if args.cmd == "evaluate":
        cfg.executor.dry_run = args.dry_run
        rules = load_rules(cfg.paths.rules_dir, logger)
        evaluate_rules(
            db,
            rules,
            scope=cfg.executor.default_run_scope,
            dry_run=cfg.executor.dry_run,
            show_progress=cfg.logging.show_progress,
            logger=logger,
        )
        return 0

    if args.cmd == "execute":
        cfg.executor.dry_run = args.dry_run
        cfg.executor.strict = args.strict
        client = None if args.dry_run else imap_login(cfg.paths.secrets_file, logger)
        try:
            execute_actions(
                client,
                db,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
            )
        finally:
            if client is not None:
                client.logout()
        return 0

    if args.cmd == "run-all":
        cfg.executor.dry_run = args.dry_run
        cfg.executor.strict = args.strict
        run_timer = PhaseTimer("run-all")
        client = imap_login(cfg.paths.secrets_file, logger)
        try:
            folders = list_all_folders(client) if args.all_folders else ["INBOX"]
            _cache_timer, folders_count, msg_count = build_cache(client, db, folders, show_progress=cfg.logging.show_progress, logger=logger)
            rules = load_rules(cfg.paths.rules_dir, logger)
            _eval_timer, rules_count, matches = evaluate_rules(
                db,
                rules,
                scope=cfg.executor.default_run_scope,
                dry_run=cfg.executor.dry_run,
                show_progress=cfg.logging.show_progress,
                logger=logger,
            )
            _exec_timer, stats = execute_actions(
                client,
                db,
                show_progress=cfg.logging.show_progress,
                dry_run=cfg.executor.dry_run,
                strict=cfg.executor.strict,
                logger=logger,
            )
            run_timer.stop()
            summary_context = {
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

    parser.error(f"Unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
