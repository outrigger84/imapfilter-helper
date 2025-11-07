"""Cache building helpers."""
from __future__ import annotations

import json
from typing import Sequence

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso
from core.imap_client import safe_search_all


def build_cache(
    client,
    db,
    folders: Sequence[str],
    *,
    show_progress: bool,
    logger: JsonLogger,
) -> tuple[PhaseTimer, int, int]:
    timer = PhaseTimer("cache")
    folders_bar = tqdm(
        folders,
        desc="📂 Caching folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )
    total_msgs = 0

    for folder in folders_bar:
        folders_bar.set_postfix_str(folder)
        logger.log("INFO", "cache_folder_start", {"folder": folder})
        try:
            sel_typ, _ = client.select(f'"{folder}"', readonly=True)
            if sel_typ != "OK":
                logger.log("INFO", "cache_folder_skipped", {"folder": folder}, console=f"⚠️ Skipped {folder}")
                continue

            uids = safe_search_all(client)
            if not uids:
                logger.log(
                    "INFO",
                    "cache_folder_empty",
                    {"folder": folder},
                    console=f"📂 {folder}: empty",
                )
                db.execute(
                    "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                    (folder, "/".join(folder.split("/")[:-1]), now_iso()),
                )
                db.commit()
                continue

            msgs_bar = tqdm(
                uids,
                desc=f"   ✉️ Fetching {folder}",
                unit="msg",
                dynamic_ncols=True,
                leave=False,
                position=1,
                disable=not show_progress,
            )

            for uid in msgs_bar:
                typ, msg_data = client.fetch(uid, "(BODY.PEEK[HEADER])")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw_hdr = msg_data[0][1]
                hdr_str = raw_hdr.decode(errors="ignore")
                db.execute(
                    "INSERT OR REPLACE INTO headers (folder, uid, data, updated_at) "
                    "VALUES(?,?,?,?)",
                    (
                        folder,
                        uid.decode(),
                        json.dumps({"header": hdr_str}),
                        now_iso(),
                    ),
                )

            db.execute(
                "INSERT OR REPLACE INTO folders VALUES(NULL,?,?,?)",
                (folder, "/".join(folder.split("/")[:-1]), now_iso()),
            )
            db.commit()
            total_msgs += len(uids)
            logger.log(
                "INFO",
                "cache_folder_done",
                {"folder": folder, "messages": len(uids)},
                console=f"✅ {folder}: {len(uids)} messages cached",
            )

        except Exception as exc:  # pragma: no cover - defensive logging
            logger.log(
                "ERROR",
                "cache_folder_failed",
                {"folder": folder, "error": str(exc)},
                console=f"❌ {folder}: {exc}",
            )

    timer.stop()
    timer.count = total_msgs
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "cache",
            "folders": len(folders),
            "messages": total_msgs,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Build Cache\n"
            f"   🗂️  Folders processed: {len(folders)}\n"
            f"   ✉️  Messages cached: {total_msgs}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
        ),
    )
    return timer, len(folders), total_msgs
