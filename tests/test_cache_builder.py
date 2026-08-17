from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "tqdm" not in sys.modules:  # pragma: no cover - test support
    tqdm_stub = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **_kwargs):
            self._iterable = list(iterable or [])

        def __iter__(self):
            return iter(self._iterable)

        def set_postfix_str(self, *_args, **_kwargs):
            return None

        def update(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    def _write(*_args, **_kwargs):
        return None

    def _tqdm(iterable=None, **kwargs):
        return _DummyTqdm(iterable, **kwargs)

    _tqdm.write = _write  # type: ignore[attr-defined]

    tqdm_stub.tqdm = _tqdm
    tqdm_stub.write = _write
    sys.modules["tqdm"] = tqdm_stub

from core.cache_builder import _parse_batch_fetch_response, build_cache, compact_cache
from core.config import build_default_config
from core.database import init_db
from core.logging_utils import JsonLogger


def test_parse_batch_fetch_response_reads_trailing_flags_item():
    # Real iCloud FETCH response shape for "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)":
    # FLAGS/INTERNALDATE arrive as a separate bytes item *after* each
    # message's (metadata, header) tuple, not inside the tuple itself.
    msg_data = [
        (b'1 (UID 100 BODY[HEADER] {5}', b'hdr1\n'),
        b' FLAGS ($NotJunk NotJunk) INTERNALDATE "26-Apr-2026 21:52:19 +0000")',
        (b'2 (UID 200 BODY[HEADER] {5}', b'hdr2\n'),
        b' FLAGS (\\Seen) INTERNALDATE "12-May-2026 09:09:33 +0000")',
        (b'3 (UID 300 BODY[HEADER] {5}', b'hdr3\n'),
        b' FLAGS () INTERNALDATE "19-May-2026 09:01:37 +0000")',
    ]

    results = _parse_batch_fetch_response(msg_data)

    assert results["100"] == (b"hdr1\n", ["$NotJunk", "NotJunk"], "26-Apr-2026 21:52:19 +0000")
    assert results["200"] == (b"hdr2\n", ["\\Seen"], "12-May-2026 09:09:33 +0000")
    assert results["300"] == (b"hdr3\n", [], "19-May-2026 09:01:37 +0000")


def test_parse_batch_fetch_response_reads_inline_flags_item():
    # Some servers put FLAGS/INTERNALDATE inside the tuple's metadata itself -
    # must keep working for that shape too.
    msg_data = [
        (b'1 (UID 100 FLAGS (\\Seen custom) INTERNALDATE "28-Oct-2025 07:30:19 +0000" BODY[HEADER] {5}', b'hdr1\n'),
    ]

    results = _parse_batch_fetch_response(msg_data)

    assert results["100"] == (b"hdr1\n", ["\\Seen", "custom"], "28-Oct-2025 07:30:19 +0000")


BATCH_FETCH_QUERY = "(BODY.PEEK[HEADER] FLAGS INTERNALDATE)"


def _batch_fetch_response(headers_by_uid: dict[str, bytes]):
    """Build an imaplib-style multi-UID FETCH response."""
    response: list = []
    for seq, (uid, header) in enumerate(headers_by_uid.items(), 1):
        envelope = f"{seq} (UID {uid} BODY[HEADER] {{{len(header)}}}".encode()
        response.append((envelope, header))
        response.append(b")")
    return "OK", response


class _FakeClient:
    """Serves the same header for every UID in a batched FETCH."""

    def __init__(self, header_bytes: bytes = b"Subject: Test\n\n"):
        self._header_bytes = header_bytes
        self.uid_calls: list[tuple[str, object, str]] = []

    def select(self, mailbox: str, readonly: bool = True):
        assert readonly is True
        return "OK", None

    def uid(self, command: str, uid, query: str):
        self.uid_calls.append((command, uid, query))
        if command != "FETCH":
            raise AssertionError(f"Unexpected UID command {command}")
        if query == BATCH_FETCH_QUERY:
            uid_set = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            return _batch_fetch_response({u: self._header_bytes for u in uid_set.split(",")})
        raise AssertionError(f"Unexpected query {query}")


class _UIDAwareClient:
    def __init__(self, messages: dict[str, tuple[bytes, bytes]]):
        self._messages = messages
        self.search_calls: list[tuple[str, object, tuple]] = []

    def select(self, mailbox: str, readonly: bool = True):
        assert readonly is True
        return "OK", None

    def uid(self, command: str, uid, *args):
        if command == "SEARCH":
            criterion = args[0]
            if criterion in ("ALL", "UNDELETED"):
                ordered = sorted(self._messages.keys(), key=int)
                return "OK", [" ".join(ordered).encode()]
            match = re.search(r'"([^\"]+)"', criterion)
            if not match:
                return "OK", [b""]
            message_id = match.group(1)
            for key, (header, _body) in self._messages.items():
                if message_id.encode() in header:
                    return "OK", [key.encode()]
            return "OK", [b""]

        if command == "FETCH":
            query = args[0]
            if query != BATCH_FETCH_QUERY:
                raise AssertionError(f"Unexpected fetch query {query}")
            uid_set = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            return _batch_fetch_response(
                {key: self._messages[key][0] for key in uid_set.split(",")}
            )
        raise AssertionError(f"Unexpected UID command {command}")


@pytest.fixture()
def cache_context(tmp_path: Path):
    cfg = build_default_config(tmp_path)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.db_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = JsonLogger(cfg.paths.log_file)
    db = init_db(cfg.paths.db_file, logger=logger)
    try:
        yield cfg, db, logger
    finally:
        db.close()


def test_build_cache_stores_headers(monkeypatch, cache_context):
    cfg, db, logger = cache_context
    client = _FakeClient()

    monkeypatch.setattr(
        "core.cache_builder.safe_search_all", lambda _client, **_kwargs: [b"1", b"2"]
    )

    timer, folders, messages = build_cache(
        client,
        db,
        ["INBOX"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
    )

    assert folders == 1
    assert messages == 2
    assert timer.count == 2
    assert all(
        call[0] == "FETCH" and call[2] == BATCH_FETCH_QUERY for call in client.uid_calls
    )

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM headers WHERE folder='INBOX'")
    (count,) = cur.fetchone()
    assert count == 2


def test_build_cache_stores_matching_uids(cache_context):
    cfg, db, logger = cache_context
    messages = {
        "101": (
            b"Message-ID: <uid-101@example.com>\r\nSubject: UID Test\r\n\r\n",
            b"Subject: UID Test\r\n\r\nBody 101",
        ),
        "202": (
            b"Message-ID: <uid-202@example.com>\r\nSubject: UID Test\r\n\r\n",
            b"Subject: UID Test\r\n\r\nBody 202",
        ),
    }
    client = _UIDAwareClient(messages)

    timer, folders, messages_cached = build_cache(
        client,
        db,
        ["INBOX"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
    )

    assert folders == 1
    assert messages_cached == 2
    assert timer.count == 2

    rows = db.execute(
        "SELECT uid, data FROM headers WHERE folder='INBOX' ORDER BY uid"
    ).fetchall()
    assert [uid for uid, _ in rows] == ["101", "202"]

    for uid_value, payload in rows:
        stored = json.loads(payload)
        header_text = stored["header"]
        match = re.search(r"Message-ID:\s*(<[^>]+>)", header_text)
        assert match is not None
        message_id = match.group(1)
        search_typ, search_resp = client.uid(
            "SEARCH",
            None,
            f'(HEADER Message-ID "{message_id}")',
        )
        assert search_typ == "OK"
        found = False
        for chunk in search_resp:
            if isinstance(chunk, (bytes, bytearray)):
                if uid_value.encode() in bytes(chunk).split():
                    found = True
                    break
        assert found, f"UID {uid_value} not found in search response {search_resp!r}"


class _DeadClient:
    """Every call raises a dropped-connection error, like a stale IMAP socket."""

    def select(self, mailbox: str, readonly: bool = True):
        raise OSError("socket error: EOF occurred in violation of protocol (_ssl.c:2406)")


def test_build_cache_reconnects_instead_of_cascading(monkeypatch, cache_context):
    """A dead connection at the start of the folder list must not doom every
    later folder — build_cache should reconnect once and keep caching."""
    cfg, db, logger = cache_context

    monkeypatch.setattr(
        "core.cache_builder.safe_search_all", lambda _client, **_kwargs: [b"1", b"2"]
    )

    fresh_client = _FakeClient()
    reconnect_calls = []

    def reconnect_fn():
        reconnect_calls.append(True)
        return fresh_client

    timer, folders, messages = build_cache(
        _DeadClient(),
        db,
        ["INBOX", "Archive"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
        reconnect_fn=reconnect_fn,
    )

    # Reconnect happens once (on the first folder); the fresh client then
    # serves the rest of the list, so both folders end up cached rather than
    # cascading into instant failures.
    assert len(reconnect_calls) == 1
    assert folders == 2
    assert messages == 4
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM headers")
    (count,) = cur.fetchone()
    assert count == 4


def test_build_cache_folder_fails_when_reconnect_unavailable(cache_context):
    """Without a reconnect_fn, a dead connection still just fails the folder
    (previous behavior) instead of raising out of build_cache entirely."""
    cfg, db, logger = cache_context

    timer, folders, messages = build_cache(
        _DeadClient(),
        db,
        ["INBOX"],
        show_progress=False,
        logger=logger,
        limit=None,
        order="newest",
    )

    assert folders == 1
    assert messages == 0


def test_compact_cache_removes_handled_headers(cache_context):
    _cfg, db, logger = cache_context

    with db:
        db.executemany(
            "INSERT INTO headers (folder, uid, data, updated_at) VALUES (?,?,?,?)",
            [
                ("INBOX", "1", json.dumps({"header": "Subject: One\n\n"}), "2024-01-01T00:00:00Z"),
                ("INBOX", "2", json.dumps({"header": "Subject: Two\n\n"}), "2024-01-02T00:00:00Z"),
                ("INBOX", "3", json.dumps({"header": "Subject: Three\n\n"}), "2024-01-03T00:00:00Z"),
            ],
        )
        db.executemany(
            "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at, executed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                ("1", "INBOX", "rule", "Archive", 100, "done", "2024-02-01T00:00:00Z", "2024-02-02T00:00:00Z"),
                ("2", "INBOX", "rule", "Archive", 100, "pending", "2024-02-01T00:00:00Z", None),
                ("3", "INBOX", "rule", "Archive", 100, "simulated", "2024-02-01T00:00:00Z", None),
            ],
        )

    timer, removed, checked = compact_cache(db, logger=logger)

    assert timer.count == removed == 1
    assert checked == 1
    remaining = db.execute("SELECT uid FROM headers ORDER BY uid").fetchall()
    assert [uid for (uid,) in remaining] == ["2", "3"]
