"""IMAP interaction helpers."""
from __future__ import annotations

import imaplib
import json
import re
import sys
from pathlib import Path
from typing import Iterable, List

from tqdm import tqdm
from core.logging_utils import JsonLogger


def _ensure_large_imap_buffer() -> None:
    """Remove imaplib's line-length cap so large SEARCH responses (e.g. huge INBOX) don't abort."""
    imaplib._MAXLINE = sys.maxsize


_FETCH_FOLD_RE = re.compile(rb"\bFETCH\b")
_LITERAL_END_RE = re.compile(rb"\{\d+\}\r?\n?$")


def _patch_fold_aware_readline(mail: imaplib.IMAP4) -> None:
    """
    Monkey-patch ``mail.readline`` to handle Dovecot's folded FETCH responses.

    Dovecot folds long FETCH envelopes across multiple lines, e.g.:

        * 1 FETCH (UID 1234567
         BODY[HEADER] {4511}
        <header bytes>
        )

    imaplib reads one line at a time and only looks for the literal-size
    marker ``{N}`` at the *end* of that line.  When the line is folded, the
    ``{N}`` appears on the *next* line, which imaplib treats as an
    "unexpected response" and raises IMAP4.error — corrupting the connection.

    This patch intercepts ``readline()`` at the instance level (avoiding the
    read-only ``IMAP4.file`` property introduced in Python 3.12) and joins
    any whitespace-prefixed continuation lines before imaplib sees them.
    """
    _orig = mail.readline
    _buf: list[bytes] = []   # at most one look-ahead line

    def _readline() -> bytes:
        line = _buf.pop() if _buf else _orig()
        for _ in range(20):   # cap to guard against pathological servers
            stripped = line.rstrip(b"\r\n")
            if not (
                line.startswith(b"* ")
                and _FETCH_FOLD_RE.search(line)
                and not _LITERAL_END_RE.search(line)
                and not stripped.endswith(b")")
            ):
                break
            nxt = _orig()
            if not nxt:
                break
            if nxt[:1] in (b" ", b"\t"):
                line = stripped + b" " + nxt.lstrip(b" \t")
            else:
                _buf.append(nxt)   # not a continuation — return next time
                break
        return line

    mail.readline = _readline


def imap_login(secrets_path: Path, logger: JsonLogger) -> imaplib.IMAP4:
    """Establish an authenticated IMAP session using the provided secrets file."""
    _ensure_large_imap_buffer()
    secrets_path = Path(secrets_path)
    if not secrets_path.exists():
        sys.exit(f"❌ Secrets file not found: {secrets_path}")
    with secrets_path.open(encoding="utf-8") as handle:
        secrets = json.load(handle)
    secrets_cfg = secrets["imap"]
    use_ssl = secrets_cfg.get("ssl", True)
    default_port = 993 if use_ssl else 143
    logger.log(
        "INFO",
        "imap_connect",
        {"host": secrets_cfg["host"], "user": secrets_cfg["username"]},
        console=f"🔐 Connecting as {secrets_cfg['username']}",
    )
    if use_ssl:
        mail = imaplib.IMAP4_SSL(secrets_cfg["host"], secrets_cfg.get("port", default_port))
    else:
        mail = imaplib.IMAP4(secrets_cfg["host"], secrets_cfg.get("port", default_port))
    mail.sock.settimeout(300)  # 5 min — large INBOX SEARCH/FETCH can be slow
    mail.login(secrets_cfg["username"], secrets_cfg["password"])
    _patch_fold_aware_readline(mail)
    return mail


def list_all_folders(client: imaplib.IMAP4, parent: str | None = None) -> List[str]:
    """
    List folders from IMAP server.

    Args:
        client: IMAP connection
        parent: If provided, list only direct children of this folder (non-recursive)
               If None, list all folders at top level

    Returns:
        List of folder names
    """
    if parent is None:
        mailbox_name = "*"
    else:
        # List direct children of parent: "INBOX/*" lists all direct children
        # Quote parent name if it contains spaces or special characters
        escaped_parent = parent.replace('\\', '\\\\').replace('"', '\\"')
        mailbox_name = f'"{escaped_parent}/*"'

    typ, data = client.list(directory='""', pattern=mailbox_name)
    if typ != "OK":
        raise RuntimeError(f"Unable to list folders: {mailbox_name}")
    folders: List[str] = []
    for line in data or []:
        if not line:
            continue
        parts = line.decode().split(' "/" ')
        if len(parts) == 2:
            folders.append(parts[1].strip('"'))
    return folders


def expand_folders_recursive(client: imaplib.IMAP4, folders: List[str], show_progress: bool = False) -> List[str]:
    """
    Expand a list of folders to include all their descendants.

    For each folder in the input list, recursively finds all subfolders and returns
    the complete flattened list. Useful for cache building with --folder-recursive.

    Args:
        client: IMAP connection
        folders: List of base folder names to expand
        show_progress: If True, display progress bar

    Returns:
        Flattened list of all folders and their descendants (deduplicated)
    """
    result = set()
    iterator = tqdm(folders, desc="📂 Expanding folders recursively", unit="folder", disable=not show_progress)

    def _expand_recursive(folder_name: str) -> None:
        """Recursively expand a folder and add it and all descendants."""
        if folder_name in result:
            return  # Already processed
        result.add(folder_name)

        try:
            children = list_all_folders(client, parent=folder_name)
            for child in children:
                _expand_recursive(child)
        except Exception:
            # If we can't list children, just continue with current folder
            pass

    for folder in iterator:
        _expand_recursive(folder)

    return sorted(list(result))


def get_folder_sizes(client: imaplib.IMAP4, folders: List[str], show_progress: bool = True) -> dict[str, int]:
    """
    Get message counts for multiple folders using IMAP STATUS command.

    Fast operation (~100ms per folder, typically <1s total for 10 folders).
    Folders that fail STATUS will have count = -1 (sorted to end).

    Args:
        client: IMAP connection
        folders: List of folder names
        show_progress: If True, display progress bar

    Returns:
        Dictionary mapping folder_name -> message_count
        Failed folders return count of -1
    """
    sizes: dict[str, int] = {}
    iterator = tqdm(folders, desc="📊 Counting folder sizes", unit="folder") if show_progress else folders
    for folder in iterator:
        try:
            typ, data = client.status(f'"{folder}"', "(MESSAGES)")
            if typ == "OK" and data and data[0]:
                # Parse response like: b'INBOX (MESSAGES 1234)'
                response = data[0].decode('utf-8', 'ignore')
                # Extract number from response
                match = re.search(r'MESSAGES\s+(\d+)', response)
                if match:
                    sizes[folder] = int(match.group(1))
                else:
                    sizes[folder] = -1
            else:
                sizes[folder] = -1
        except Exception:
            # If STATUS fails, mark as -1 (sort to end)
            sizes[folder] = -1
    return sizes


def safe_search_all(client: imaplib.IMAP4, undeleted_only: bool = False) -> Iterable[bytes]:
    """
    Search for all messages in the current folder.

    Args:
        client: IMAP connection
        undeleted_only: If True, only return messages NOT marked as \\Deleted

    Returns:
        List of message UIDs

    Raises:
        OSError: If a socket/network error occurs (connection is in bad state, caller must discard it)
    """
    import socket as _socket
    criteria = "UNDELETED" if undeleted_only else "ALL"
    try:
        typ, data = client.uid("SEARCH", None, criteria)
    except imaplib.IMAP4.error:
        return []
    except (_socket.timeout, OSError):
        # Let socket/network errors propagate so the caller can discard the connection
        raise
    if typ != "OK" or not data:
        return []

    results: list[bytes] = []
    for chunk in data:
        if not chunk or not isinstance(chunk, (bytes, bytearray)):
            continue
        results.extend(bytes(chunk).split())
    return results
