#!/usr/bin/env python3
"""
IMAPFilter Helper (Emoji Edition)
---------------------------------
🎉 Features:
 • All paths under /root/imapfilter/
 • Timezone-aware timestamps
 • Fun emoji progress bars
 • Per-phase and overall summaries (console + JSON log)
 • Nested tqdm bars (folders + messages)
"""

import imaplib, email, json, os, re, sys, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tqdm import tqdm
from time import perf_counter

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_DEFAULTS = {
    "paths": {
        "rules_dir": str(BASE_DIR / "rules"),
        "secrets_file": str(BASE_DIR / "secrets.json"),
        "db_file": str(BASE_DIR / "cache.db"),
        "log_file": str(BASE_DIR / "imapfilter-helper.log"),
    },
    "logging": {"show_progress": True},
    "executor": {"default_run_scope": "inbox", "dry_run": False},
}
LOG_FILE = Path(CONFIG_DEFAULTS["paths"]["log_file"])

# ----------------------------------------------------------------------
# Utility: Logger and Timer
# ----------------------------------------------------------------------
def log(level, message, context=None, console=None):
    ts = datetime.now(timezone.utc).isoformat()
    entry = {"timestamp": ts, "level": level, "message": message}
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
    def format_time(self):
        s = int(self.elapsed)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h} h {m} m {s} s"
        elif m:
            return f"{m} m {s} s"
        return f"{s} s"

# ----------------------------------------------------------------------
# SQLite helpers
# ----------------------------------------------------------------------
def init_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY, name TEXT, parent TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS headers (uid TEXT PRIMARY KEY, folder TEXT, data TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, uid TEXT, folder TEXT, rule_name TEXT, target TEXT, status TEXT, created_at TEXT, executed_at TEXT)")
    db.commit()
    return db

# ----------------------------------------------------------------------
# IMAP helpers
# ----------------------------------------------------------------------
def imap_login(cfg):
    secrets_path = Path(cfg["paths"]["secrets_file"])
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
    folders_bar = tqdm(folders, desc="📂 Caching folders", unit="folder", dynamic_ncols=True, leave=True, position=0, disable=not show)
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
                db.execute("INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)", (folder, "/".join(folder.split("/")[:-1]), datetime.now(timezone.utc).isoformat()))
                db.commit()
                continue
            msgs_bar = tqdm(uids, desc=f"   ✉️ Fetching {folder}", unit="msg", dynamic_ncols=True, leave=False, position=1, disable=not show)
            for uid in msgs_bar:
                typ, msg_data = M.fetch(uid, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw_hdr = msg_data[0][1]
                hdr_str = raw_hdr.decode(errors="ignore")
                db.execute("INSERT OR REPLACE INTO headers VALUES(?,?,?,?)", (uid.decode(), folder, json.dumps({"header": hdr_str}), datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)", (folder, "/".join(folder.split("/")[:-1]), datetime.now(timezone.utc).isoformat()))
            db.commit()
            total_msgs += len(uids)
            log("INFO", "cache_folder_done", {"folder": folder, "messages": len(uids)}, console=f"✅ {folder}: {len(uids)} messages cached")
        except Exception as e:
            log("ERROR", "cache_folder_failed", {"folder": folder, "error": str(e)}, console=f"❌ {folder}: {e}")
    timer.stop()
    timer.count = total_msgs
    log("INFO", "phase_summary", {"phase": "cache", "folders": len(folders), "messages": total_msgs, "elapsed_sec": timer.elapsed, "rate": timer.rate()}, console=f"\n📊 Summary — Build Cache\n   🗂️  Folders processed: {len(folders)}\n   ✉️  Messages cached: {total_msgs}\n   ⏱️  Duration: {timer.format_time()} ({timer.rate():.1f} msg/s)\n")
    return timer, len(folders), total_msgs

# ----------------------------------------------------------------------
# Evaluate phase
# ----------------------------------------------------------------------
def load_rules(rule_dir):
    rules = []
    for f in sorted(Path(rule_dir).glob("*.json")):
        try:
            rule = json.load(open(f))
            rule["_file"] = f.name
            rules.append(rule)
        except Exception as e:
            log("ERROR", "rule_load_failed", {"file": str(f), "error": str(e)}, console=f"❌ Failed to load {f.name}")
    log("INFO", "rules_loaded", {"count": len(rules)}, console=f"📜 Loaded {len(rules)} rules")
    return rules

def rule_match(header, cond):
    val = header.get(cond["header"].lower(), "")
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
    folders_bar = tqdm(folder_groups.items(), desc="🧩 Evaluating folders", unit="folder", dynamic_ncols=True, leave=True, position=0, disable=not show)
    total_matches = 0
    for folder, msgs in folders_bar:
        folders_bar.set_postfix_str(folder)
        msgs_bar = tqdm(msgs, desc=f"   🎯 Checking {folder}", unit="msg", dynamic_ncols=True, leave=False, position=1, disable=not show)
        for uid, data in msgs_bar:
            hdr = json.loads(data)["header"]
            header = {k.lower(): v for k, v in email.message_from_string(hdr).items()}
            for rule in rules:
                if scope == "inbox" and not folder.lower().endswith("inbox"):
                    continue
                conds = rule.get("conditions", [])
                if conds and all(rule_match(header, c) for c in conds):
                    action = rule.get("action", {})
                    db.execute("INSERT INTO actions (uid, folder, rule_name, target, status, created_at) VALUES (?,?,?,?,?,?)", (uid, folder, rule["name"], action.get("target", ""), "pending" if not cfg["executor"]["dry_run"] else "simulated", datetime.now(timezone.utc).isoformat()))
                    total_matches += 1
                    log("INFO", "rule_match", {"rule": rule["name"], "folder": folder, "uid": uid, "target": action.get("target"), "dry_run": cfg["executor"]["dry_run"]})
        db.commit()
    timer.stop()
    timer.count = total_matches
    log("INFO", "phase_summary", {"phase": "evaluate", "rules": len(rules), "matches": total_matches, "elapsed_sec": timer.elapsed, "rate": timer.rate()}, console=f"\n📊 Summary — Evaluate Rules\n   🧩  Rules evaluated: {len(rules)}\n   🎯  Matches found: {total_matches}\n   ⏱️  Duration: {timer.format_time()} ({timer.rate():.1f} msg/s)\n")
    return timer, len(rules), total_matches

# ----------------------------------------------------------------------
# Execute phase
# ----------------------------------------------------------------------
def execute_actions(M, db, cfg):
    show = cfg["logging"].get("show_progress", True)
    timer = PhaseTimer("execute")
    cur = db.cursor()
    cur.execute("SELECT folder, target, COUNT(*), GROUP_CONCAT(uid) FROM actions WHERE status='pending' GROUP BY folder,target")
    rows = cur.fetchall()
    if not rows:
        log("INFO", "execute_nothing", {"dry_run": cfg["executor"]["dry_run"]}, console="ℹ️ No pending actions")
        return timer, 0
    folders_bar = tqdm(rows, desc="📦 Executing folders", unit="folder", dynamic_ncols=True, leave=True, position=0, disable=not show)
    total_done = 0
    for folder, target, count, uids_str in folders_bar:
        uids = uids_str.split(",")
        folders_bar.set_postfix_str(f"{folder} → {target}")
        msgs_bar = tqdm(uids, desc=f"   🚚 Moving {folder}", unit="msg", dynamic_ncols=True, leave=False, position=1, disable=not show)
        if cfg["executor"]["dry_run"]:
            log("INFO", "dry_action", {"folder": folder, "target": target, "count": count}, console=f"🧪 Dry run: {folder} → {target} ({count})")
            continue
        try:
            M.select(f'"{folder}"')
            for uid in msgs_bar:
                M.uid("COPY", uid, f'"{target}"')
                M.uid("STORE", uid, "+FLAGS", "\\Deleted")
                db.execute("UPDATE actions SET status='done', executed_at=? WHERE uid=?", (datetime.now(timezone.utc).isoformat(), uid))
                total_done += 1
            M.expunge()
            db.commit()
            log("INFO", "execute_folder_done", {"folder": folder, "target": target, "moved": count}, console=f"✅ {folder}: moved {count} → {target}")
        except Exception as e:
            log("ERROR", "execute_failed", {"folder": folder, "error": str(e)}, console=f"❌ {folder}: {e}")
    timer.stop()
    timer.count = total_done
    log("INFO", "phase_summary", {"phase": "execute", "done": total_done, "elapsed_sec": timer.elapsed, "rate": timer.rate()}, console=f"\n📊 Summary — Execute Actions\n   📦  Actions executed: {total_done}\n   ⏱️  Duration: {timer.format_time()} ({timer.rate():.1f} msg/s)\n   ✅  Status: complete\n")
    return timer, total_done

# ----------------------------------------------------------------------
# CLI and main
# ----------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="IMAPFilter Helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build-cache", help="Build local message cache")
    p_build.add_argument("--all-folders", action="store_true", help="Scan all folders")
    p_eval = sub.add_parser("evaluate", help="Evaluate rules against cache")
    p_eval.add_argument("--dry-run", action="store_true")
    p_exec = sub.add_parser("execute", help="Execute queued actions")
    p_exec.add_argument("--dry-run", action="store_true")
    p_run = sub.add_parser("run-all", help="Build cache, evaluate, and execute")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--all-folders", action="store_true")
    args = parser.parse_args()
    cfg = CONFIG_DEFAULTS.copy()
    Path(cfg["paths"]["rules_dir"]).mkdir(exist_ok=True)
    db = init_db(cfg["paths"]["db_file"])

    if args.cmd == "build-cache":
        M = imap_login(cfg)
        try:
            folders = list_all_folders(M) if args.all_folders else ["INBOX"]
            build_cache(M, db, folders, cfg)
        finally:
            M.logout()

    elif args.cmd == "evaluate":
        cfg["executor"]["dry_run"] = args.dry_run
        rules = load_rules(cfg["paths"]["rules_dir"])
        evaluate_rules(db, rules, cfg["executor"]["default_run_scope"], cfg)

    elif args.cmd == "execute":
        cfg["executor"]["dry_run"] = args.dry_run
        M = None if args.dry_run else imap_login(cfg)
        try:
            execute_actions(M, db, cfg)
        finally:
            if M:
                M.logout()

    elif args.cmd == "run-all":
        cfg["executor"]["dry_run"] = args.dry_run
        run_timer = PhaseTimer("run-all")
        M = imap_login(cfg)
        try:
            folders = list_all_folders(M) if args.all_folders else ["INBOX"]
            t_cache, folders_count, msg_count = build_cache(M, db, folders, cfg)
            rules = load_rules(cfg["paths"]["rules_dir"])
            t_eval, rules_count, matches = evaluate_rules(db, rules, cfg["executor"]["default_run_scope"], cfg)
            t_exec, done = execute_actions(M, db, cfg)
            run_timer.stop()
            total = msg_count or matches or done
            log("INFO", "run_summary", {"duration_sec": run_timer.elapsed, "folders": folders_count, "messages": msg_count, "rules": rules_count, "matches": matches, "actions": done}, console=f"\n🏁 Run Summary\n   🕒  Total runtime: {t_cache.format_time()}\n   🗂️  Folders: {folders_count}\n   ✉️  Messages: {msg_count}\n   🧩  Rules: {rules_count}\n   🎯  Matches: {matches}\n   📦  Actions executed: {done}\n   ✅  Completed successfully!\n")
        finally:
            M.logout()

if __name__ == "__main__":
    main()
