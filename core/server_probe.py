"""IMAP server capability and performance probe logic."""
from __future__ import annotations

import dataclasses
import imaplib
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Optional

from core.imap_client import imap_login, list_all_folders, get_folder_sizes, safe_search_all
from core.logging_utils import JsonLogger


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CapabilityResult:
    raw: list[str]
    performance: list[str]
    filtering: list[str]
    auth: list[str]
    other: list[str]
    error: Optional[str] = None


@dataclass
class ConnectionLimitResult:
    successful: int
    tested_counts: list[int]
    fail_at: Optional[int] = None
    error_type: Optional[str] = None
    cap_reached: bool = False
    error: Optional[str] = None


@dataclass
class LatencySample:
    command: str
    samples_ms: list[float]
    min_ms: float
    mean_ms: float
    p95_ms: float
    max_ms: float


@dataclass
class LatencyResult:
    folder: str
    n_samples: int
    samples: list[LatencySample]
    error: Optional[str] = None


@dataclass
class SpeedResult:
    operation: str
    folder: str
    total_messages: int
    batch_results: list[dict]
    error: Optional[str] = None


@dataclass
class SearchResult:
    folder: str
    total_messages: int
    search_all_ms: float
    uid_search_all_ms: float
    msg_per_sec: float
    error: Optional[str] = None


@dataclass
class FolderInfo:
    name: str
    flags: list[str]
    special_use: list[str]
    message_count: int
    read_only: bool = False


@dataclass
class ProbeResults:
    host: str
    port: int
    probed_at: str
    capabilities: Optional[CapabilityResult] = None
    connection_limit: Optional[ConnectionLimitResult] = None
    latency: Optional[LatencyResult] = None
    download: list[SpeedResult] = field(default_factory=list)
    upload: Optional[SpeedResult] = None
    search: list[SearchResult] = field(default_factory=list)
    folders: list[FolderInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Probe functions
# ---------------------------------------------------------------------------

_PERFORMANCE_CAPS = {"IDLE", "LITERAL+", "COMPRESS=DEFLATE", "CONDSTORE", "QRESYNC"}
_FILTERING_CAPS = {"MOVE", "UIDPLUS", "ESEARCH", "WITHIN", "SORT"}
_SPECIAL_USE_FLAGS = {
    r"\Archive", r"\Drafts", r"\Junk", r"\Sent", r"\Trash",
    r"\All", r"\Important", r"\Flagged",
}


def probe_capabilities(client: imaplib.IMAP4) -> CapabilityResult:
    """Probe IMAP capabilities and categorise them."""
    try:
        typ, data = client.capability()
        if typ != "OK" or not data:
            return CapabilityResult(raw=[], performance=[], filtering=[], auth=[], other=[],
                                    error="CAPABILITY command failed")

        raw_line = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
        tokens = raw_line.split()
        # Remove the leading "CAPABILITY" token if present
        skip = {"CAPABILITY", "IMAP4rev1", "IMAP4rev2"}
        tokens = [t for t in tokens if t not in skip]

        performance = [t for t in tokens if t in _PERFORMANCE_CAPS]
        filtering = [t for t in tokens if t in _FILTERING_CAPS]
        auth = [t for t in tokens if t.startswith("AUTH=")]
        other = [t for t in tokens
                 if t not in _PERFORMANCE_CAPS
                 and t not in _FILTERING_CAPS
                 and not t.startswith("AUTH=")]

        return CapabilityResult(raw=tokens, performance=performance,
                                filtering=filtering, auth=auth, other=other)
    except Exception as exc:
        return CapabilityResult(raw=[], performance=[], filtering=[], auth=[], other=[],
                                error=str(exc))


def probe_connection_limit(
    secrets_path: Path,
    max_test: int,
    logger: JsonLogger,
) -> ConnectionLimitResult:
    """Test how many simultaneous IMAP connections the server accepts."""
    if not secrets_path.exists():
        return ConnectionLimitResult(
            successful=0,
            tested_counts=[],
            error=f"Secrets file not found: {secrets_path}",
        )

    test_counts = [n for n in [1, 2, 4, 8, 16] if n <= max_test]
    open_conns: list[imaplib.IMAP4_SSL] = []
    successful = 0
    fail_at: Optional[int] = None
    error_type: Optional[str] = None

    try:
        for target_count in test_counts:
            # Open connections up to the target count
            while len(open_conns) < target_count:
                try:
                    conn = imap_login(secrets_path, logger)
                    open_conns.append(conn)
                except (imaplib.IMAP4.error, OSError, ConnectionRefusedError) as exc:
                    fail_at = len(open_conns) + 1
                    error_type = type(exc).__name__ + ": " + str(exc)
                    return ConnectionLimitResult(
                        successful=successful,
                        tested_counts=test_counts,
                        fail_at=fail_at,
                        error_type=error_type,
                    )
            successful = target_count
    finally:
        for conn in open_conns:
            try:
                conn.logout()
            except Exception:
                pass

    return ConnectionLimitResult(
        successful=successful,
        tested_counts=test_counts,
        fail_at=fail_at,
        error_type=error_type,
        cap_reached=(fail_at is None and successful == max(test_counts, default=0)),
    )


def _compute_latency_sample(client: imaplib.IMAP4, folder: str, command: str, n: int) -> LatencySample:
    """Collect n timing samples for a single IMAP command."""
    samples_ms: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        if command == "NOOP":
            client.noop()
        elif command == "SELECT":
            client.select(f'"{folder}"', readonly=True)
        elif command == "STATUS":
            client.status(f'"{folder}"', "(MESSAGES)")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples_ms.append(elapsed_ms)

    mean_ms = statistics.mean(samples_ms)
    if len(samples_ms) >= 2:
        p95_ms = statistics.quantiles(samples_ms, n=100)[94]
    else:
        p95_ms = samples_ms[0]

    return LatencySample(
        command=command,
        samples_ms=samples_ms,
        min_ms=min(samples_ms),
        mean_ms=mean_ms,
        p95_ms=p95_ms,
        max_ms=max(samples_ms),
    )


def probe_command_latency(client: imaplib.IMAP4, folder: str, n: int) -> LatencyResult:
    """Measure command latency for NOOP, SELECT, and STATUS."""
    try:
        # Select the folder first so NOOP has context
        client.select(f'"{folder}"', readonly=True)
        samples = []
        for cmd in ("NOOP", "SELECT", "STATUS"):
            s = _compute_latency_sample(client, folder, cmd, n)
            samples.append(s)
        return LatencyResult(folder=folder, n_samples=n, samples=samples)
    except Exception as exc:
        return LatencyResult(folder=folder, n_samples=n, samples=[], error=str(exc))


def probe_download_speed(client: imaplib.IMAP4, folder: str) -> SpeedResult:
    """Benchmark header download speed across several batch sizes."""
    try:
        client.select(f'"{folder}"', readonly=True)
        uids = list(safe_search_all(client))
        if not uids:
            return SpeedResult(operation="download", folder=folder, total_messages=0,
                               batch_results=[], error="folder is empty")

        total = len(uids)
        batch_sizes = [bs for bs in [10, 50, 200] if bs <= total]
        if not batch_sizes:
            batch_sizes = [total]

        batch_results = []
        for batch_size in batch_sizes:
            uid_set = b",".join(uids[:batch_size])
            t0 = time.perf_counter()
            typ, data = client.uid("FETCH", uid_set, "(BODY.PEEK[HEADER])")
            elapsed = time.perf_counter() - t0

            if typ != "OK" or not data:
                continue

            # Only count tuple items — imaplib interleaves b")" literals
            total_bytes = sum(
                len(item[1]) for item in data
                if isinstance(item, tuple) and len(item) > 1 and item[1]
            )
            actual_fetched = sum(1 for item in data if isinstance(item, tuple))
            kb = total_bytes / 1024
            msg_per_sec = actual_fetched / elapsed if elapsed > 0 else 0
            kb_per_sec = kb / elapsed if elapsed > 0 else 0

            batch_results.append({
                "batch_size": batch_size,
                "actual_fetched": actual_fetched,
                "elapsed_sec": round(elapsed, 3),
                "msg_per_sec": round(msg_per_sec, 1),
                "kb_per_sec": round(kb_per_sec, 1),
            })

        return SpeedResult(operation="download", folder=folder,
                           total_messages=total, batch_results=batch_results)
    except Exception as exc:
        return SpeedResult(operation="download", folder=folder, total_messages=0,
                           batch_results=[], error=str(exc))


def probe_upload_speed(client: imaplib.IMAP4, folder: str) -> SpeedResult:
    """Benchmark upload speed by appending synthetic messages then cleaning up."""
    tag = f"PROBE-{int(time.time())}-{os.getpid()}"
    known_uids: list[str] = []
    batch_results = []

    # Build synthetic messages: (size_kb, label)
    message_specs = [(1, "1 KB"), (10, "10 KB"), (100, "100 KB")]

    def _make_message(size_kb: int) -> bytes:
        msg = Message()
        msg["Subject"] = tag
        msg["From"] = "probe@localhost"
        msg["To"] = "probe@localhost"
        padding = "X" * (size_kb * 1024 - 200)
        msg.set_payload(padding)
        return msg.as_bytes()

    try:
        for size_kb, label in message_specs:
            raw = _make_message(size_kb)
            t0 = time.perf_counter()
            typ, append_data = client.append(
                f'"{folder}"', None,
                imaplib.Time2Internaldate(time.time()),
                raw,
            )
            elapsed = time.perf_counter() - t0

            if typ == "OK" and append_data:
                # Try to parse APPENDUID from response for cleanup
                response_str = (append_data[0] or b"").decode("utf-8", "ignore")
                uid_match = re.search(r"APPENDUID\s+\d+\s+(\d+)", response_str)
                if uid_match:
                    known_uids.append(uid_match.group(1))

            kb = len(raw) / 1024
            kb_per_sec = kb / elapsed if elapsed > 0 else 0
            msg_per_sec = 1 / elapsed if elapsed > 0 else 0

            batch_results.append({
                "batch_size": 1,
                "label": label,
                "actual_fetched": 1,
                "elapsed_sec": round(elapsed, 3),
                "msg_per_sec": round(msg_per_sec, 2),
                "kb_per_sec": round(kb_per_sec, 1),
            })

        return SpeedResult(operation="upload", folder=folder,
                           total_messages=len(message_specs), batch_results=batch_results)
    except Exception as exc:
        return SpeedResult(operation="upload", folder=folder, total_messages=0,
                           batch_results=[], error=str(exc))
    finally:
        # Always clean up probe messages
        try:
            client.select(f'"{folder}"')  # read-write for STORE
            # Search by subject tag first
            typ, search_data = client.uid("SEARCH", None, f'SUBJECT "{tag}"')
            uids_to_delete: list[str] = []
            if typ == "OK" and search_data and search_data[0]:
                uids_to_delete = search_data[0].decode().split()
            # Fallback to known UIDs from APPENDUID
            if not uids_to_delete:
                uids_to_delete = known_uids

            if uids_to_delete:
                uid_set = ",".join(uids_to_delete)
                client.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)")
                client.expunge()
        except Exception:
            pass


def probe_search_performance(client: imaplib.IMAP4, folder: str) -> SearchResult:
    """Benchmark SEARCH ALL and UID SEARCH ALL performance."""
    try:
        typ, select_data = client.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            return SearchResult(folder=folder, total_messages=0,
                                search_all_ms=0, uid_search_all_ms=0, msg_per_sec=0,
                                error=f"SELECT failed: {typ}")

        # imaplib.select() returns (typ, [b'N']) where N is the EXISTS count directly
        total = 0
        if select_data and select_data[0]:
            try:
                total = int(select_data[0])
            except (ValueError, TypeError):
                for item in select_data:
                    if isinstance(item, bytes):
                        m = re.search(rb"(\d+)", item)
                        if m:
                            total = int(m.group(1))
                            break

        if total == 0:
            return SearchResult(folder=folder, total_messages=0,
                                search_all_ms=0, uid_search_all_ms=0, msg_per_sec=0,
                                error="folder is empty")

        t0 = time.perf_counter()
        client.search(None, "ALL")
        search_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        client.uid("SEARCH", None, "ALL")
        uid_search_ms = (time.perf_counter() - t0) * 1000

        avg_ms = (search_ms + uid_search_ms) / 2
        msg_per_sec = total / (avg_ms / 1000) if avg_ms > 0 else 0

        return SearchResult(
            folder=folder,
            total_messages=total,
            search_all_ms=round(search_ms, 1),
            uid_search_all_ms=round(uid_search_ms, 1),
            msg_per_sec=round(msg_per_sec, 1),
        )
    except Exception as exc:
        return SearchResult(folder=folder, total_messages=0,
                            search_all_ms=0, uid_search_all_ms=0, msg_per_sec=0,
                            error=str(exc))


def probe_folder_attributes(client: imaplib.IMAP4) -> list[FolderInfo]:
    """List all folders and their attributes."""
    try:
        typ, data = client.list('""', '*')
        if typ != "OK" or not data:
            return []

        folders: list[FolderInfo] = []
        for line in data:
            if not line:
                continue
            decoded = line.decode() if isinstance(line, bytes) else str(line)

            # Split on ' "/" ' to separate flags+name
            parts = decoded.split(' "/" ')
            if len(parts) != 2:
                continue

            flags_part = parts[0].strip()
            name = parts[1].strip().strip('"')

            # Extract flags from (flags) prefix
            flag_match = re.search(r'\(([^)]*)\)', flags_part)
            flags: list[str] = []
            if flag_match:
                flag_str = flag_match.group(1).strip()
                flags = flag_str.split() if flag_str else []

            special_use = [f for f in flags if f in _SPECIAL_USE_FLAGS]

            folders.append(FolderInfo(
                name=name,
                flags=flags,
                special_use=special_use,
                message_count=0,
            ))

        # Bulk-fetch message counts
        folder_names = [f.name for f in folders]
        if folder_names:
            sizes = get_folder_sizes(client, folder_names, show_progress=False)
            for fi in folders:
                fi.message_count = sizes.get(fi.name, -1)

        return folders
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_ms(ms: float) -> str:
    """Format milliseconds as '12ms' or '1.2s'."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms)}ms"


def _fmt_kbps(kbps: float) -> str:
    """Format KB/s with 1 decimal for small values to avoid banker's-rounding to 0."""
    if kbps < 10:
        return f"{kbps:.1f} KB/s"
    return f"{kbps:.0f} KB/s"


def format_report(results: ProbeResults) -> str:
    """Format probe results as a human-readable report."""
    lines: list[str] = []
    lines.append("=== IMAP Server Probe Results ===")
    lines.append(f"Host: {results.host}  Port: {results.port}  Probed: {results.probed_at}")
    lines.append("")

    # Capabilities
    lines.append("[Capabilities]")
    cap = results.capabilities
    if cap is None or cap.error:
        reason = cap.error if cap else "not run"
        lines.append(f"  (skipped: {reason})")
    else:
        lines.append(f"  Performance: {' '.join(cap.performance) or '(none)'}")
        lines.append(f"  Filtering:   {' '.join(cap.filtering) or '(none)'}")
        lines.append(f"  Auth:        {' '.join(cap.auth) or '(none)'}")
        lines.append(f"  Other:       {' '.join(cap.other) or '(none)'}")
    lines.append("")

    # Connection concurrency
    lines.append("[Connection Concurrency]")
    cl = results.connection_limit
    if cl is None or cl.error:
        reason = cl.error if cl else "not run"
        lines.append(f"  (skipped: {reason})")
    else:
        lines.append(f"  Successful: {cl.successful} simultaneous connections")
        if cl.fail_at is not None:
            lines.append(f"  Failed at:  {cl.fail_at} ({cl.error_type or 'unknown error'})")
        elif cl.cap_reached:
            lines.append(f"  No failure up to {cl.successful} (test cap — retry with --max-connections N)")
        else:
            lines.append(f"  No failure detected up to {cl.successful} connections")
    lines.append("")

    # Command latency
    lines.append(f"[Command Latency] ({results.latency.n_samples if results.latency else 0} samples, "
                 f"{results.latency.folder if results.latency else '?'})")
    lat = results.latency
    if lat is None or lat.error:
        reason = lat.error if lat else "not run"
        lines.append(f"  (skipped: {reason})")
    else:
        for s in lat.samples:
            lines.append(
                f"  {s.command:<6}: min={_fmt_ms(s.min_ms):<6}  "
                f"mean={_fmt_ms(s.mean_ms):<6}  "
                f"p95={_fmt_ms(s.p95_ms):<6}  "
                f"max={_fmt_ms(s.max_ms)}"
            )
    lines.append("")

    # Download speed
    lines.append("[Download Speed]")
    if not results.download:
        lines.append("  (not run)")
    else:
        for dl in results.download:
            if dl.error:
                lines.append(f"  {dl.folder}: (skipped: {dl.error})")
                continue
            lines.append(f"  {dl.folder} ({dl.total_messages:,} messages)")
            for br in dl.batch_results:
                lines.append(
                    f"    batch={br['batch_size']:>3}:  "
                    f"{br['msg_per_sec']:>5.0f} msg/s  "
                    f"{br['kb_per_sec']:>6.0f} KB/s"
                )
    lines.append("")

    # Upload speed
    lines.append("[Upload Speed]")
    up = results.upload
    if up is None:
        lines.append("  (not run)")
    elif up.error:
        lines.append(f"  (skipped: {up.error})")
    else:
        lines.append(f"  ({up.folder})")
        for br in up.batch_results:
            label = br.get("label", f"{br['batch_size']} msg")
            lines.append(
                f"  {label:>6}:  "
                f"{br['msg_per_sec']:>5.2f} msg/s  "
                f"{_fmt_kbps(br['kb_per_sec'])}"
            )
    lines.append("")

    # Search performance
    lines.append("[Search Performance]")
    if not results.search:
        lines.append("  (not run)")
    else:
        for sr in results.search:
            if sr.error:
                lines.append(f"  {sr.folder}: (skipped: {sr.error})")
                continue
            lines.append(
                f"  {sr.folder} ({sr.total_messages:,} msg): "
                f"SEARCH={sr.msg_per_sec:.0f} msg/s  "
                f"(SEARCH={_fmt_ms(sr.search_all_ms)}, UID SEARCH={_fmt_ms(sr.uid_search_all_ms)})"
            )
    lines.append("")

    # Folder attributes
    lines.append("[Folder Attributes]")
    if not results.folders:
        lines.append("  (not run)")
    else:
        lines.append(f"  Total: {len(results.folders)} folders")
        special_counts: dict[str, int] = {}
        for fi in results.folders:
            for su in fi.special_use:
                key = su.lstrip("\\")
                special_counts[key] = special_counts.get(key, 0) + 1
        if special_counts:
            parts = "  ".join(f"{k}={v}" for k, v in sorted(special_counts.items()))
            lines.append(f"  Special-use: {parts}")
    lines.append("")

    return "\n".join(lines)


def save_json_report(results: ProbeResults, path: Path) -> None:
    """Save probe results as a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(results), f, indent=2, default=str)
