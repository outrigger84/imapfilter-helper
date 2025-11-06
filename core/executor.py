"""Execute queued actions."""
from __future__ import annotations

import imaplib
from typing import Dict, List, Tuple

from tqdm import tqdm

from core.logging_utils import JsonLogger, now_iso
from core.timers import PhaseTimer


ActionRow = Tuple[int, str, str, str, str, int, str, str]


def execute_actions(client, db, cfg, logger: JsonLogger) -> tuple[PhaseTimer, Dict[str, int]]:
    show = cfg.logging.show_progress
    strict = cfg.executor.strict
    dry = cfg.executor.dry_run
    timer = PhaseTimer("execute")

    cur = db.cursor()
    cur.execute(
        "SELECT id, uid, folder, target, rule_name, priority, status, created_at "
        "FROM actions WHERE status='pending' ORDER BY priority DESC, created_at ASC"
    )
    actions: List[ActionRow] = cur.fetchall()

    if not actions:
        logger.log("INFO", "execute_nothing", {"dry_run": dry}, console="ℹ️ No pending actions")
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    chosen: List[ActionRow] = []
    seen_uids: set[str] = set()
    suppressed = 0

    for row in actions:
        a_id, uid, folder, target, rule_name, priority, status, created_at = row
        if uid in seen_uids:
            suppressed += 1
            if not dry:
                db.execute("UPDATE actions SET status='suppressed', executed_at=? WHERE id=?", (now_iso(), a_id))
            logger.log(
                "INFO",
                "duplicate_action_suppressed",
                {"uid": uid, "rule": rule_name, "priority": priority, "target": target},
            )
            continue
        seen_uids.add(uid)
        chosen.append(row)

    if not dry:
        db.commit()

    grouped: Dict[tuple[str, str], List[Tuple[int, str, str, int]]] = {}
    for a_id, uid, folder, target, rule_name, priority, status, created_at in chosen:
        grouped.setdefault((folder, target), []).append((a_id, uid, rule_name, priority))

    folders_bar = tqdm(
        list(grouped.items()),
        desc="📦 Executing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show,
    )

    stats = {"done": 0, "skipped": 0, "failed": 0, "suppressed": suppressed}

    for (folder, target), items in folders_bar:
        folders_bar.set_postfix_str(f"{folder} → {target}")
        uids = [uid for _, uid, _, _ in items]

        msgs_bar = tqdm(
            uids,
            desc=f"   🚚 Moving {folder}",
            unit="msg",
            dynamic_ncols=True,
            leave=False,
            position=1,
            disable=not show,
        )

        if dry:
            logger.log(
                "INFO",
                "dry_action_group",
                {"folder": folder, "target": target, "count": len(uids)},
                console=f"🧪 Dry run: {folder} → {target} ({len(uids)})",
            )
            continue

        try:
            sel_typ, _ = client.select(f'"{folder}"')
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            uid_to_action = {uid: a_id for (a_id, uid, _, _) in items}

            for uid in msgs_bar:
                a_id = uid_to_action[uid]
                try:
                    typ1, _ = client.uid("COPY", uid, f'"{target}"')
                    if typ1 != "OK":
                        raise imaplib.IMAP4.error("UID COPY failed")

                    typ2, _ = client.uid("STORE", uid, "+FLAGS", "\\Deleted")
                    if typ2 != "OK":
                        raise imaplib.IMAP4.error("UID STORE +FLAGS \\Deleted failed")

                    db.execute("UPDATE actions SET status='done', executed_at=? WHERE id=?", (now_iso(), a_id))
                    stats["done"] += 1

                except imaplib.IMAP4.error as exc:
                    message = str(exc).lower()
                    if "no such message" in message or "uid command error" in message or "failed" in message:
                        if strict:
                            db.execute("UPDATE actions SET status='failed', executed_at=? WHERE id=?", (now_iso(), a_id))
                            db.commit()
                            logger.log(
                                "ERROR",
                                "message_missing_strict_abort",
                                {
                                    "uid": uid,
                                    "folder": folder,
                                    "target": target,
                                    "error": str(exc),
                                },
                                console=f"💥 STRICT: missing UID {uid} in {folder} — aborting",
                            )
                            raise
                        db.execute("UPDATE actions SET status='skipped', executed_at=? WHERE id=?", (now_iso(), a_id))
                        try:
                            db.execute("DELETE FROM headers WHERE uid=? AND folder=?", (uid, folder))
                            logger.log("INFO", "cache_cleanup", {"uid": uid, "folder": folder})
                        except Exception as cleanup_exc:  # pragma: no cover - best effort cleanup
                            logger.log(
                                "WARN",
                                "cache_cleanup_failed",
                                {"uid": uid, "folder": folder, "error": str(cleanup_exc)},
                            )
                        stats["skipped"] += 1
                        continue

                    db.execute("UPDATE actions SET status='failed', executed_at=? WHERE id=?", (now_iso(), a_id))
                    stats["failed"] += 1
                    logger.log(
                        "ERROR",
                        "execute_failed",
                        {"uid": uid, "folder": folder, "target": target, "error": str(exc)},
                        console=f"❌ {folder}/{uid}: {exc}",
                    )

            try:
                client.expunge()
            except Exception:  # pragma: no cover - best effort cleanup
                pass

            db.commit()
            logger.log(
                "INFO",
                "execute_folder_done",
                {"folder": folder, "target": target, "moved": len(uids)},
                console=f"✅ {folder}: handled {len(uids)} → {target}",
            )

        except Exception as exc:
            if strict:
                raise
            logger.log(
                "ERROR",
                "execute_group_failed",
                {"folder": folder, "target": target, "error": str(exc)},
                console=f"💥 Group failed: {folder} → {target}: {exc}",
            )

    timer.stop()
    timer.count = stats["done"]
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "execute",
            **stats,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Execute Actions\n"
            f"   📦  Actions executed: {stats['done']}\n"
            f"   ⚠️  Skipped (missing): {stats['skipped']}\n"
            f"   🚫  Suppressed (duplicates): {stats['suppressed']}\n"
            f"   💥  Failed: {stats['failed']}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
            f"   {'🔒 STRICT' if strict else '✅ Completed'} {'(dry-run)' if dry else ''}\n"
        ),
    )
    return timer, stats
