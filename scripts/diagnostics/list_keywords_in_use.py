#!/usr/bin/env python3
"""Scan every folder and report which IMAP keywords/flags are actually
set on messages right now (as opposed to core/config.json's predefined
list, which is just the vocabulary offered by the rule wizard - nothing
in it is guaranteed to actually be applied to any message).

Fetches FLAGS only (no headers/bodies), so it's fast and read-only.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from core.imap_client import imap_login, list_all_folders, safe_search_all  # noqa: E402
from core.logging_utils import JsonLogger  # noqa: E402

SYSTEM_FLAGS = {r"\Seen", r"\Answered", r"\Flagged", r"\Deleted", r"\Draft", r"\Recent"}
FETCH_BATCH_SIZE = 500
FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


def main() -> int:
    secrets_path = REPO_ROOT / "data" / "secrets.json"
    logger = JsonLogger(REPO_ROOT / "data" / "imapfilter-helper.log")

    client = imap_login(secrets_path, logger)
    try:
        folders = list_all_folders(client)
        print(f"Scanning {len(folders)} folders for keywords/flags in use...\n")

        custom_counter: Counter[str] = Counter()
        system_counter: Counter[str] = Counter()
        custom_by_folder: dict[str, Counter[str]] = {}
        messages_with_custom = 0
        total_messages = 0
        start = time.time()

        for i, folder in enumerate(folders, 1):
            try:
                sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            except Exception as exc:
                print(f"[{i}/{len(folders)}] ⚠️  could not select {folder}: {exc}")
                continue
            if sel_typ != "OK":
                continue

            uids = list(safe_search_all(client, undeleted_only=True))
            if not uids:
                continue
            total_messages += len(uids)

            for batch_start in range(0, len(uids), FETCH_BATCH_SIZE):
                batch = uids[batch_start : batch_start + FETCH_BATCH_SIZE]
                uid_set = b",".join(
                    u if isinstance(u, (bytes, bytearray)) else str(u).encode() for u in batch
                )
                try:
                    typ, msg_data = client.uid("FETCH", uid_set, "(FLAGS)")
                except Exception as exc:
                    print(f"[{i}/{len(folders)}] ⚠️  FETCH failed in {folder}: {exc}")
                    continue
                if typ != "OK":
                    continue
                for item in msg_data:
                    if not item or not isinstance(item, (bytes, bytearray)):
                        continue
                    m = FLAGS_RE.search(item)
                    if not m:
                        continue
                    flags = [f for f in m.group(1).decode("ascii", "ignore").split() if f]
                    custom = [f for f in flags if f not in SYSTEM_FLAGS]
                    for f in flags:
                        if f in SYSTEM_FLAGS:
                            system_counter[f] += 1
                    if custom:
                        messages_with_custom += 1
                        custom_counter.update(custom)
                        custom_by_folder.setdefault(folder, Counter()).update(custom)

            if i % 50 == 0:
                elapsed = time.time() - start
                print(f"[{i}/{len(folders)}] ...{elapsed:.0f}s elapsed, {len(custom_counter)} distinct custom keyword(s) so far")

        elapsed = time.time() - start
        print(f"\nDone in {elapsed:.0f}s. Scanned {total_messages} messages across {len(folders)} folders.\n")

        print("=== System flags in use ===")
        for flag, count in system_counter.most_common():
            print(f"  {flag}: {count}")

        print("\n=== Custom keywords in use ===")
        if not custom_counter:
            print("  (none found - no message anywhere in the mailbox carries a custom keyword)")
        else:
            for keyword, count in custom_counter.most_common():
                print(f"  {keyword}: {count} message(s)")
                folders_with_it = sorted(
                    ((f, c[keyword]) for f, c in custom_by_folder.items() if keyword in c),
                    key=lambda x: -x[1],
                )[:5]
                for f, c in folders_with_it:
                    print(f"      {c:>5}  {f}")

        print(f"\nMessages with >=1 custom keyword: {messages_with_custom}/{total_messages}")
        return 0
    finally:
        client.logout()


if __name__ == "__main__":
    raise SystemExit(main())
