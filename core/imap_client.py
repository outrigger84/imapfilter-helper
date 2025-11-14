"""IMAP interaction helpers."""
from __future__ import annotations

import imaplib
import json
import sys
from pathlib import Path
from typing import Iterable, List

from core.logging_utils import JsonLogger


_IMAP_MAXLINE = 10_000_000


def _ensure_large_imap_buffer() -> None:
    """Increase imaplib's internal line buffer if the default is too small."""

    current = getattr(imaplib, "_MAXLINE", 0)
    if current < _IMAP_MAXLINE:
        imaplib._MAXLINE = _IMAP_MAXLINE


def imap_login(secrets_path: Path, logger: JsonLogger) -> imaplib.IMAP4_SSL:
    """Establish an authenticated IMAP session using the provided secrets file."""
    _ensure_large_imap_buffer()
    secrets_path = Path(secrets_path)
    if not secrets_path.exists():
        sys.exit(f"❌ Secrets file not found: {secrets_path}")
    with secrets_path.open(encoding="utf-8") as handle:
        secrets = json.load(handle)
    secrets_cfg = secrets["imap"]
    logger.log(
        "INFO",
        "imap_connect",
        {"host": secrets_cfg["host"], "user": secrets_cfg["username"]},
        console=f"🔐 Connecting as {secrets_cfg['username']}",
    )
    mail = imaplib.IMAP4_SSL(secrets_cfg["host"], secrets_cfg.get("port", 993))
    mail.login(secrets_cfg["username"], secrets_cfg["password"])
    return mail


def list_all_folders(client: imaplib.IMAP4) -> List[str]:
    typ, data = client.list()
    if typ != "OK":
        raise RuntimeError("Unable to list folders")
    folders: List[str] = []
    for line in data or []:
        if not line:
            continue
        parts = line.decode().split(' "/" ')
        if len(parts) == 2:
            folders.append(parts[1].strip('"'))
    return folders


def safe_search_all(client: imaplib.IMAP4) -> Iterable[bytes]:
    typ, data = client.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data:
        return []

    results: list[bytes] = []
    for chunk in data:
        if not chunk or not isinstance(chunk, (bytes, bytearray)):
            continue
        results.extend(bytes(chunk).split())
    return results
