"""Low-level IMAP/string helpers shared across the executor package."""
from __future__ import annotations

import base64
import imaplib
import sqlite3
from typing import Iterable


def _encode_mailbox_utf7(mailbox: str) -> str:
    """Encode a mailbox name to IMAP modified UTF-7 (mUTF-7, RFC 3501).

    The only ASCII character that must be encoded is '&', which becomes '&-'.
    Non-ASCII characters are encoded as &<modified-base64>-.
    """
    result = []
    i = 0
    while i < len(mailbox):
        ch = mailbox[i]
        if ch == '&':
            result.append('&-')
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            # Collect run of non-printable-ASCII characters
            run = []
            while i < len(mailbox) and (ord(mailbox[i]) < 0x20 or ord(mailbox[i]) > 0x7e):
                run.append(mailbox[i])
                i += 1
            encoded = ''.join(run).encode('utf-16-be')
            b64 = base64.b64encode(encoded).decode('ascii').rstrip('=').replace('/', ',')
            result.append(f'&{b64}-')
            continue
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _quote_mailbox(mailbox: str) -> str:
    """Quote and mUTF-7-encode a mailbox name for use in IMAP commands."""
    if not mailbox:
        return '""'
    encoded = _encode_mailbox_utf7(mailbox)
    escaped = encoded.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _imap_response_text(response: Iterable[bytes | str] | None) -> str:
    if not response:
        return ""
    parts: list[str] = []
    for item in response:
        if not item:
            continue
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", "ignore"))
        else:
            parts.append(str(item))
    return " ".join(part for part in parts if part).strip()


def _format_imap_details(response: Iterable[bytes | str] | None) -> str:
    text = _imap_response_text(response)
    return f": {text}" if text else ""


def _should_try_create_folder(response: Iterable[bytes | str] | None) -> bool:
    text = _imap_response_text(response).lower()
    if not text:
        return False
    keywords = (
        "trycreate",
        "no such mailbox",
        "does not exist",
        "not found",
        "nonexistent",
    )
    return any(keyword in text for keyword in keywords)


def _is_invalid_mailbox_name_error(exc_str: str) -> bool:
    """Return True if the error indicates the target mailbox name is permanently invalid."""
    s = exc_str.lower()
    return "character not allowed" in s or ("cannot" in s and "mailbox name" in s)


def _is_connection_dead(exc: Exception) -> bool:
    """Return True if the exception indicates the IMAP TCP/SSL socket is gone."""
    if isinstance(exc, (imaplib.IMAP4.abort, EOFError)):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("eof", "connection reset", "broken pipe", "connection aborted")
    )


def _uidvalidity_mismatch(
    db: sqlite3.Connection,
    client: imaplib.IMAP4,
    folder: str,
) -> tuple[str, str] | None:
    """Compare the live UIDVALIDITY of the just-SELECTed folder with the cached one.

    Cached UIDs are only meaningful while the folder's UIDVALIDITY is unchanged
    (RFC 3501); after a reset the same UID can address a different message, so
    executing stale actions could move — and expunge — the wrong mail.

    Must be called immediately after a successful SELECT of ``folder`` (imaplib
    pops the untagged UIDVALIDITY response on read). Returns (cached, live) on
    mismatch, or None when the values match or either side is unavailable
    (pre-guard caches have no snapshot; some test doubles report none).
    """
    from core.imap_client import get_selected_uidvalidity

    live = get_selected_uidvalidity(client)
    if not live:
        return None
    try:
        row = db.execute(
            "SELECT uidvalidity FROM folder_uidvalidity WHERE folder=?",
            (folder,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row[0]:
        return None
    cached = str(row[0])
    if cached != live:
        return cached, live
    return None


