"""Diagnostic script to exercise the IMAP move flows."""
from __future__ import annotations

import argparse
import imaplib
import textwrap
import uuid
from dataclasses import dataclass
from email.utils import formatdate
from typing import Iterable, List, Sequence

from core.config import build_default_config
from core.imap_client import imap_login
from core.logging_utils import JsonLogger

INBOX = "INBOX"
INBOX_QUOTED = '"INBOX"'


class MoveError(RuntimeError):
    """Raised when a move operation fails."""


@dataclass
class MoveResult:
    method: str
    message_id: str
    subject: str
    success: bool
    details: str
    source_present: bool
    destination_present: bool


def _quote_mailbox(mailbox: str) -> str:
    mailbox = mailbox.strip()
    if mailbox.startswith('"') and mailbox.endswith('"'):
        return mailbox
    return f'"{mailbox}"'


def _imap_response_text(response: Iterable[bytes | str] | None) -> str:
    if not response:
        return ""
    parts: List[str] = []
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


def _ensure_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, logger: JsonLogger) -> None:
    quoted = _quote_mailbox(mailbox)
    typ, resp = client.select(quoted, readonly=True)
    if typ == "OK":
        logger.log(
            "INFO",
            "diag_mailbox_exists",
            {"mailbox": mailbox},
            console=f"📁 Mailbox '{mailbox}' is available.",
        )
        try:
            client.close()
        except imaplib.IMAP4.error:
            pass
        return

    details = _format_imap_details(resp)
    logger.log(
        "INFO",
        "diag_mailbox_missing",
        {"mailbox": mailbox, "status": typ, "details": details},
        console=f"📁 Mailbox '{mailbox}' missing{details}; attempting to create.",
    )
    create_typ, create_resp = client.create(quoted)
    if create_typ != "OK":
        create_details = _format_imap_details(create_resp)
        raise MoveError(
            f"Unable to create mailbox '{mailbox}'{create_details}"
        )
    logger.log(
        "INFO",
        "diag_mailbox_created",
        {"mailbox": mailbox},
        console=f"✅ Created mailbox '{mailbox}'.",
    )


@dataclass
class AppendedMessage:
    subject: str
    message_id: str
    uid: str


def _append_message(
    client: imaplib.IMAP4_SSL,
    method: str,
    logger: JsonLogger,
    *,
    recipient: str,
) -> AppendedMessage:
    unique = uuid.uuid4()
    message_id = f"<move-diagnostics-{unique}@example.com>"
    subject = f"Move diagnostics: {method}"
    body = textwrap.dedent(
        f"""
        From: Move Diagnostics <move-diagnostics@example.com>
        To: {recipient}
        Subject: {subject}
        Message-ID: {message_id}
        Date: {formatdate(localtime=True)}
        Content-Type: text/plain; charset=utf-8

        This is an automatically generated message to test the '{method}' move strategy.
        It was created by the move diagnostics helper.
        """
    ).strip()

    payload = (body.replace("\n", "\r\n") + "\r\n").encode("utf-8")

    logger.log(
        "INFO",
        "diag_append_message",
        {"method": method, "message_id": message_id},
        console=f"✉️ Appending test message for '{method}'.",
    )
    append_typ, append_resp = client.append(INBOX_QUOTED, None, None, payload)
    if append_typ != "OK":
        details = _format_imap_details(append_resp)
        raise MoveError(f"APPEND failed{details}")

    uid = _locate_message_uid(client, message_id, logger)
    logger.log(
        "INFO",
        "diag_append_success",
        {"method": method, "message_id": message_id, "uid": uid},
        console=f"✅ Appended message UID {uid}.",
    )
    return AppendedMessage(subject=subject, message_id=message_id, uid=uid)


def _locate_message_uid(
    client: imaplib.IMAP4_SSL,
    message_id: str,
    logger: JsonLogger,
) -> str:
    logger.log(
        "INFO",
        "diag_search_inbox",
        {"message_id": message_id},
        console="🔍 Locating appended message in INBOX...",
    )
    typ, resp = client.select(INBOX_QUOTED)
    if typ != "OK":
        details = _format_imap_details(resp)
        raise MoveError(f"Unable to select INBOX{details}")
    try:
        search_typ, search_resp = client.uid(
            "SEARCH", None, f'(HEADER Message-ID "{message_id}")'
        )
    finally:
        try:
            client.close()
        except imaplib.IMAP4.error:
            pass
    if search_typ != "OK":
        details = _format_imap_details(search_resp)
        raise MoveError(f"SEARCH failed{details}")
    if not search_resp or not search_resp[0]:
        raise MoveError("Message not found in INBOX after append")
    uids = search_resp[0].decode("utf-8", "ignore").split()
    if not uids:
        raise MoveError("No UID returned for appended message")
    return uids[-1]


def _message_exists(
    client: imaplib.IMAP4_SSL,
    mailbox: str,
    message_id: str,
) -> bool:
    quoted = _quote_mailbox(mailbox)
    typ, resp = client.select(quoted, readonly=True)
    if typ != "OK":
        return False
    try:
        search_typ, search_resp = client.uid(
            "SEARCH", None, f'(HEADER Message-ID "{message_id}")'
        )
    finally:
        try:
            client.close()
        except imaplib.IMAP4.error:
            pass
    if search_typ != "OK" or not search_resp:
        return False
    return bool(search_resp[0])


def _run_uid_move(
    client: imaplib.IMAP4_SSL,
    uid: str,
    destination: str,
    logger: JsonLogger,
) -> tuple[bool, str]:
    quoted_dest = _quote_mailbox(destination)
    logger.log(
        "INFO",
        "diag_uid_move_attempt",
        {"uid": uid, "destination": destination},
        console=f"🚚 Attempting UID MOVE to '{destination}'.",
    )
    typ, resp = client.select(INBOX_QUOTED)
    if typ != "OK":
        details = _format_imap_details(resp)
        return False, f"Unable to select INBOX{details}"
    try:
        move_typ, move_resp = client.uid("MOVE", uid, quoted_dest)
    finally:
        try:
            client.close()
        except imaplib.IMAP4.error:
            pass
    if move_typ == "OK":
        details = _format_imap_details(move_resp)
        return True, f"UID MOVE succeeded{details}"

    details = _format_imap_details(move_resp)
    if _should_try_create_folder(move_resp):
        logger.log(
            "INFO",
            "diag_uid_move_create",
            {"destination": destination},
            console=f"📁 Destination missing{details}; attempting to create and retry.",
        )
        try:
            _ensure_mailbox(client, destination, logger)
        except MoveError as exc:  # pragma: no cover - defensive logging
            return False, str(exc)
        typ, resp = client.select(INBOX_QUOTED)
        if typ != "OK":
            details = _format_imap_details(resp)
            return False, f"Unable to re-select INBOX{details}"
        try:
            move_typ, move_resp = client.uid("MOVE", uid, quoted_dest)
        finally:
            try:
                client.close()
            except imaplib.IMAP4.error:
                pass
        if move_typ == "OK":
            details = _format_imap_details(move_resp)
            return True, f"UID MOVE succeeded after creating mailbox{details}"
        details = _format_imap_details(move_resp)
    return False, f"UID MOVE failed{details}"


def _run_copy_delete(
    client: imaplib.IMAP4_SSL,
    uid: str,
    destination: str,
    logger: JsonLogger,
) -> tuple[bool, str]:
    quoted_dest = _quote_mailbox(destination)
    logger.log(
        "INFO",
        "diag_copy_attempt",
        {"uid": uid, "destination": destination},
        console=f"📬 Attempting UID COPY then delete to '{destination}'.",
    )
    typ, resp = client.select(INBOX_QUOTED)
    if typ != "OK":
        details = _format_imap_details(resp)
        return False, f"Unable to select INBOX{details}"
    try:
        copy_typ, copy_resp = client.uid("COPY", uid, quoted_dest)
        if copy_typ != "OK" and _should_try_create_folder(copy_resp):
            logger.log(
                "INFO",
                "diag_copy_create",
                {"destination": destination},
                console="📁 Destination missing; creating and retrying COPY.",
            )
            try:
                _ensure_mailbox(client, destination, logger)
            except MoveError as exc:
                return False, str(exc)
            typ, resp = client.select(INBOX_QUOTED)
            if typ != "OK":
                details = _format_imap_details(resp)
                return False, f"Unable to re-select INBOX{details}"
            copy_typ, copy_resp = client.uid("COPY", uid, quoted_dest)
        if copy_typ != "OK":
            details = _format_imap_details(copy_resp)
            return False, f"UID COPY failed{details}"
        store_typ, store_resp = client.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
        if store_typ != "OK":
            details = _format_imap_details(store_resp)
            return False, f"UID STORE failed{details}"
        expunge_typ, expunge_resp = client.expunge()
    finally:
        try:
            client.close()
        except imaplib.IMAP4.error:
            pass
    if expunge_typ != "OK":
        details = _format_imap_details(expunge_resp)
        return False, f"EXPUNGE failed{details}"
    details = _format_imap_details(expunge_resp)
    return True, f"COPY+DELETE succeeded{details}"


def _run_method(
    client: imaplib.IMAP4_SSL,
    method: str,
    destination: str,
    message: AppendedMessage,
    logger: JsonLogger,
) -> MoveResult:
    if method == "uid-move":
        success, details = _run_uid_move(client, message.uid, destination, logger)
    elif method == "copy-delete":
        success, details = _run_copy_delete(client, message.uid, destination, logger)
    else:  # pragma: no cover - defensive programming
        raise MoveError(f"Unknown method '{method}'")

    source_present = _message_exists(client, INBOX, message.message_id)
    destination_present = _message_exists(client, destination, message.message_id)

    logger.log(
        "INFO",
        "diag_verification",
        {
            "method": method,
            "message_id": message.message_id,
            "source_present": source_present,
            "destination_present": destination_present,
            "details": details,
        },
        console=(
            "✅" if success and destination_present else "❌"
        )
        + f" {method} -> {destination}: {details}"
        + (
            " (present in destination)"
            if destination_present
            else " (missing from destination)"
        ),
    )

    return MoveResult(
        method=method,
        message_id=message.message_id,
        subject=message.subject,
        success=success,
        details=details,
        source_present=source_present,
        destination_present=destination_present,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise IMAP move operations against test messages.",
    )
    parser.add_argument(
        "--destination",
        default="Test",
        help="Mailbox to move messages into (default: %(default)s).",
    )
    parser.add_argument(
        "--recipient",
        default="sj.gibson@mac.com",
        help="Recipient address to include in the appended message headers.",
    )
    parser.add_argument(
        "--methods",
        nargs="+",
        default=["uid-move", "copy-delete"],
        choices=["uid-move", "copy-delete"],
        help="Move strategies to test.",
    )
    parser.add_argument(
        "--ensure-destination",
        action="store_true",
        help="Create the destination mailbox when missing before running tests.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = build_default_config()
    logger = JsonLogger(cfg.paths.log_file)
    client: imaplib.IMAP4_SSL | None = None

    try:
        client = imap_login(cfg.paths.secrets_file, logger)
        logger.log(
            "INFO",
            "diag_connected",
            {"server": getattr(client, "host", "(unknown)")},
            console="🔐 Logged in to IMAP server.",
        )

        if args.ensure_destination:
            _ensure_mailbox(client, args.destination, logger)

        results: list[MoveResult] = []
        for method in args.methods:
            message = _append_message(
                client,
                method,
                logger,
                recipient=args.recipient,
            )
            result = _run_method(client, method, args.destination, message, logger)
            results.append(result)

        failures = [result for result in results if not (result.success and result.destination_present)]
        if failures:
            logger.log(
                "ERROR",
                "diag_summary_failure",
                {
                    "failures": [
                        {
                            "method": failure.method,
                            "details": failure.details,
                            "destination_present": failure.destination_present,
                        }
                        for failure in failures
                    ]
                },
                console="❌ One or more move strategies failed. See log for details.",
            )
            return 1

        logger.log(
            "INFO",
            "diag_summary_success",
            {"methods": args.methods, "destination": args.destination},
            console="🎉 All move strategies succeeded.",
        )
        return 0
    except MoveError as exc:
        logger.log(
            "ERROR",
            "diag_exception",
            {"error": str(exc)},
            console=f"❌ {exc}",
        )
        return 2
    finally:
        if client is not None:
            try:
                client.logout()
            except imaplib.IMAP4.error:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
