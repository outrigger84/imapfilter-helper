"""MBOX importer: classify messages by rules and upload directly to target IMAP folders."""
from __future__ import annotations

import datetime
import email.errors
import email.utils
import hashlib
import imaplib
import mailbox
import os
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, Optional

from tqdm import tqdm

from core.connection_pool import IMAPConnectionPool
from core.imap_client import imap_login
from core.logging_utils import JsonLogger
from core.rule_engine import decode_header_value, find_matching_rule, load_rules, _parse_header_date


def _quote_mailbox(name: str) -> str:
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        return name
    return f'"{name}"'


def _message_exists_in_folder(client: imaplib.IMAP4_SSL, message_id: str) -> bool:
    """Return True if a message with this Message-ID exists in the currently-selected folder.

    Passes the SEARCH criterion as bytes to prevent imaplib from wrapping the
    multi-word string in double-quotes (which turns it into a body-text search).
    """
    try:
        mid = message_id.strip()
        # Bytes arg bypasses imaplib._checkquote so the server receives:
        #   SEARCH HEADER Message-ID <value>
        # instead of:
        #   SEARCH "HEADER Message-ID <value>"  (body-text search — wrong)
        criterion = ("HEADER Message-ID " + mid).encode("ascii", "ignore")
        typ, dat = client._simple_command("SEARCH", criterion)
        if typ != "OK":
            return False
        # dat[0] is b'1 3 7' (seq nums) or b'' when nothing matches
        return bool(dat and dat[0] and dat[0].strip())
    except Exception:
        return False


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


# Headers that must be removed before uploading to iCloud IMAP.
# X-Apple-Action / X-Apple-MoveToFolder tell the server to execute a deferred
# action (e.g. move to INBOX) when the message arrives, which conflicts with
# the target folder and causes [UNAVAILABLE] Unexpected exception.
# X-Apple-UUID lets the server detect a duplicate of an already-stored message.
# The ICL and Mozilla headers are internal metadata with no meaning on re-import.
_STRIP_BEFORE_UPLOAD: frozenset[str] = frozenset({
    "x-apple-action",
    "x-apple-movetofolder",
    "x-apple-uuid",
    "x-icl-info",
    "x-icl-score",
    "x-mozilla-status",
    "x-mozilla-status2",
    "x-mozilla-keys",
})


def _message_to_crlf_bytes(msg: mailbox.mboxMessage) -> bytes:
    """Convert mboxMessage to RFC-2822 bytes with CRLF line endings."""
    import io
    import email.generator as _eg

    for header in _STRIP_BEFORE_UPLOAD:
        del msg[header]  # no-op if absent; removes all instances if present

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
    """Select or create an IMAP folder. Returns True if usable.

    Tracks the last-selected folder as an attribute on the connection object
    (connections are pooled/reused across worker threads, so this can't live
    in thread-local state) and skips the SELECT round trip when the
    connection is already sitting on the target folder. CREATE does not
    implicitly select the mailbox per the IMAP spec, so the CREATE branch
    must issue a real SELECT before the "selected" state can be trusted or
    cached — skipping that would leave _message_exists_in_folder()'s bare
    SEARCH (no mailbox argument) silently querying the wrong folder.
    """
    if getattr(client, "_ih_selected_folder", None) == folder:
        return True

    quoted = _quote_mailbox(folder)
    typ, _ = client.select(quoted, readonly=True)
    if typ == "OK":
        client._ih_selected_folder = folder
        return True

    typ, resp = client.create(quoted)
    if typ != "OK":
        logger.log("ERROR", "folder_create_failed", {"folder": folder, "resp": str(resp)},
                   console=f"❌ Could not create folder: {folder}")
        return False

    logger.log("INFO", "folder_created", {"folder": folder},
               console=f"📁 Created folder: {folder}")

    typ, resp = client.select(quoted, readonly=True)
    if typ == "OK":
        client._ih_selected_folder = folder
        return True

    logger.log("ERROR", "folder_select_after_create_failed", {"folder": folder, "resp": str(resp)},
               console=f"❌ Could not select newly created folder: {folder}")
    return False


# ---------------------------------------------------------------------------
# Progress file — tracks uploaded Message-IDs for crash recovery
# ---------------------------------------------------------------------------

_MAX_FILENAME_BYTES = 255  # ext4/most POSIX filesystems


def _progress_path_for(mbox_path: Path) -> Path:
    """Derive the progress-file path for one mbox file.

    Normally just appends .progress to the filename, but some mbox files
    accumulate very long names (e.g. repeated failed-run prefixes from
    earlier tooling), and a name that's already near the filesystem's
    255-byte limit overflows once .progress is appended, raising
    ENAMETOOLONG. Fall back to a short, deterministic hash-based name in
    that case so progress tracking still works and stays consistently keyed
    to the same source file across runs (needed for resume).
    """
    candidate = mbox_path.with_suffix(mbox_path.suffix + ".progress")
    if len(candidate.name.encode("utf-8", "surrogateescape")) <= _MAX_FILENAME_BYTES:
        return candidate
    digest = hashlib.sha256(str(mbox_path).encode("utf-8", "surrogateescape")).hexdigest()[:16]
    return mbox_path.parent / f".mbox_import_progress_{digest}.progress"


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
    seen_keys: Optional[dict[str, tuple[Path, int]]] = None,
    dedup: bool = True,
) -> tuple[dict[str, list[int]], int, dict[int, str], list[int], list[int]]:
    """Stream through MBOX and record the target folder index for each message.

    Skips messages whose Message-ID is already in uploaded_ids (progress recovery).

    When dedup is enabled, also skips messages whose dedup key (Message-ID,
    or a content hash when Message-ID is absent) was already seen — either
    earlier in this same file or in a previously classified file sharing the
    same seen_keys dict, so duplicates anywhere in the batch are only
    uploaded once.

    Returns:
        folder_indices    — folder name → list of integer mbox keys
        unmatched_count   — messages routed to default_folder due to no rule match
        index_to_msgid    — mbox key → Message-ID (empty string if header absent)
        skipped_indices   — mbox keys skipped because already uploaded (for cleanup)
        duplicate_indices — mbox keys skipped as duplicates of an earlier message
                             in this batch (for cleanup)
    """
    if seen_keys is None:
        seen_keys = {}
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
    duplicate_count = 0
    duplicate_indices: list[int] = []

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

            if dedup:
                # Message-ID is the standard identity when present; fall back to
                # a content hash for messages missing it (malformed/spam mail)
                # so those can still be deduplicated across the batch.
                dedup_key = (
                    f"id:{msg_id}" if msg_id
                    else f"sha256:{hashlib.sha256(_message_to_crlf_bytes(msg)).hexdigest()}"
                )
                if dedup_key in seen_keys:
                    duplicate_count += 1
                    duplicate_indices.append(i)
                    index_to_msgid[i] = msg_id
                    bar.update(1)
                    continue
                seen_keys[dedup_key] = (mbox_path, i)

            # msg.items() can yield email.header.Header objects (instead of
            # str) for headers containing raw 8-bit bytes that aren't valid
            # encoded-words; normalize so downstream rule matching sees str.
            header_dict = {k.lower(): decode_header_value(v) for k, v in msg.items()}

            if no_move:
                target = default_folder
                unmatched_count += 1
                if verbose:
                    subject = header_dict.get("subject", "(no subject)")
                    print(f"  [{i+1}] {subject[:60]} → {target} (--no-move)")
            else:
                # Mbox messages carry no IMAP flags, but age_days_* conditions
                # can still be evaluated from the Date: header.
                msg_date = _parse_header_date(header_dict.get("date"))
                matching_rule = find_matching_rule(header_dict, sorted_rules, date=msg_date)
                if matching_rule:
                    action = matching_rule.get("action") or {}
                    if not action:
                        actions = matching_rule.get("actions") or []
                        action = next((a for a in actions if a.get("type") == "move"), {})
                    target = action.get("target", default_folder)

                    if verbose:
                        subject = header_dict.get("subject", "(no subject)")
                        print(f"  [{i+1}] {subject[:60]} → {target} (rule: {matching_rule.get('name', '?')})")
                else:
                    target = default_folder
                    unmatched_count += 1
                    if verbose:
                        subject = header_dict.get("subject", "(no subject)")
                        print(f"  [{i+1}] {subject[:60]} → {target} (no rule matched)")

            folder_indices[target].append(i)
            index_to_msgid[i] = msg_id
            bar.update(1)

    if skipped_count:
        print(f"   ⏭️  Skipped {skipped_count} already-uploaded messages (from progress file)")
    if duplicate_count:
        print(f"   🔁 Found {duplicate_count} duplicate(s) already seen in this batch (will not be uploaded)")

    return dict(folder_indices), unmatched_count, index_to_msgid, skipped_indices, duplicate_indices


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
# Batch work-item construction (multi-file import)
# ---------------------------------------------------------------------------

class WorkItem(NamedTuple):
    """One unit of upload work: a slice of one file's messages bound for one folder.

    Always scoped to a single mbox_path — Phase 6 cleanup removes uploaded
    messages from the source mbox by integer index, and an index is only
    meaningful within the mailbox.mbox object it came from. Merging indices
    from two different files into one WorkItem would corrupt that cleanup.
    """
    mbox_path: Path
    folder: str
    indices: list[int]


def _build_work_items(
    per_file_folder_indices: dict[Path, dict[str, list[int]]],
    chunk_size: int,
) -> list[WorkItem]:
    """Flatten per-file folder_indices into WorkItems, splitting oversized ones.

    Splitting a (file, folder) pair whose index list exceeds chunk_size lets
    multiple connections drain one large folder together instead of stranding
    it on a single worker, and caps how many messages a single connection
    drop punts to the error mbox.
    """
    items: list[WorkItem] = []
    for mbox_path, folder_indices in per_file_folder_indices.items():
        for folder, indices in folder_indices.items():
            if not indices:
                continue
            if chunk_size <= 0:
                items.append(WorkItem(mbox_path, folder, indices))
                continue
            for start in range(0, len(indices), chunk_size):
                items.append(WorkItem(mbox_path, folder, indices[start:start + chunk_size]))
    return items


def _order_work_items(
    work_items: list[WorkItem],
    folder_order: str,
    default_folder: str,
) -> list[WorkItem]:
    """Sort WorkItems so same-folder items land adjacent in the queue.

    Ranking is by each folder's aggregate message count across every file in
    the batch (not just the current item's own chunk), using the same
    most-first/least-first/alpha semantics as the single-file upload order.
    A stable sort preserves the incoming (file-major) order as the tiebreak.
    """
    folder_totals: dict[str, int] = defaultdict(int)
    for item in work_items:
        folder_totals[item.folder] += len(item.indices)

    if folder_order == "most-first":
        key = lambda item: -folder_totals[item.folder]
    elif folder_order == "least-first":
        key = lambda item: folder_totals[item.folder]
    else:
        key = lambda item: (0 if item.folder == default_folder else 1, item.folder)

    return sorted(work_items, key=key)


class _ThreadMboxHandleCache:
    """Per-thread cache of mailbox.mbox handles, keyed by file path.

    mailbox.mbox lazily builds its offset index (TOC) on first access via a
    full-file scan; reusing one handle per (thread, file) across chunks
    avoids re-scanning the whole file for every chunk. mailbox.mbox objects
    are not thread-safe, so a handle is only ever reused within the thread
    that created it — cross-thread reuse is not possible with this design.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._all_handles: list[mailbox.mbox] = []
        self._lock = threading.Lock()

    def get(self, mbox_path: Path) -> mailbox.mbox:
        cache = getattr(self._local, "handles", None)
        if cache is None:
            cache = {}
            self._local.handles = cache
        handle = cache.get(mbox_path)
        if handle is None:
            handle = mailbox.mbox(str(mbox_path))
            cache[mbox_path] = handle
            with self._lock:
                self._all_handles.append(handle)
        return handle

    def close_all(self) -> None:
        with self._lock:
            handles, self._all_handles = self._all_handles, []
        for handle in handles:
            try:
                handle.close()
            except Exception:
                pass


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
    message_bar: Optional[tqdm] = None,
    message_bar_lock: Optional[threading.Lock] = None,
) -> tuple[int, int, list[int], list[int]]:
    """Upload messages (read one-at-a-time by index) to one IMAP folder via APPEND.

    Returns (uploaded, failed, successful_indices, failed_indices).
    Neither list is flushed from the mbox here — caller does one batch flush at
    the end to avoid invalidating remaining indices mid-run.

    err_lock and progress_lock should be supplied when called from multiple threads
    to serialise writes to the shared err_mbox and progress file. message_bar is a
    single tqdm counter shared across every concurrent worker (one bar, not one per
    chunk, so N workers don't render N colliding bars) — message_bar_lock guards
    its .update() calls, since tqdm's own counter increment isn't thread-safe.
    """
    def _bump(n: int = 1) -> None:
        if message_bar is not None:
            with (message_bar_lock or _null_context()):
                message_bar.update(n)

    if not _ensure_folder(client, folder, logger):
        if err_mbox is not None:
            with (err_lock or _null_context()):
                for idx in indices:
                    try:
                        err_mbox.add(mbox[idx])
                    except Exception:
                        pass
                err_mbox.flush()
        _bump(len(indices))
        return 0, len(indices), [], list(indices)

    quoted = _quote_mailbox(folder)
    uploaded = 0
    failed = 0
    successful_indices: list[int] = []
    failed_indices: list[int] = []

    for i, idx in enumerate(indices):
        msg = None
        aborted = False
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
                    subject = decode_header_value(msg.get("subject", "(no subject)"))
                    print(f"    ✓ {subject[:60]}")
            else:
                resp_str = " ".join(
                    r.decode("utf-8", "ignore") if isinstance(r, bytes) else str(r)
                    for r in (resp or [])
                )
                # On [UNAVAILABLE], check whether the message already exists in the
                # folder (iCloud deduplicates by Message-ID and returns this error
                # rather than silently accepting the duplicate).
                msg_id = (index_to_msgid or {}).get(idx, "").strip()
                already_exists = False
                if "unavailable" in resp_str.lower() and msg_id:
                    if _message_exists_in_folder(client, msg_id):
                        already_exists = True
                        uploaded += 1
                        successful_indices.append(idx)
                        logger.log("INFO", "append_already_exists",
                                   {"folder": folder, "msg_id": msg_id},
                                   console="    ↩  Already in folder (skipped duplicate)")
                        if progress_path is not None and index_to_msgid is not None:
                            with (progress_lock or _null_context()):
                                _record_uploaded(progress_path, msg_id)

                if not already_exists:
                    failed += 1
                    logger.log("WARN", "append_failed",
                               {"folder": folder, "resp": resp_str},
                               console=f"    ⚠️  APPEND rejected ({resp_str})")
                    # Only mark the message as handled (removed from the source
                    # mbox in phase 6, skipped on retry) once it is safely in the
                    # error mbox — otherwise a failed message would exist nowhere.
                    if _preserve_failed_message(err_mbox, err_lock, msg, logger, folder):
                        failed_indices.append(idx)
                        if msg_id and progress_path is not None:
                            with (progress_lock or _null_context()):
                                _record_uploaded(progress_path, msg_id)

        except imaplib.IMAP4.abort:
            # Connection is dead; propagate so the caller can reconnect and
            # retry. This message and every remaining one in the chunk never
            # got attempted, so bump the counter for all of them at once in
            # the finally block below rather than leaving it permanently short.
            aborted = True
            raise
        except Exception as exc:
            failed += 1
            logger.log("WARN", "append_error",
                       {"folder": folder, "error": str(exc)})
            # Same invariant as the APPEND-rejected branch: never mark a failed
            # message as handled unless the error mbox actually holds it.
            if _preserve_failed_message(err_mbox, err_lock, msg, logger, folder):
                failed_indices.append(idx)
                msg_id = (index_to_msgid or {}).get(idx, "")
                if msg_id and progress_path is not None:
                    with (progress_lock or _null_context()):
                        _record_uploaded(progress_path, msg_id)
        finally:
            _bump(len(indices) - i if aborted else 1)

    if err_mbox is not None:
        with (err_lock or _null_context()):
            err_mbox.flush()
    return uploaded, failed, successful_indices, failed_indices


@contextmanager
def _null_context():
    """No-op context manager used when no lock is needed (single-threaded path)."""
    yield


def _preserve_failed_message(err_mbox, err_lock, msg, logger, folder: str) -> bool:
    """Write a failed message to the error mbox; return True only on success.

    Callers must not remove the message from the source mbox (or record it as
    processed) unless this returns True — a message that failed to upload and
    could not be preserved must stay in the source mbox for the next run.
    """
    if err_mbox is None or msg is None:
        return False
    with (err_lock or _null_context()):
        try:
            err_mbox.add(msg)
            return True
        except Exception as exc:
            logger.log(
                "ERROR",
                "error_mbox_write_failed",
                {"folder": folder, "error": str(exc)},
                console="    ❌ Could not preserve failed message in error mbox; keeping it in source",
            )
            return False


def _safe_logout(client: imaplib.IMAP4_SSL) -> None:
    """Attempt IMAP logout, silently ignoring errors on already-dead connections."""
    try:
        client.logout()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _cleanup_uploaded_messages(mbox_path: Path, indices_to_remove: set[int], logger: JsonLogger) -> None:
    """Remove processed messages from one source mbox in a single batch flush.

    Covers: successfully uploaded this run, already uploaded in a prior run
    (skipped during classification), and messages that failed and were saved
    to the error mbox — retries should be done from the error file, not the
    source.
    """
    if not indices_to_remove:
        return

    logger.log("INFO", "mbox_cleanup_start", {"count": len(indices_to_remove)},
               console=f"🧹 Removing {len(indices_to_remove)} processed messages from {mbox_path.name} ...")
    try:
        cleanup_mbox = mailbox.mbox(str(mbox_path))
        for idx in indices_to_remove:
            try:
                cleanup_mbox.remove(idx)
            except Exception:
                pass
        cleanup_mbox.flush()
        cleanup_mbox.close()
    except OSError as exc:
        if exc.errno != 36:  # ENAMETOOLONG
            raise
        # Source mbox filename is too long for mailbox.mbox to place its
        # temp file alongside it. Write a cleaned copy with a short name in
        # the same directory then atomically rename it into place.
        tmp_path = mbox_path.parent / f"_cleanup_{os.getpid()}.mbox"
        try:
            src = mailbox.mbox(str(mbox_path))
            dst = mailbox.mbox(str(tmp_path))
            for i, msg in enumerate(src):
                if i not in indices_to_remove:
                    dst.add(msg)
            dst.flush()
            dst.close()
            src.close()
            tmp_path.replace(mbox_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    remaining = sum(1 for _ in mailbox.mbox(str(mbox_path)))
    logger.log("INFO", "mbox_cleanup_done", {"remaining": remaining},
               console=f"✅ Cleanup complete — {remaining} messages remain in {mbox_path.name}")


def _finalize_progress_file(progress_path: Path, total_failed: int, logger: JsonLogger) -> None:
    """Remove one file's progress file on a clean run, or keep it for retry."""
    if total_failed == 0 and progress_path.exists():
        progress_path.unlink()
        logger.log("INFO", "progress_cleared", {"path": str(progress_path)},
                   console="🗑️  Progress file removed (all messages uploaded successfully)")
    elif progress_path.exists():
        logger.log("INFO", "progress_kept", {"path": str(progress_path)},
                   console=f"♻️  Progress file kept at {progress_path.name} — re-run to retry failures")


def _process_work_item(
    item: WorkItem,
    pool: IMAPConnectionPool,
    secrets_path: Path,
    mbox_cache: _ThreadMboxHandleCache,
    *,
    preserve_flags: bool,
    verbose: bool,
    logger: JsonLogger,
    err_mbox: mailbox.mbox,
    err_lock: threading.Lock,
    progress_path: Path,
    progress_lock: threading.Lock,
    index_to_msgid: dict[int, str],
    message_bar: Optional[tqdm] = None,
    message_bar_lock: Optional[threading.Lock] = None,
) -> tuple[Path, str, int, int, list[int], list[int]]:
    """Upload one WorkItem's messages using a pooled connection.

    On a connection drop, the whole item's indices are failed to the error
    mbox and the pool's dead connection is replaced — there is no in-place
    retry here (unlike the old single-connection sequential path). Chunking
    (see _build_work_items) keeps a single drop's blast radius bounded to at
    most chunk_size messages instead of a whole folder's backlog.
    """
    worker_mbox = mbox_cache.get(item.mbox_path)
    conn = pool.acquire()
    conn_healthy = True
    up: int = 0
    fail: int = 0
    succ: list[int] = []
    fail_idxs: list[int] = []
    try:
        up, fail, succ, fail_idxs = _append_folder_batch(
            conn, item.folder, item.indices, worker_mbox,
            preserve_flags=preserve_flags,
            verbose=verbose,
            logger=logger,
            err_mbox=err_mbox,
            err_lock=err_lock,
            progress_path=progress_path,
            progress_lock=progress_lock,
            index_to_msgid=index_to_msgid,
            message_bar=message_bar,
            message_bar_lock=message_bar_lock,
        )
    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
        conn_healthy = False
        fail = len(item.indices)
        fail_idxs = list(item.indices)
        logger.log("WARN", "imap_worker_conn_dropped", {
            "folder": item.folder, "file": str(item.mbox_path), "error": str(exc),
        }, console=f"  ⚠️  Worker connection dropped for {item.mbox_path.name} → {item.folder}: {exc}")
        with err_lock:
            for idx in item.indices:
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
    return item.mbox_path, item.folder, up, fail, succ, fail_idxs


def run_mbox_import(
    mbox_paths: list[Path],
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
    chunk_size: int = 2000,
    dedup: bool = True,
) -> int:
    """Main entry point for the mbox-import command.

    Accepts one or more mbox files. All files are classified up front, then
    uploaded from a single shared IMAP connection pool: work is split into
    per-(file, folder) chunks (see WorkItem/_build_work_items) so idle
    connections always have something to pull regardless of which file or
    folder it came from, and no single oversized destination folder can
    strand one connection while the rest sit idle.
    """

    # Phase 1: Load rules (shared across every file in the batch)
    rules = load_rules(rules_dir, logger)
    logger.log("INFO", "rules_loaded", {"count": len(rules)},
               console=f"📋 Loaded {len(rules)} rules")

    if not rules:
        logger.log("WARN", "no_rules", {},
                   console="⚠️  No rules found — all messages will go to the default folder")

    if no_move:
        logger.log("INFO", "mbox_no_move", {},
                   console=f"⚙️  --no-move: skipping rule routing, all messages → {default_folder}")

    # Phase 2/3: Load progress + classify every file up front. Classification
    # is local disk/CPU work with no network calls, so doing it for the whole
    # batch before any uploading starts is cheap relative to upload time and
    # keeps the design simple (no need to pipeline classify/upload).
    progress_paths: dict[Path, Path] = {}
    per_file_folder_indices: dict[Path, dict[str, list[int]]] = {}
    per_file_index_to_msgid: dict[Path, dict[int, str]] = {}
    per_file_skipped: dict[Path, list[int]] = {}
    per_file_unmatched: dict[Path, int] = {}
    per_file_duplicates: dict[Path, list[int]] = {}
    # Shared across every file's classify pass so a duplicate is caught no
    # matter which file in the batch it first appeared in.
    seen_keys: dict[str, tuple[Path, int]] = {}

    for mbox_path in mbox_paths:
        progress_path = _progress_path_for(mbox_path)
        progress_paths[mbox_path] = progress_path

        uploaded_ids = _load_progress(progress_path)
        if uploaded_ids:
            logger.log("INFO", "progress_loaded", {"count": len(uploaded_ids), "path": str(progress_path)},
                       console=f"♻️  Resuming {mbox_path.name}: {len(uploaded_ids)} messages already uploaded, skipping them")

        logger.log("INFO", "mbox_classify_start", {"path": str(mbox_path)},
                   console=f"📂 Classifying messages from {mbox_path.name} ...")

        folder_indices, unmatched_count, index_to_msgid, skipped_indices, duplicate_indices = _classify_messages(
            mbox_path, rules, default_folder, limit, verbose, uploaded_ids, no_move=no_move,
            seen_keys=seen_keys, dedup=dedup,
        )

        # Duplicates with a real Message-ID are recorded to this file's own
        # progress file (mirroring the reactive "already exists" path) so a
        # resumed run doesn't redo the dedup check for them.
        for dup_idx in duplicate_indices:
            dup_msg_id = index_to_msgid.get(dup_idx, "")
            if dup_msg_id:
                _record_uploaded(progress_path, dup_msg_id)

        total = sum(len(v) for v in folder_indices.values())
        matched_count = total - unmatched_count
        logger.log("INFO", "classify_done", {
            "path": str(mbox_path), "total": total, "matched": matched_count,
            "unmatched": unmatched_count, "folders": len(folder_indices),
            "duplicates": len(duplicate_indices),
        }, console=f"✅ {mbox_path.name}: {total} messages → {len(folder_indices)} folders "
                   f"({matched_count} matched by rules, {unmatched_count} unmatched, "
                   f"{len(duplicate_indices)} duplicate)")

        per_file_folder_indices[mbox_path] = folder_indices
        per_file_index_to_msgid[mbox_path] = index_to_msgid
        per_file_skipped[mbox_path] = skipped_indices
        per_file_unmatched[mbox_path] = unmatched_count
        per_file_duplicates[mbox_path] = duplicate_indices

    # Phase 4: Dry-run output
    if dry_run:
        grand_total = 0
        for mbox_path in mbox_paths:
            folder_indices = per_file_folder_indices[mbox_path]
            total = sum(len(v) for v in folder_indices.values())
            grand_total += total
            if len(mbox_paths) > 1:
                print(f"\n=== {mbox_path.name} ===")
            _print_dry_run_plan(folder_indices, per_file_unmatched[mbox_path], default_folder, total)
        if len(mbox_paths) > 1:
            print(f"Grand total across {len(mbox_paths)} files: {grand_total} messages\n")
        return 0

    # Phase 5: Build the shared work-item queue, then upload from one pool.
    work_items = _order_work_items(
        _build_work_items(per_file_folder_indices, chunk_size),
        folder_order, default_folder,
    )

    # Auto-generate a combined error mbox path if not specified. Failed
    # messages don't need to stay tied to their originating file — they just
    # need to be re-uploadable later via a follow-up mbox-import run.
    if error_mbox_path is None:
        date_str = datetime.date.today().isoformat()
        if len(mbox_paths) == 1:
            stem = mbox_paths[0].stem
            candidate = f"{date_str}-{stem}-error.mbox"
            if len(candidate.encode()) > 255:
                # Accumulated date prefixes make the name too long for the filesystem.
                # Keep the batch range (e.g. "1100001-1110000") as the unique identifier.
                m = re.search(r'(\d{5,}-\d{5,})', stem)
                short_stem = f"converted_merged.{m.group(1)}" if m else stem[-(220 - len(date_str)):]
                candidate = f"{date_str}-{short_stem}-error.mbox"
        else:
            candidate = f"{date_str}-mbox-import-batch-error.mbox"
        error_mbox_path = mbox_paths[0].parent / candidate

    err_mbox = mailbox.mbox(str(error_mbox_path))
    logger.log("INFO", "error_mbox_open", {"path": str(error_mbox_path)},
               console=f"📝 Failed messages will be saved to {error_mbox_path.name}")

    total_uploaded = 0
    total_failed = 0
    results_lock = threading.Lock()
    err_lock = threading.Lock()
    progress_lock = threading.Lock()
    message_bar_lock = threading.Lock()

    per_file_successful: dict[Path, list[int]] = defaultdict(list)
    per_file_failed: dict[Path, list[int]] = defaultdict(list)
    per_file_remaining: dict[Path, int] = defaultdict(int)
    for item in work_items:
        per_file_remaining[item.mbox_path] += 1

    def _finish_file(mbox_path: Path) -> None:
        """Run cleanup + progress-file finalization once a file's work is done."""
        successful = per_file_successful.get(mbox_path, [])
        failed = per_file_failed.get(mbox_path, [])
        skipped = per_file_skipped.get(mbox_path, [])
        duplicates = per_file_duplicates.get(mbox_path, [])
        indices_to_remove = set(successful) | set(skipped) | set(failed) | set(duplicates)
        _cleanup_uploaded_messages(mbox_path, indices_to_remove, logger)
        _finalize_progress_file(progress_paths[mbox_path], len(failed), logger)
        return len(failed)

    # Files that classified to zero work items (e.g. fully covered by a prior
    # progress file) never hit the remaining-counter decrement below —
    # finish them now instead of waiting on a counter that never reaches 0.
    for mbox_path in mbox_paths:
        if per_file_remaining.get(mbox_path, 0) == 0:
            total_failed += _finish_file(mbox_path)

    mbox_cache = _ThreadMboxHandleCache()
    pool: Optional[IMAPConnectionPool] = None
    try:
        if work_items:
            num_workers = max(1, min(parallel_workers, len(work_items)))
            logger.log("INFO", "imap_connect", {"workers": num_workers, "work_items": len(work_items)},
                       console=f"🔐 Connecting to IMAP (pool: {num_workers} workers, {len(work_items)} chunks) ...")
            pool = IMAPConnectionPool(secrets_path, max_connections=num_workers, logger=logger)
            total_messages = sum(len(item.indices) for item in work_items)
            items_bar = tqdm(total=len(work_items), desc="Uploading", unit="chunk", position=0)
            # One shared counter across every worker (not one bar per chunk,
            # which would render N colliding bars) so progress stays visible
            # even while a single chunk of up to chunk_size messages is
            # still in flight — chunk-level completion alone can go quiet
            # for a long stretch on a slow/high-latency IMAP server.
            messages_bar = tqdm(total=total_messages, desc="Messages", unit="msg", position=1)

            try:
                with ThreadPoolExecutor(max_workers=num_workers) as executor:
                    futures = [
                        executor.submit(
                            _process_work_item, item, pool, secrets_path, mbox_cache,
                            preserve_flags=preserve_flags,
                            verbose=verbose,
                            logger=logger,
                            err_mbox=err_mbox,
                            err_lock=err_lock,
                            progress_path=progress_paths[item.mbox_path],
                            progress_lock=progress_lock,
                            index_to_msgid=per_file_index_to_msgid[item.mbox_path],
                            message_bar=messages_bar,
                            message_bar_lock=message_bar_lock,
                        )
                        for item in work_items
                    ]
                    for future in as_completed(futures):
                        mbox_path, folder, uploaded, failed, successful_indices, failed_indices = future.result()
                        finished_file: Optional[Path] = None
                        with results_lock:
                            total_uploaded += uploaded
                            total_failed += failed
                            per_file_successful[mbox_path].extend(successful_indices)
                            per_file_failed[mbox_path].extend(failed_indices)
                            per_file_remaining[mbox_path] -= 1
                            if per_file_remaining[mbox_path] == 0:
                                finished_file = mbox_path
                        items_bar.update(1)
                        items_bar.set_postfix({"last": f"{mbox_path.name}:{folder}"[:40]})
                        logger.log("INFO", "chunk_upload_done", {
                            "file": str(mbox_path), "folder": folder, "uploaded": uploaded, "failed": failed,
                        }, console=f"  📤 {mbox_path.name} → {folder}: {uploaded} uploaded, {failed} failed")
                        if finished_file is not None:
                            # total_failed for this file was already accounted for
                            # above via per-chunk `failed` counts, so ignore the
                            # count _finish_file returns here — it only performs
                            # cleanup/progress-file work at this call site.
                            _finish_file(finished_file)
            finally:
                items_bar.close()
                messages_bar.close()
    finally:
        if pool is not None:
            pool.shutdown()
        mbox_cache.close_all()
        err_mbox.close()

    # Phase 8: Summary
    total_duplicates = sum(len(v) for v in per_file_duplicates.values())
    status = "✅" if total_failed == 0 else "⚠️"
    summary = f"\n{status} mbox-import complete: {total_uploaded} uploaded, {total_failed} failed"
    if total_duplicates:
        summary += f", {total_duplicates} duplicate (skipped, not uploaded)"
    if total_failed > 0:
        summary += f"\n   Failed messages saved to: {error_mbox_path}"
    logger.log("INFO", "mbox_import_done", {
        "total_uploaded": total_uploaded, "total_failed": total_failed, "total_duplicates": total_duplicates,
        "files": len(mbox_paths),
    }, console=summary)

    return 0 if total_failed == 0 else 1
