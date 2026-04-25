"""MBOX importer: classify messages by rules and upload directly to target IMAP folders."""
from __future__ import annotations

import datetime
import email.errors
import email.utils
import imaplib
import mailbox
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from core.connection_pool import IMAPConnectionPool
from core.imap_client import imap_login
from core.logging_utils import JsonLogger
from core.rule_engine import find_matching_rule, load_rules


def _quote_mailbox(name: str) -> str:
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        return name
    return f'"{name}"'


def _mbox_flags_to_imap(msg: mailbox.mboxMessage) -> Optional[str]:
    """Convert mbox Status/X-Status flags to an IMAP flags string."""
    flag_str = msg.get_flags()  # e.g. "RAF"
    imap_flags = []
    if "R" in flag_str:
        imap_flags.append(r"\Seen")
    if "A" in flag_str:
        imap_flags.append(r"\Answered")
    if "F" in flag_str:
        imap_flags.append(r"\Flagged")
    if "D" in flag_str:
        imap_flags.append(r"\Deleted")
    if "T" in flag_str:
        imap_flags.append(r"\Draft")
    if not imap_flags:
        return None
    return "(" + " ".join(imap_flags) + ")"


def _sanitize_message_headers(msg: mailbox.mboxMessage) -> None:
    """Remove headers with embedded newlines or binary control chars.

    Python 3.14+ raises HeaderWriteError for such headers. This strips them
    in-place so flattening can proceed — the data was already corrupted.
    """
    bad_keys: set[str] = set()
    for key, val in msg.items():
        if "\n" in val or "\r" in val:
            bad_keys.add(key)
        elif any(ord(c) < 32 and c != "\t" for c in val):
            bad_keys.add(key)
    for key in bad_keys:
        del msg[key]


def _message_to_crlf_bytes(msg: mailbox.mboxMessage) -> bytes:
    """Convert mboxMessage to RFC-2822 bytes with CRLF line endings."""
    import io
    import email.generator as _eg

    class _Utf8BytesGenerator(_eg.BytesGenerator):
        """BytesGenerator that writes non-ASCII payload strings as UTF-8."""
        def write(self, s: str) -> None:
            self._fp.write(s.encode("utf-8", "surrogateescape"))

    def _flatten(m: mailbox.mboxMessage) -> bytes:
        buf = io.BytesIO()
        gen = _Utf8BytesGenerator(buf, mangle_from_=False)
        gen.flatten(m, unixfrom=False)
        return buf.getvalue()

    try:
        raw = _flatten(msg)
    except email.errors.HeaderWriteError:
        # Corrupted header (e.g. binary data with embedded newlines).
        # Strip the offending headers and retry.
        _sanitize_message_headers(msg)
        raw = _flatten(msg)
    return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _get_delivery_time(msg: mailbox.mboxMessage):
    """Extract delivery time from message headers, returning imaplib-compatible format."""
    # Try From_ envelope line first (most reliable in mbox format)
    from_line = msg.get_from()
    if from_line:
        try:
            time_str = from_line.split(" ", 1)[1]
            t = time_str.replace(",", " ").lower()
            t = re.sub(r" (sun|mon|tue|wed|thu|fri|sat) ", " ", " " + t + " ")
            if ":" not in t:
                t += " 00:00:00"
            parsed = email.utils.parsedate_tz(t)
            if parsed:
                ts = email.utils.mktime_tz(parsed)
                if ts > 0:
                    return imaplib.Time2Internaldate(ts)
        except Exception:
            pass

    # Try Received: header
    received = msg.get("received")
    if received:
        try:
            t = received.split(";", 1)[1].strip()
            parsed = email.utils.parsedate_tz(t)
            if parsed:
                ts = email.utils.mktime_tz(parsed)
                if ts > 0:
                    return imaplib.Time2Internaldate(ts)
        except Exception:
            pass

    # Try Date: header
    date_str = msg.get("date")
    if date_str:
        try:
            parsed = email.utils.parsedate_tz(date_str)
            if parsed:
                ts = email.utils.mktime_tz(parsed)
                if ts > 0:
                    return imaplib.Time2Internaldate(ts)
        except Exception:
            pass

    return None


def _ensure_folder(client: imaplib.IMAP4_SSL, folder: str, logger: JsonLogger) -> bool:
    """Select or create an IMAP folder. Returns True if usable."""
    quoted = _quote_mailbox(folder)
    typ, _ = client.select(quoted, readonly=True)
    if typ == "OK":
        return True
    typ, resp = client.create(quoted)
    if typ == "OK":
        logger.log("INFO", "folder_created", {"folder": folder},
                   console=f"📁 Created folder: {folder}")
        return True
    logger.log("ERROR", "folder_create_failed", {"folder": folder, "resp": str(resp)},
               console=f"❌ Could not create folder: {folder}")
    return False


# ---------------------------------------------------------------------------
# Progress file — tracks uploaded Message-IDs for crash recovery
# ---------------------------------------------------------------------------

def _load_progress(progress_path: Path) -> set[str]:
    """Return the set of Message-IDs already uploaded (from a previous run)."""
    if not progress_path.exists():
        return set()
    uploaded: set[str] = set()
    with progress_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                uploaded.add(line)
    return uploaded


def _record_uploaded(progress_path: Path, msg_id: str) -> None:
    """Append one Message-ID to the progress file."""
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write(msg_id + "\n")


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify_messages(
    mbox_path: Path,
    rules: list[dict],
    default_folder: str,
    limit: Optional[int],
    verbose: bool,
    uploaded_ids: set[str],
    no_move: bool = False,
) -> tuple[dict[str, list[int]], int, dict[int, str], list[int]]:
    """Stream through MBOX and record the target folder index for each message.

    Skips messages whose Message-ID is already in uploaded_ids (progress recovery).

    Returns:
        folder_indices   — folder name → list of integer mbox keys
        unmatched_count  — messages routed to default_folder due to no rule match
        index_to_msgid   — mbox key → Message-ID (empty string if header absent)
        skipped_indices  — mbox keys skipped because already uploaded (for cleanup)
    """
    sorted_rules = sorted(rules, key=lambda r: int(r.get("priority", 100)))

    mbox = mailbox.mbox(str(mbox_path))
    total = len(mbox)
    if limit:
        total = min(total, limit)

    folder_indices: dict[str, list[int]] = defaultdict(list)
    index_to_msgid: dict[int, str] = {}
    unmatched_count = 0
    skipped_count = 0
    skipped_indices: list[int] = []

    with tqdm(total=total, desc="Classifying messages", unit="msg") as bar:
        for i, msg in enumerate(mbox):
            if limit and i >= limit:
                break

            msg_id = (msg.get("message-id") or "").strip()

            # Skip messages already successfully uploaded in a previous run
            if msg_id and msg_id in uploaded_ids:
                skipped_count += 1
                skipped_indices.append(i)
                bar.update(1)
                continue

            header_dict = {k.lower(): v for k, v in msg.items()}

            if no_move:
                target = default_folder
                unmatched_count += 1
                if verbose:
                    subject = msg.get("subject", "(no subject)")
                    print(f"  [{i+1}] {subject[:60]} → {target} (--no-move)")
            else:
                matching_rule = find_matching_rule(header_dict, sorted_rules)
                if matching_rule:
                    action = matching_rule.get("action") or {}
                    if not action:
                        actions = matching_rule.get("actions") or []
                        action = next((a for a in actions if a.get("type") == "move"), {})
                    target = action.get("target", default_folder)

                    if verbose:
                        subject = msg.get("subject", "(no subject)")
                        print(f"  [{i+1}] {subject[:60]} → {target} (rule: {matching_rule.get('name', '?')})")
                else:
                    target = default_folder
                    unmatched_count += 1
                    if verbose:
                        subject = msg.get("subject", "(no subject)")
                        print(f"  [{i+1}] {subject[:60]} → {target} (no rule matched)")

            folder_indices[target].append(i)
            index_to_msgid[i] = msg_id
            bar.update(1)

    if skipped_count:
        print(f"   ⏭️  Skipped {skipped_count} already-uploaded messages (from progress file)")

    return dict(folder_indices), unmatched_count, index_to_msgid, skipped_indices


# ---------------------------------------------------------------------------
# Dry-run display
# ---------------------------------------------------------------------------

def _print_dry_run_plan(
    folder_indices: dict[str, list[int]],
    unmatched_count: int,
    default_folder: str,
    total: int,
) -> None:
    """Print a human-readable dry-run summary table."""
    print("\nDry-run \u2014 mbox-import (nothing will be uploaded)")
    print(f"   Total messages: {total}\n")
    print(f"   {'Messages':>9}   Folder")
    print("   " + "\u2500" * 50)

    sorted_items = sorted(folder_indices.items(), key=lambda kv: len(kv[1]), reverse=True)
    for folder, indices in sorted_items:
        count = len(indices)
        if folder == default_folder and unmatched_count > 0:
            matched_count = count - unmatched_count
            if matched_count > 0:
                print(f"   {matched_count:>9}   {folder}")
            print(f"   {unmatched_count:>9}   {folder}  \u2190 default, no rule matched")
        else:
            print(f"   {count:>9}   {folder}")
    print()


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _append_folder_batch(
    client: imaplib.IMAP4_SSL,
    folder: str,
    indices: list[int],
    mbox: mailbox.mbox,
    *,
    preserve_flags: bool,
    verbose: bool,
    logger: JsonLogger,
    err_mbox: Optional[mailbox.mbox] = None,
    err_lock: Optional[threading.Lock] = None,
    progress_path: Optional[Path] = None,
    progress_lock: Optional[threading.Lock] = None,
    index_to_msgid: Optional[dict[int, str]] = None,
    show_inner_bar: bool = True,
) -> tuple[int, int, list[int], list[int]]:
    """Upload messages (read one-at-a-time by index) to one IMAP folder via APPEND.

    Returns (uploaded, failed, successful_indices, failed_indices).
    Neither list is flushed from the mbox here — caller does one batch flush at
    the end to avoid invalidating remaining indices mid-run.

    err_lock and progress_lock should be supplied when called from multiple threads
    to serialise writes to the shared err_mbox and progress file.
    """
    if not _ensure_folder(client, folder, logger):
        if err_mbox is not None:
            with (err_lock or _null_context()):
                for idx in indices:
                    try:
                        err_mbox.add(mbox[idx])
                    except Exception:
                        pass
                err_mbox.flush()
        return 0, len(indices), [], list(indices)

    quoted = _quote_mailbox(folder)
    uploaded = 0
    failed = 0
    successful_indices: list[int] = []
    failed_indices: list[int] = []

    bar = tqdm(indices, desc=f"  {folder}", unit="msg", position=1,
               leave=False, disable=not show_inner_bar)
    for idx in bar:
        msg = None
        try:
            msg = mbox[idx]
            payload = _message_to_crlf_bytes(msg)
            flags = _mbox_flags_to_imap(msg) if preserve_flags else None
            date_time = _get_delivery_time(msg)

            typ, resp = client.append(quoted, flags, date_time, payload)

            if typ != "OK":
                resp_text = " ".join(
                    r.decode("utf-8", "ignore") if isinstance(r, bytes) else str(r)
                    for r in (resp or [])
                ).lower()
                if any(k in resp_text for k in ("trycreate", "no such mailbox", "does not exist")):
                    client.create(quoted)
                    typ, resp = client.append(quoted, flags, date_time, payload)

            if typ == "OK":
                uploaded += 1
                successful_indices.append(idx)

                # Persist progress immediately so a restart can skip this message
                if progress_path is not None and index_to_msgid is not None:
                    msg_id = index_to_msgid.get(idx, "")
                    if msg_id:
                        with (progress_lock or _null_context()):
                            _record_uploaded(progress_path, msg_id)

                if verbose:
                    subject = msg.get("subject", "(no subject)")
                    print(f"    ✓ {subject[:60]}")
            else:
                failed += 1
                failed_indices.append(idx)
                logger.log("WARN", "append_failed",
                           {"folder": folder, "resp": str(resp)})
                if err_mbox is not None and msg is not None:
                    with (err_lock or _null_context()):
                        try:
                            err_mbox.add(msg)
                        except Exception:
                            pass  # Message too corrupted to write to error mbox
                msg_id = index_to_msgid.get(idx, "")
                if msg_id:
                    with (progress_lock or _null_context()):
                        _record_uploaded(progress_path, msg_id)

        except imaplib.IMAP4.abort:
            # Connection is dead; propagate so the caller can reconnect and retry.
            raise
        except Exception as exc:
            failed += 1
            failed_indices.append(idx)
            logger.log("WARN", "append_error",
                       {"folder": folder, "error": str(exc)})
            if err_mbox is not None and msg is not None:
                with (err_lock or _null_context()):
                    try:
                        err_mbox.add(msg)
                    except Exception:
                        pass  # Message too corrupted to write to error mbox
            msg_id = index_to_msgid.get(idx, "")
            if msg_id:
                with (progress_lock or _null_context()):
                    _record_uploaded(progress_path, msg_id)

    if err_mbox is not None:
        with (err_lock or _null_context()):
            err_mbox.flush()
    bar.close()
    return uploaded, failed, successful_indices, failed_indices


@contextmanager
def _null_context():
    """No-op context manager used when no lock is needed (single-threaded path)."""
    yield


def _safe_logout(client: imaplib.IMAP4_SSL) -> None:
    """Attempt IMAP logout, silently ignoring errors on already-dead connections."""
    try:
        client.logout()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_mbox_import(
    mbox_path: Path,
    *,
    rules_dir: Path,
    secrets_path: Path,
    default_folder: str,
    dry_run: bool,
    verbose: bool,
    limit: Optional[int],
    preserve_flags: bool,
    error_mbox_path: Optional[Path],
    logger: JsonLogger,
    parallel_workers: int = 1,
    no_move: bool = False,
    folder_order: str = "alpha",
) -> int:
    """Main entry point for the mbox-import command."""

    # Progress file sits alongside the mbox: e.g. Folder2.mbox.progress
    progress_path = mbox_path.with_suffix(mbox_path.suffix + ".progress")

    # Phase 1: Load rules
    rules = load_rules(rules_dir, logger)
    logger.log("INFO", "rules_loaded", {"count": len(rules)},
               console=f"📋 Loaded {len(rules)} rules")

    if not rules:
        logger.log("WARN", "no_rules", {},
                   console="⚠️  No rules found — all messages will go to the default folder")

    # Phase 2: Load progress (already-uploaded Message-IDs from a previous run)
    uploaded_ids = _load_progress(progress_path)
    if uploaded_ids:
        logger.log("INFO", "progress_loaded", {"count": len(uploaded_ids)},
                   console=f"♻️  Resuming: {len(uploaded_ids)} messages already uploaded, skipping them")

    # Phase 3: Classify — streams through mbox storing only integer indices
    if no_move:
        logger.log("INFO", "mbox_no_move", {},
                   console=f"⚙️  --no-move: skipping rule routing, all messages → {default_folder}")
    logger.log("INFO", "mbox_classify_start", {"path": str(mbox_path)},
               console=f"📂 Classifying messages from {mbox_path.name} ...")

    folder_indices, unmatched_count, index_to_msgid, skipped_indices = _classify_messages(
        mbox_path, rules, default_folder, limit, verbose, uploaded_ids, no_move=no_move
    )

    total = sum(len(v) for v in folder_indices.values())
    matched_count = total - unmatched_count
    logger.log("INFO", "classify_done", {
        "total": total, "matched": matched_count, "unmatched": unmatched_count,
        "folders": len(folder_indices),
    }, console=f"✅ {total} messages → {len(folder_indices)} folders "
               f"({matched_count} matched by rules, {unmatched_count} unmatched)")

    # Phase 4: Dry-run output
    if dry_run:
        _print_dry_run_plan(folder_indices, unmatched_count, default_folder, total)
        return 0

    # Phase 5: Connect and upload — reads messages one-at-a-time from mbox
    # Auto-generate error mbox path if not specified
    if error_mbox_path is None:
        date_str = datetime.date.today().isoformat()
        stem = mbox_path.stem
        error_mbox_path = mbox_path.parent / f"{date_str}-{stem}-error.mbox"

    err_mbox = mailbox.mbox(str(error_mbox_path))
    logger.log("INFO", "error_mbox_open", {"path": str(error_mbox_path)},
               console=f"📝 Failed messages will be saved to {error_mbox_path.name}")

    total_uploaded = 0
    total_failed = 0
    all_successful_indices: list[int] = []
    all_failed_indices: list[int] = []

    num_workers = max(1, min(parallel_workers, len(folder_indices)))

    _RECONNECT_ATTEMPTS = 3

    if num_workers == 1:
        # --- Sequential path (single connection) ---
        logger.log("INFO", "imap_connect", {}, console="🔐 Connecting to IMAP ...")
        client = imap_login(secrets_path, logger)
        mbox = mailbox.mbox(str(mbox_path))
        try:
            # Determine folder processing order.
            if folder_order == "most-first":
                _folder_seq = sorted(
                    folder_indices.keys(),
                    key=lambda f: len(folder_indices[f]),
                    reverse=True,
                )
            elif folder_order == "least-first":
                _folder_seq = sorted(
                    folder_indices.keys(),
                    key=lambda f: len(folder_indices[f]),
                )
            else:
                # alpha: default folder first (INBOX visible early), then alphabetical
                _folder_seq = sorted(
                    folder_indices.keys(),
                    key=lambda f: (0 if f == default_folder else 1, f),
                )
            folders_bar = tqdm(
                ((f, folder_indices[f]) for f in _folder_seq),
                desc="Uploading",
                unit="folder",
                position=0,
                total=len(folder_indices),
            )
            for folder, indices in folders_bar:
                folders_bar.set_postfix({"folder": folder[:30]})

                # Retry the folder batch on connection drops, reconnecting each time.
                folder_uploaded = 0
                folder_failed = 0
                folder_successful: list[int] = []
                folder_failed_idxs: list[int] = []
                remaining = list(indices)

                for attempt in range(1, _RECONNECT_ATTEMPTS + 1):
                    try:
                        uploaded, failed, successful_indices, failed_indices = _append_folder_batch(
                            client, folder, remaining, mbox,
                            preserve_flags=preserve_flags,
                            verbose=verbose,
                            logger=logger,
                            err_mbox=err_mbox,
                            progress_path=progress_path,
                            index_to_msgid=index_to_msgid,
                        )
                        folder_uploaded += uploaded
                        folder_failed += failed
                        folder_successful.extend(successful_indices)
                        folder_failed_idxs.extend(failed_indices)
                        break  # success — move on to next folder
                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                        logger.log("WARN", "imap_conn_dropped", {
                            "folder": folder, "attempt": attempt, "error": str(exc),
                        }, console=f"  ⚠️  Connection dropped (attempt {attempt}/{_RECONNECT_ATTEMPTS}): {exc}")
                        _safe_logout(client)
                        if attempt < _RECONNECT_ATTEMPTS:
                            logger.log("INFO", "imap_reconnect", {},
                                       console="  🔄 Reconnecting ...")
                            client = imap_login(secrets_path, logger)
                            # Exclude already-uploaded indices from the retry
                            succ_set = set(folder_successful)
                            remaining = [i for i in remaining if i not in succ_set]
                        else:
                            logger.log("ERROR", "imap_folder_give_up", {"folder": folder},
                                       console=f"  ❌ Giving up on {folder} after {_RECONNECT_ATTEMPTS} attempts")
                            succ_set = set(folder_successful)
                            give_up_indices = [i for i in remaining if i not in succ_set]
                            folder_failed += len(give_up_indices)
                            folder_failed_idxs.extend(give_up_indices)
                            if err_mbox is not None:
                                for idx in give_up_indices:
                                    try:
                                        err_mbox.add(mbox[idx])
                                    except Exception:
                                        pass
                                err_mbox.flush()

                total_uploaded += folder_uploaded
                total_failed += folder_failed
                all_successful_indices.extend(folder_successful)
                all_failed_indices.extend(folder_failed_idxs)
                logger.log("INFO", "folder_upload_done", {
                    "folder": folder, "uploaded": folder_uploaded, "failed": folder_failed,
                }, console=f"  📤 {folder}: {folder_uploaded} uploaded, {folder_failed} failed")
        finally:
            _safe_logout(client)
            if err_mbox is not None:
                err_mbox.close()
    else:
        # --- Parallel path (connection pool) ---
        logger.log("INFO", "imap_connect", {"workers": num_workers},
                   console=f"🔐 Connecting to IMAP (pool: {num_workers} workers) ...")
        pool = IMAPConnectionPool(secrets_path, max_connections=num_workers, logger=logger)
        err_lock = threading.Lock()
        progress_lock = threading.Lock()
        results_lock = threading.Lock()

        # Determine folder processing order for parallel upload.
        if folder_order == "most-first":
            folder_items = sorted(
                folder_indices.items(),
                key=lambda kv: len(kv[1]),
                reverse=True,
            )
        elif folder_order == "least-first":
            folder_items = sorted(
                folder_indices.items(),
                key=lambda kv: len(kv[1]),
            )
        else:
            # alpha: default folder first, then alphabetical
            folder_items = sorted(
                folder_indices.items(),
                key=lambda kv: (0 if kv[0] == default_folder else 1, kv[0]),
            )
        folders_bar = tqdm(total=len(folder_items), desc="Uploading", unit="folder", position=0)

        def _worker(folder: str, indices: list[int]) -> tuple[str, int, int, list[int], list[int]]:
            # Each worker opens its own mbox handle (mbox reads are not thread-safe)
            worker_mbox = mailbox.mbox(str(mbox_path))
            conn = pool.acquire()
            conn_healthy = True
            up: int = 0
            fail: int = 0
            succ: list[int] = []
            fail_idxs: list[int] = []
            try:
                up, fail, succ, fail_idxs = _append_folder_batch(
                    conn, folder, indices, worker_mbox,
                    preserve_flags=preserve_flags,
                    verbose=verbose,
                    logger=logger,
                    err_mbox=err_mbox,
                    err_lock=err_lock,
                    progress_path=progress_path,
                    progress_lock=progress_lock,
                    index_to_msgid=index_to_msgid,
                    show_inner_bar=False,
                )
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                conn_healthy = False
                fail = len(indices)
                fail_idxs = list(indices)
                logger.log("WARN", "imap_worker_conn_dropped", {
                    "folder": folder, "error": str(exc),
                }, console=f"  ⚠️  Worker connection dropped for {folder}: {exc}")
                if err_mbox is not None:
                    with err_lock:
                        for idx in indices:
                            try:
                                err_mbox.add(worker_mbox[idx])
                            except Exception:
                                pass
                        err_mbox.flush()
            finally:
                if conn_healthy:
                    pool.release(conn)
                else:
                    _safe_logout(conn)
                    # Replace the dead connection in the pool with a fresh one
                    try:
                        fresh = imap_login(secrets_path, logger)
                        pool.release(fresh)
                    except Exception:
                        with pool._lock:
                            pool._created -= 1
                worker_mbox.close()
            return folder, up, fail, succ, fail_idxs

        try:
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                future_to_folder = {
                    executor.submit(_worker, folder, indices): folder
                    for folder, indices in folder_items
                }
                for future in as_completed(future_to_folder):
                    folder, uploaded, failed, successful_indices, failed_indices = future.result()
                    with results_lock:
                        total_uploaded += uploaded
                        total_failed += failed
                        all_successful_indices.extend(successful_indices)
                        all_failed_indices.extend(failed_indices)
                    folders_bar.update(1)
                    folders_bar.set_postfix({"last": folder[:30]})
                    logger.log("INFO", "folder_upload_done", {
                        "folder": folder, "uploaded": uploaded, "failed": failed,
                    }, console=f"  📤 {folder}: {uploaded} uploaded, {failed} failed")
        finally:
            folders_bar.close()
            pool.shutdown()
            if err_mbox is not None:
                err_mbox.close()

    # Phase 6: Remove processed messages from mbox in one batch flush.
    # Covers: successfully uploaded this run, already uploaded in a prior run
    # (skipped during classification), and messages that failed and were saved to
    # the error mbox — retries should be done from the error file, not the source.
    indices_to_remove = list(set(all_successful_indices) | set(skipped_indices) | set(all_failed_indices))
    if indices_to_remove:
        logger.log("INFO", "mbox_cleanup_start", {"count": len(indices_to_remove)},
                   console=f"🧹 Removing {len(indices_to_remove)} uploaded messages from {mbox_path.name} ...")
        cleanup_mbox = mailbox.mbox(str(mbox_path))
        for idx in indices_to_remove:
            try:
                cleanup_mbox.remove(idx)
            except Exception:
                pass
        cleanup_mbox.flush()
        cleanup_mbox.close()

        remaining = sum(1 for _ in mailbox.mbox(str(mbox_path)))
        logger.log("INFO", "mbox_cleanup_done", {"remaining": remaining},
                   console=f"✅ Cleanup complete — {remaining} messages remain in {mbox_path.name}")

    # Phase 7: Clean up progress file if run completed without failures
    if total_failed == 0 and progress_path.exists():
        progress_path.unlink()
        logger.log("INFO", "progress_cleared", {},
                   console=f"🗑️  Progress file removed (all messages uploaded successfully)")
    elif progress_path.exists():
        logger.log("INFO", "progress_kept", {"path": str(progress_path)},
                   console=f"♻️  Progress file kept at {progress_path.name} — re-run to retry failures")

    # Phase 8: Summary
    status = "✅" if total_failed == 0 else "⚠️"
    summary = f"\n{status} mbox-import complete: {total_uploaded} uploaded, {total_failed} failed"
    if total_failed > 0:
        summary += f"\n   Failed messages saved to: {error_mbox_path}"
    logger.log("INFO", "mbox_import_done", {
        "total_uploaded": total_uploaded, "total_failed": total_failed,
    }, console=summary)

    return 0 if total_failed == 0 else 1
