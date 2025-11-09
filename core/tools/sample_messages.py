"""Script to copy random INBOX messages into the ``Test`` folder."""
from __future__ import annotations

import imaplib
import random
from typing import Iterable, List

from tqdm import tqdm

from core.config import build_default_config
from core.imap_client import imap_login, safe_search_all
from core.logging_utils import JsonLogger

TARGET_MAILBOX = "Test"
TARGET_QUOTED = f'"{TARGET_MAILBOX}"'
INBOX_QUOTED = '"INBOX"'


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


def _prompt_for_count(max_messages: int) -> int:
    while True:
        raw = input(
            f"How many messages would you like to copy to '{TARGET_MAILBOX}'? (1-{max_messages}): "
        ).strip()
        if not raw:
            print("Please enter a whole number.")
            continue
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a valid whole number.")
            continue
        if value < 1:
            print("Please enter a positive number of messages to copy.")
            continue
        if value > max_messages:
            print(
                f"Only {max_messages} messages are available; copying {max_messages} instead."
            )
            return max_messages
        return value


def main() -> int:
    cfg = build_default_config()
    logger = JsonLogger(cfg.paths.log_file)
    client: imaplib.IMAP4_SSL | None = None

    try:
        client = imap_login(cfg.paths.secrets_file, logger)
        logger.log(
            "INFO",
            "sample_select_inbox",
            {"folder": "INBOX"},
            console="📂 Selecting INBOX",
        )
        typ, resp = client.select(INBOX_QUOTED)
        if typ != "OK":
            details = _format_imap_details(resp)
            logger.log(
                "ERROR",
                "sample_select_failed",
                {"folder": "INBOX", "status": typ, "details": details},
                console=f"❌ Unable to open INBOX{details}",
            )
            return 1

        available = [uid.decode("utf-8", "ignore") for uid in safe_search_all(client)]
        total_available = len(available)
        if total_available == 0:
            logger.log(
                "WARN",
                "sample_no_messages",
                {},
                console="⚠️ INBOX contains no messages to copy.",
            )
            return 0

        logger.log(
            "INFO",
            "sample_available",
            {"available": total_available},
            console=f"ℹ️ Found {total_available} messages in INBOX.",
        )

        count = _prompt_for_count(total_available)
        selected_uids = random.sample(available, k=count)
        logger.log(
            "INFO",
            "sample_begin",
            {"requested": count, "target": TARGET_MAILBOX},
            console=f"🚚 Copying {count} message(s) to {TARGET_MAILBOX}...",
        )

        failures: list[dict[str, str]] = []
        copied = 0
        target_ready = False

        with tqdm(
            total=count,
            desc=f"Copying to {TARGET_MAILBOX}",
            unit="msg",
            dynamic_ncols=True,
        ) as bar:
            for uid in selected_uids:
                try:
                    typ1, copy_resp = client.uid("COPY", uid, TARGET_QUOTED)
                    if (
                        typ1 != "OK"
                        and not target_ready
                        and _should_try_create_folder(copy_resp)
                    ):
                        logger.log(
                            "INFO",
                            "sample_create_mailbox",
                            {"target": TARGET_MAILBOX},
                            console=f"📁 Creating missing folder {TARGET_MAILBOX}",
                        )
                        create_typ, create_resp = client.create(TARGET_QUOTED)
                        if create_typ != "OK":
                            details = _format_imap_details(create_resp)
                            logger.log(
                                "ERROR",
                                "sample_create_failed",
                                {
                                    "target": TARGET_MAILBOX,
                                    "status": create_typ,
                                    "details": details,
                                },
                                console=f"❌ Unable to create {TARGET_MAILBOX}{details}",
                            )
                            failures.append(
                                {
                                    "uid": uid,
                                    "error": f"CREATE failed{details}",
                                }
                            )
                            bar.update(1)
                            continue
                        target_ready = True
                        typ1, copy_resp = client.uid("COPY", uid, TARGET_QUOTED)

                    if typ1 != "OK":
                        details = _format_imap_details(copy_resp)
                        logger.log(
                            "ERROR",
                            "sample_copy_failed",
                            {"uid": uid, "status": typ1, "details": details},
                            console=f"❌ Failed to copy UID {uid}{details}",
                        )
                        failures.append(
                            {"uid": uid, "error": f"UID COPY failed{details}"}
                        )
                    else:
                        target_ready = True
                        copied += 1
                except imaplib.IMAP4.error as exc:
                    logger.log(
                        "ERROR",
                        "sample_copy_exception",
                        {"uid": uid, "error": str(exc)},
                        console=f"❌ Exception while copying UID {uid}: {exc}",
                    )
                    failures.append({"uid": uid, "error": str(exc)})
                finally:
                    bar.update(1)

        logger.log(
            "INFO",
            "sample_complete",
            {
                "copied": copied,
                "failed": len(failures),
                "target": TARGET_MAILBOX,
            },
            console=f"✅ Copied {copied} message(s); {len(failures)} failure(s).",
        )

        if failures:
            for item in failures:
                logger.log(
                    "ERROR",
                    "sample_failure_detail",
                    item,
                    console=f"   ↳ UID {item['uid']}: {item['error']}",
                )
            return 1

        return 0

    finally:
        if client is not None:
            try:
                client.close()
            except imaplib.IMAP4.error:
                pass
            client.logout()


if __name__ == "__main__":
    raise SystemExit(main())
