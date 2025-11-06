#!/usr/bin/env python3
"""
IMAPFilter Helper (Priority + A+ Cache Clean Edition)
-----------------------------------------------------
• Priority-aware execution (higher priority wins)
• Duplicate suppression for same UID across rules
• Option A+ on missing message: skip + remove from cache (headers)
• --strict: abort on missing/failed IMAP ops (production-safe)
• Emoji progress bars + per-phase & run summaries
• JSON logs to file, tidy console via tqdm.write()
• Timezone-aware timestamps
• All paths local to /root/imapfilter
"""

import imaplib, email, json, re, sys, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tqdm import tqdm
from time import perf_counter

from imapfilter.core.config import get_paths, load_config, resolve_path


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
LOG_FILE = resolve_path("log_file")


# ----------------------------------------------------------------------
# Utilities: time, logging, timer
# ----------------------------------------------------------------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log(level, message, context=None, console=None):
    entry = {"timestamp": now_iso(), "level": level, "message": message}
    if context:
        entry["context"] = context
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if console:
        tqdm.write(console)

class PhaseTimer:
    def __init__(self, phase):
        self.phase = phase
        self.start = perf_counter()
        self.end = None
        self.count = 0
    def stop(self):
        self.end = perf_counter()
    @property
    def elapsed(self):
        return (self.end or perf_counter()) - self.start
    def rate(self):
        return self.count / self.elapsed if self.elapsed > 0 else 0.0
    def fmt(self):
        s = int(self.elapsed)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h: return f"{h} h {m} m {s} s"
        if m: return f"{m} m {s} s"
        return f"{s} s"


# ----------------------------------------------------------------------
# SQLite helpers (with light migration)
# ----------------------------------------------------------------------
def init_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS headers (uid TEXT PRIMARY KEY, folder TEXT, data TEXT, updated_at TEXT)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS actions ("
        "id INTEGER PRIMARY KEY, uid TEXT, folder TEXT, rule_name TEXT, target TEXT, "
        "priority INTEGER, status TEXT, created_at TEXT, executed_at TEXT)"
    )
    # Migrations (idempotent)
    _ensure_column(db, "actions", "priority", "INTEGER", default=100)
    _ensure_column(db, "actions", "executed_at", "TEXT", default=None)
    db.commit()
    return db

def _ensure_column(db, table, column, coltype, default=None):
    cur = db.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        if default is not None:
            if isinstance(default, int):
                db.execute(f"UPDATE {table} SET {column}={default} WHERE {column} IS NULL")
            else:
                db.execute(f"UPDATE {table} SET {column}=? WHERE {column} IS NULL", (default,))
        db.commit()


# ----------------------------------------------------------------------
# IMAP helpers
# ----------------------------------------------------------------------
def imap_login(cfg):
    secrets_path = resolve_path("secrets_file", cfg)
    if not secrets_path.exists():
        sys.exit(f"❌ Secrets file not found: {secrets_path}")
    secrets = json.load(open(secrets_path))
    s = secrets["imap"]
    log("INFO", "Connecting to IMAP", {"host": s["host"], "user": s["username"]}, console=f"🔐 Connecting as {s['username']}")
    M = imaplib.IMAP4_SSL(s["host"], s.get("port", 993))
    M.login(s["username"], s["password"])
    return M

def list_all_folders(M):
    typ, data = M.list()
    if typ != "OK":
        raise RuntimeError("Unable to list folders")
    folders = []
    for line in data:
        if not line:
            continue
        parts = line.decode().split(' "/" ')
        if len(parts) == 2:
            folders.append(parts[1].strip('"'))
    return folders

def safe_search_all(M):
    typ, data = M.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


# ----------------------------------------------------------------------
# Cache phase
# ----------------------------------------------------------------------
def build_cache(M, db, folders, cfg):
    show = cfg["logging"].get("show_progress", True)
    timer = PhaseTimer("cache")
    folders_bar = tqdm(
        folders, desc="📂 Caching folders", unit="folder",
        dynamic_ncols=True, leave=True, position=0, disable=not show
    )
    total_msgs = 0

    for folder in folders_bar:
        folders_bar.set_postfix_str(folder)
        log("INFO", "cache_folder_start", {"folder": folder})
        try:
            sel_typ, _ = M.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                log("INFO", "cache_folder_skipped", {"folder": folder}, console=f"⚠️ Skipped {folder}")
                continue

            uids = safe_search_all(M)
            if not uids:
                log("INFO", "cache_folder_empty", {"folder": folder}, console=f"📂 {folder}: empty")
                db.execute(
                    "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                    (folder, "/".join(folder.split("/")[:-1]), now_iso()),
                )
                db.commit()
                continue

            msgs_bar = tqdm(
                uids, desc=f"   ✉️ Fetching {folder}", unit="msg",
                dynamic_ncols=True, leave=False, position=1, disable=not show
            )

            for uid in msgs_bar:
                typ, msg_data = M.fetch(uid, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw_hdr = msg_data[0][1]
                hdr_str = raw_hdr.decode(errors="ignore")
                db.execute(
                    "INSERT OR REPLACE INTO headers VALUES(?,?,?,?)",
                    (uid.decode(), folder, json.dumps({"header": hdr_str}), now_iso()),
                )

            db.execute(
                "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                (folder, "/".join(folder.split("/")[:-1]), now_iso()),
            )
            db.commit()
            total_msgs += len(uids)
            log("INFO", "cache_folder_done", {"folder": folder, "messages": len(uids)}, console=f"✅ {folder}: {len(uids)} messages cached")

        except Exception as e:
            log("ERROR", "cache_folder_failed", {"folder": folder, "error": str(e)}, console=f"❌ {folder}: {e}")

    timer.stop()
    timer.count = total_msgs
    log(
        "INFO", "phase_summary",
        {"phase": "cache", "folders": len(folders), "messages": total_msgs, "elapsed_sec": timer.elapsed, "rate": timer.rate()},
        console=f"\n📊 Summary — Build Cache\n   🗂️  Folders processed: {len(folders)}\n   ✉️  Messages cached: {total_msgs}\n   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n",
    )
    return timer, len(folders), total_msgs


# ----------------------------------------------------------------------
# Evaluate phase
# ----------------------------------------------------------------------
def load_rules(rule_dir):
    rules = []
    Path(rule_dir).mkdir(exist_ok=True)
    for f in sorted(Path(rule_dir).glob("*.json")):
        try:
            rule = json.load(open(f))
            rule["_file"] = f.name
            rule["priority"] = int(rule.get("priority", 100))
            rules.append(rule)
        except Exception as e:
            log("ERROR", "rule_load_failed", {"file": str(f), "error": str(e)}, console=f"❌ Failed to load {f.name}")
    log("INFO", "rules_loaded", {"count": len(rules)}, console=f"📜 Loaded {len(rules)} rules")
    return rules

def rule_match(header, cond):
    val = header.get(cond["header"].lower(), "")
    if val is None:
        val = ""
    pattern = cond.get("contains") or cond.get("regex")
    if not pattern:
        return False
    if "regex" in cond:
        return bool(re.search(pattern, val, re.I))
    return pattern.lower() in val.lower()

def evaluate_rules(db, rules, scope, cfg):
    show = cfg["logging"].get("show_progress", True)
    timer = PhaseTimer("evaluate")

    cur = db.cursor()
    cur.execute("SELECT uid, folder, data FROM headers")
    rows = cur.fetchall()

    folder_groups = {}
    for uid, folder, data in rows:
        folder_groups.setdefault(folder, []).append((uid, data))

    folders_bar = tqdm(
        folder_groups.items(), desc="🧩 Evaluating folders", unit="folder",
        dynamic_ncols=True, leave=True, position=0, disable=not show
    )

    total_matches = 0
    for folder, msgs in folders_bar:
        folders_bar.set_postfix_str(folder)
        msgs_bar = tqdm(
            msgs, desc=f"   🎯 Checking {folder}", unit="msg",
            dynamic_ncols=True, leave=False, position=1, disable=not show
        )

        for uid, data in msgs_bar:
            hdr = json.loads(data)["header"]
            header = {k.lower(): v for k, v in email.message_from_string(hdr).items()}

            for rule in rules:
                if scope == "inbox" and not folder.lower().endswith("inbox"):
                    continue

                conds = rule.get("conditions", [])
                if conds and all(rule_match(header, c) for c in conds):
                    action = rule.get("action", {})
                    db.execute(
                        "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            uid,
                            folder,
                            rule["name"],
                            action.get("target", ""),
                            int(rule.get("priority", 100)),
                            "pending" if not cfg["executor"]["dry_run"] else "simulated",
                            now_iso(),
                        ),
                    )
                    total_matches += 1
                    log("INFO", "rule_match", {
                        "rule": rule["name"], "priority": int(rule.get("priority", 100)),
                        "folder": folder, "uid": uid, "target": action.get("target"),
                        "dry_run": cfg["executor"]["dry_run"]
                    })

        db.commit()

    timer.stop()
    timer.count = total_matches
    log(
        "INFO", "phase_summary",
        {"phase": "evaluate", "rules": len(rules), "matches": total_matches, "elapsed_sec": timer.elapsed, "rate": timer.rate()},
        console=f"\n📊 Summary — Evaluate Rules\n   🧩  Rules evaluated: {len(rules)}\n   🎯  Matches found: {total_matches}\n   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n",
    )
    return timer, len(rules), total_matches


# ----------------------------------------------------------------------
# Execute phase (priority-aware + A+ cache clean + strict option)
# ----------------------------------------------------------------------
def execute_actions(M, db, cfg):
    show = cfg["logging"].get("show_progress", True)
    strict = cfg["executor"].get("strict", False)
    dry = cfg["executor"].get("dry_run", False)
    timer = PhaseTimer("execute")

    cur = db.cursor()
    # Pull all pending actions ordered by priority DESC and created_at ASC
    cur.execute(
        "SELECT id, uid, folder, target, rule_name, priority, status, created_at "
        "FROM actions WHERE status='pending' ORDER BY priority DESC, created_at ASC"
    )
    actions = cur.fetchall()

    if not actions:
        log("INFO", "execute_nothing", {"dry_run": dry}, console="ℹ️ No pending actions")
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    # Deduplicate by UID: highest priority wins; suppress others
    chosen = []
    seen_uids = set()
    suppressed = 0

    for row in actions:
        a_id, uid, folder, target, rule_name, priority, status, created_at = row
        if uid in seen_uids:
            suppressed += 1
            # Mark suppressed for traceability (do not in dry-run)
            if not dry:
                db.execute("UPDATE actions SET status='suppressed', executed_at=? WHERE id=?", (now_iso(), a_id))
            log("INFO", "duplicate_action_suppressed", {
                "uid": uid, "rule": rule_name, "priority": priority, "target": target
            })
            continue
        seen_uids.add(uid)
        chosen.append(row)

    if not dry:
        db.commit()

    # Group chosen by (folder -> target) for efficient IMAP operations
    grouped = {}
    for a_id, uid, folder, target, rule_name, priority, status, created_at in chosen:
        grouped.setdefault((folder, target), []).append((a_id, uid, rule_name, priority))

    folders_bar = tqdm(
        list(grouped.items()), desc="📦 Executing folders", unit="folder",
        dynamic_ncols=True, leave=True, position=0, disable=not show
    )

    stats = {"done": 0, "skipped": 0, "failed": 0, "suppressed": suppressed}

    for (folder, target), items in folders_bar:
        folders_bar.set_postfix_str(f"{folder} → {target}")
        uids = [uid for _, uid, _, _ in items]

        msgs_bar = tqdm(
            uids, desc=f"   🚚 Moving {folder}", unit="msg",
            dynamic_ncols=True, leave=False, position=1, disable=not show
        )

        if dry:
            log("INFO", "dry_action_group", {"folder": folder, "target": target, "count": len(uids)},
                console=f"🧪 Dry run: {folder} → {target} ({len(uids)})")
            continue

        try:
            sel_typ, _ = M.select(f'"{folder}"')
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            # Build a quick lookup: uid -> action_id
            uid_to_action = {uid: a_id for (a_id, uid, _, _) in items}

            for uid in msgs_bar:
                a_id = uid_to_action[uid]
                try:
                    # COPY then mark deleted
                    typ1, _ = M.uid("COPY", uid, f'"{target}"')
                    if typ1 != "OK":
                        raise imaplib.IMAP4.error("UID COPY failed")

                    typ2, _ = M.uid("STORE", uid, "+FLAGS", "\\Deleted")
                    if typ2 != "OK":
                        raise imaplib.IMAP4.error("UID STORE +FLAGS \\Deleted failed")

                    db.execute("UPDATE actions SET status='done', executed_at=? WHERE id=?", (now_iso(), a_id))
                    stats["done"] += 1

                except imaplib.IMAP4.error as e:
                    msg = str(e).lower()
                    if "no such message" in msg or "uid command error" in msg or "failed" in msg:
                        # Option A+: skip + remove stale cache record
                        if strict:
                            db.execute("UPDATE actions SET status='failed', executed_at=? WHERE id=?", (now_iso(), a_id))
                            db.commit()
                            log("ERROR", "message_missing_strict_abort", {"uid": uid, "folder": folder, "target": target, "error": str(e)},
                                console=f"💥 STRICT: missing UID {uid} in {folder} — aborting")
                            raise
                        else:
                            db.execute("UPDATE actions SET status='skipped', executed_at=? WHERE id=?", (now_iso(), a_id))
                            try:
                                db.execute("DELETE FROM headers WHERE uid=? AND folder=?", (uid, folder))
                                log("INFO", "cache_cleanup", {"uid": uid, "folder": folder})
                            except Exception as ce:
                                log("WARN", "cache_cleanup_failed", {"uid": uid, "folder": folder, "error": str(ce)})
                            stats["skipped"] += 1
                            continue
                    # Other IMAP error
                    db.execute("UPDATE actions SET status='failed', executed_at=? WHERE id=?", (now_iso(), a_id))
                    stats["failed"] += 1
                    log("ERROR", "execute_failed", {"uid": uid, "folder": folder, "target": target, "error": str(e)}, console=f"❌ {folder}/{uid}: {e}")

            # Try to expunge even if some failed/skipped
            try:
                M.expunge()
            except Exception:
                pass

            db.commit()
            log("INFO", "execute_folder_done", {"folder": folder, "target": target, "moved": len(uids)},
                console=f"✅ {folder}: handled {len(uids)} → {target}")

        except Exception as e:
            # Strict mode may bubble to here
            if strict:
                raise
            log("ERROR", "execute_group_failed", {"folder": folder, "target": target, "error": str(e)}, console=f"💥 Group failed: {folder} → {target}: {e}")

    timer.stop()
    timer.count = stats["done"]
    log(
        "INFO", "phase_summary",
        {"phase": "execute", **stats, "elapsed_sec": timer.elapsed, "rate": timer.rate()},
        console=(
            f"\n📊 Summary — Execute Actions\n"
            f"   📦  Actions executed: {stats['done']}\n"
            f"   ⚠️  Skipped (missing): {stats['skipped']}\n"
            f"   🚫  Suppressed (duplicates): {stats['suppressed']}\n"
            f"   💥  Failed: {stats['failed']}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
            f"   {'✅  Status: complete' if not strict else '🔒  Mode: STRICT'}\n"
        ),
    )
    return timer, stats


# ----------------------------------------------------------------------
# CLI and main
# ----------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="IMAPFilter Helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # build-cache
    p_build = sub.add_parser("build-cache", help="Build local message cache")
    p_build.add_argument("--all-folders", action="store_true", help="Scan all folders")

    # evaluate
    p_eval = sub.add_parser("evaluate", help="Evaluate rules against cache")
    p_eval.add_argument("--dry-run", action="store_true", help="Simulate rule matches only")

    # execute
    p_exec = sub.add_parser("execute", help="Execute queued actions")
    p_exec.add_argument("--dry-run", action="store_true", help="Simulate execution only (no IMAP writes)")
    p_exec.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops")

    # run-all
    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate everything (no IMAP writes)")
    p_run.add_argument("--all-folders", action="store_true", help="Process all folders, not just INBOX")
    p_run.add_argument("--strict", action="store_true", help="Abort on missing/failed IMAP ops during execute")

    args = parser.parse_args()
    cfg = load_config()
    paths = get_paths(cfg)
    global LOG_FILE
    LOG_FILE = resolve_path("log_file", cfg)

    paths["rules_dir"].mkdir(parents=True, exist_ok=True)
    db = init_db(paths["db_file"])

    if args.cmd == "build-cache":
        M = imap_login(cfg)
        try:
            folders = list_all_folders(M) if args.all_folders else ["INBOX"]
            build_cache(M, db, folders, cfg)
        finally:
            M.logout()

    elif args.cmd == "evaluate":
        cfg["executor"]["dry_run"] = args.dry_run
        rules = load_rules(resolve_path("rules_dir", cfg))
        evaluate_rules(db, rules, cfg["executor"]["default_run_scope"], cfg)

    elif args.cmd == "execute":
        cfg["executor"]["dry_run"] = args.dry_run
        cfg["executor"]["strict"] = args.strict
        M = None if args.dry_run else imap_login(cfg)
        try:
            execute_actions(M, db, cfg)
        finally:
            if M:
                M.logout()

    elif args.cmd == "run-all":
        cfg["executor"]["dry_run"] = args.dry_run
        cfg["executor"]["strict"] = args.strict
        run_timer = PhaseTimer("run-all")
        M = imap_login(cfg)
        try:
            folders = list_all_folders(M) if args.all_folders else ["INBOX"]
            t_cache, folders_count, msg_count = build_cache(M, db, folders, cfg)
            rules = load_rules(resolve_path("rules_dir", cfg))
            t_eval, rules_count, matches = evaluate_rules(db, rules, cfg["executor"]["default_run_scope"], cfg)
            t_exec, stats = execute_actions(M, db, cfg)
            run_timer.stop()
            log(
                "INFO", "run_summary",
                {
                    "duration_sec": run_timer.elapsed,
                    "folders": folders_count, "messages": msg_count,
                    "rules": rules_count, "matches": matches,
                    **{f"exec_{k}": v for k, v in stats.items()},
                    "strict": cfg["executor"]["strict"], "dry_run": cfg["executor"]["dry_run"],
                },
                console=(
                    f"\n🏁 Run Summary\n"
                    f"   🕒  Total runtime: {t_cache.fmt() if t_cache else PhaseTimer('r').fmt()}\n"
                    f"   🗂️  Folders: {folders_count}\n"
                    f"   ✉️  Messages: {msg_count}\n"
                    f"   🧩  Rules: {rules_count}\n"
                    f"   🎯  Matches: {matches}\n"
                    f"   📦  Executed: {stats.get('done',0)}  |  ⚠️ Skipped: {stats.get('skipped',0)}  |  🚫 Suppressed: {stats.get('suppressed',0)}  |  💥 Failed: {stats.get('failed',0)}\n"
                    f"   {'🔒 STRICT' if cfg['executor']['strict'] else '✅ Completed'} {'(dry-run)' if cfg['executor']['dry_run'] else ''}\n"
                ),
            )
        finally:
            M.logout()


if __name__ == "__main__":
    main()