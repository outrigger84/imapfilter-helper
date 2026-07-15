"""Single-connection action execution (execute_actions)."""
from __future__ import annotations

import imaplib
import json
import re
import time
from email.parser import HeaderParser
from email.policy import default
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Sequence

from tqdm import tqdm

from core.backup import backup_messages, backup_all_cached_messages, BackupResult
from core.logging_utils import JsonLogger, PhaseTimer, now_iso

HEADER_PARSER = HeaderParser(policy=default)

from core.executor.helpers import (
    _encode_mailbox_utf7,
    _format_imap_details,
    _imap_response_text,
    _is_connection_dead,
    _is_invalid_mailbox_name_error,
    _should_try_create_folder,
    _uidvalidity_mismatch,
)

from core.executor.verification import _verify_move

from core.executor.conflicts import resolve_pending_conflicts


def execute_actions(
    client: imaplib.IMAP4 | None,
    db,
    *,
    reconnect_fn: Callable[[], imaplib.IMAP4] | None = None,
    show_progress: bool,
    dry_run: bool,
    strict: bool,
    logger: JsonLogger,
    verbose: bool = False,
    limit: int | None = None,
    folders: Sequence[str] | None = None,
    verify_moves: bool = False,
    backup_moved: bool = False,
    backup_all: bool = False,
    backup_dir: Path | None = None,
    disabled_action_types: set[str] | None = None,
    folder_order: str = "alpha",
) -> tuple[PhaseTimer, Dict[str, int]]:
    if not dry_run and client is None:
        raise ValueError("An IMAP client is required when not running in dry-run mode")

    timer = PhaseTimer("execute")

    # Validate backup parameters
    if (backup_moved or backup_all) and backup_dir is None:
        raise ValueError("backup_dir must be specified when backup is enabled")

    if backup_moved and backup_all:
        logger.log(
            "WARN",
            "backup_both_enabled",
            console="⚠️  Both --backup-moved and --backup-all specified. Using --backup-all.",
        )
        backup_moved = False  # backup_all takes precedence

    logger.log(
        "INFO",
        "execute_log_hint",
        {"log_file": str(logger.log_file)},
        console=f"📝 Detailed logs: {logger.log_file}",
    )

    folder_params = tuple(folders) if folders else ()
    folder_filter = ""
    if folder_params:
        placeholders = ",".join("?" for _ in folder_params)
        folder_filter = f" AND folder IN ({placeholders})"

    disabled_types = disabled_action_types or set()
    action_type_params: tuple[str, ...] = ()
    action_type_filter = ""
    if disabled_types:
        placeholders = ",".join("?" for _ in disabled_types)
        action_type_filter = f" AND action_type NOT IN ({placeholders})"
        action_type_params = tuple(disabled_types)
        logger.log(
            "INFO",
            "execute_action_types_disabled",
            {"disabled": sorted(disabled_types)},
            console=f"⚙️  Skipping action types: {', '.join(sorted(disabled_types))}",
        )

    # Resolve any conflicts left over from earlier all-match evaluation runs.
    resolve_pending_conflicts(db, logger)

    where_params = folder_params + action_type_params
    pending_cur = db.cursor()
    pending_cur.execute(
        "SELECT COUNT(*) FROM actions WHERE status='pending'" + folder_filter + action_type_filter,
        where_params,
    )
    pending_total = pending_cur.fetchone()[0] or 0
    if pending_total == 0:
        logger.log("INFO", "execute_nothing", {"dry_run": dry_run}, console="ℹ️ No pending actions")
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    suppressed = 0  # calculated after CTE is built below

    # Order by effective priority first (ensures keywords execute before moves across all rules)
    # Then by folder/target for batch processing, then by creation order
    if folder_order == "most-first":
        order_clause = "ORDER BY priority DESC, folder_action_count DESC, folder, target, created_at ASC, id ASC"
    elif folder_order == "least-first":
        order_clause = "ORDER BY priority DESC, folder_action_count ASC, folder, target, created_at ASC, id ASC"
    else:
        order_clause = "ORDER BY priority DESC, folder, target, created_at ASC, id ASC"

    # Deduplication: Keep only one occurrence of each unique action per UID
    # PARTITION BY (uid, folder, rule_name, action_type, target, action_data) allows:
    # - All actions from same rule to execute
    # - Different rules' actions to execute
    # - Only prevents identical duplicate actions in the same folder
    # - Includes folder to handle cases where emails are moved and re-matched
    folder_count_col = ""
    if folder_order in ("most-first", "least-first"):
        folder_count_col = "\n                COUNT(*) OVER (PARTITION BY target) AS folder_action_count,"
    dedup_cte = f"""
        WITH ranked AS (
            SELECT
                id,
                uid,
                folder,
                target,
                rule_name,
                priority,
                created_at,
                action_type,
                action_data,{folder_count_col}
                ROW_NUMBER() OVER (
                    PARTITION BY uid, folder, rule_name, action_type, target, action_data
                    ORDER BY created_at ASC, id ASC
                ) AS rn
            FROM actions
            WHERE status='pending'
    )
"""
    combined_filter = folder_filter + action_type_filter
    if combined_filter:
        dedup_cte = dedup_cte.replace(
            "WHERE status='pending'",
            f"WHERE status='pending'{combined_filter}",
        )
    selection_source = "ranked WHERE rn=1"
    limit_param: tuple[int, ...] = ()
    if limit is not None:
        extra_col = ", folder_action_count" if folder_order in ("most-first", "least-first") else ""
        dedup_cte += (
            "    , limited AS (\n"
            f"        SELECT id, uid, folder, target, rule_name, priority, created_at, action_type, action_data{extra_col}\n"
            "        FROM ranked\n"
            "        WHERE rn=1\n"
            f"        {order_clause}\n"
            "        LIMIT ?\n"
            "    )\n"
        )
        selection_source = "limited"
        limit_param = (int(limit),)

    dedup_params = where_params + limit_param

    # dedup_params, not where_params: when limit is set the CTE text contains
    # the LIMIT ? placeholder, and sqlite requires a binding for it even
    # though this query only reads from "ranked".
    suppressed_count_cur = db.cursor()
    suppressed_count_cur.execute(
        dedup_cte + "SELECT COUNT(*) FROM ranked WHERE rn>1",
        dedup_params,
    )
    suppressed = suppressed_count_cur.fetchone()[0] or 0

    if suppressed > 0:
        duplicates_cur = db.cursor()
        duplicates_cur.execute(
            dedup_cte
            + "SELECT id, uid, folder, target, rule_name, priority FROM ranked WHERE rn>1 ORDER BY uid",
            dedup_params,
        )
        while True:
            rows = duplicates_cur.fetchmany(256)
            if not rows:
                break
            updates: list[tuple[str, int]] = []
            timestamp = now_iso() if not dry_run else ""
            for a_id, uid, folder, target, rule_name, priority in rows:
                logger.log(
                    "INFO",
                    "duplicate_action_suppressed",
                    {"uid": uid, "rule": rule_name, "priority": priority, "target": target},
                )
                if not dry_run:
                    updates.append((timestamp, a_id))
            if updates:
                db.executemany(
                    "UPDATE actions SET status='suppressed', executed_at=? WHERE id=?",
                    updates,
                )
        if not dry_run:
            db.commit()

    group_counts_cur = db.cursor()
    group_counts_cur.execute(
        dedup_cte
        + f"SELECT folder, target, COUNT(*) FROM {selection_source} GROUP BY folder, target ORDER BY folder, target",
        dedup_params,
    )
    group_counts = group_counts_cur.fetchall()
    group_totals = {(folder, target): count for folder, target, count in group_counts}

    if verbose:
        groups_context = {
            f"{folder}→{target or '(no target)'}": count for (folder, target), count in group_totals.items()
        }
        lines = "\n".join(
            f"      • {pair}: {count}" for pair, count in sorted(groups_context.items())
        )
        logger.log(
            "INFO",
            "execute_overview",
            {"groups": groups_context, "dry_run": dry_run, "strict": strict},
            console=("📂 Execution plan:" + (f"\n{lines}" if lines else "")),
        )

    actions_total = sum(group_totals.values())
    actions_bar = tqdm(
        total=actions_total if actions_total > 0 else None,
        desc="⚙️ Executing actions",
        unit="action",
        dynamic_ncols=True,
        leave=True,
        position=0,
        disable=not show_progress,
    )

    folders_bar = tqdm(
        total=len(group_totals) if group_totals else None,
        desc="📦 Executing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=1,
        disable=not show_progress,
    )

    stats = {"done": 0, "skipped": 0, "failed": 0, "suppressed": suppressed}

    if verbose and suppressed:
        logger.log(
            "INFO",
            "execute_suppressed_duplicates",
            {"suppressed": suppressed},
            console=f"🚫 Suppressed {suppressed} duplicate actions",
        )

    message_id_cache: dict[tuple[str, str], str | None] = {}

    def _cached_message_id(folder: str, uid: str) -> str | None:
        key = (folder, uid)
        if key in message_id_cache:
            return message_id_cache[key]
        message_id: str | None = None
        try:
            row = db.execute(
                "SELECT data FROM headers WHERE folder=? AND uid=?",
                (folder, uid),
            ).fetchone()
        except Exception:
            row = None
        if row and row[0]:
            try:
                payload = json.loads(row[0])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                header_text = payload.get("header")
                if isinstance(header_text, str):
                    try:
                        parsed = HEADER_PARSER.parsestr(header_text)
                        if parsed is not None:
                            value = parsed.get("Message-ID") or parsed.get("Message-Id")
                            if isinstance(value, str) and value.strip():
                                message_id = value.strip()
                    except Exception:
                        # Fallback: extract Message-ID with regex for malformed headers
                        match = re.search(
                            r'Message-I[Dd]:\s*<?(\[?[^\]>\s]+\]?)>?',
                            header_text,
                            re.IGNORECASE | re.MULTILINE
                        )
                        if match:
                            message_id = match.group(1).strip()
        message_id_cache[key] = message_id
        return message_id

    def _message_id_criteria(message_id: str) -> str:
        escaped = message_id.replace('"', '\\"')
        return f'(UNDELETED HEADER Message-ID "{escaped}")'

    def _search_has_results(search_resp) -> bool:
        if not search_resp:
            return False
        first = search_resp[0]
        if isinstance(first, bytes):
            text = first.decode("ascii", "ignore")
        else:
            text = str(first)
        return bool(text.strip())

    def _uid_search_mailbox(
        mailbox: str,
        message_id: str,
        *,
        target: str | None,
        uid: str,
    ) -> tuple[bool, str, Iterable | None]:
        assert client is not None
        criteria = _message_id_criteria(message_id)
        search_typ, search_resp = client.uid("SEARCH", None, criteria)
        log_imap_call(
            "imap_uid_search",
            op_label="UID SEARCH",
            status=search_typ,
            response=search_resp,
            folder=mailbox,
            target=target,
            uid=uid,
        )
        if search_typ != "OK":
            return False, search_typ, search_resp
        return _search_has_results(search_resp), search_typ, search_resp

    def _verify_destination_mailbox(
        folder: str,
        target: str,
        message_id: str,
        *,
        uid: str,
    ) -> tuple[bool, str]:
        assert client is not None
        quoted_target = f'"{target}"'
        dest_selected = False
        sel_typ, sel_resp = client.select(quoted_target, readonly=True)
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{target}"',
            status=sel_typ,
            response=sel_resp,
            folder=target,
            target=target,
            uid=uid,
        )
        if sel_typ != "OK":
            rese_typ, rese_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=rese_typ,
                response=rese_resp,
                folder=folder,
                target=target,
                uid=uid,
            )
            if rese_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot re-open folder {folder}")
            return False, sel_typ
        dest_selected = True
        try:
            found, search_status, _ = _uid_search_mailbox(
                target, message_id, target=target, uid=uid
            )
        finally:
            if dest_selected:
                try:
                    client.close()
                except imaplib.IMAP4.error:
                    pass
            rese_typ, rese_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=rese_typ,
                response=rese_resp,
                folder=folder,
                target=target,
                uid=uid,
            )
            if rese_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot re-open folder {folder}")
        if search_status != "OK":
            return False, search_status
        return found, search_status

    select_cur = db.cursor()
    select_cur.execute(
        dedup_cte
        + "SELECT id, uid, folder, target, rule_name, priority, created_at, action_type, action_data "
        f"FROM {selection_source} "
        f"{order_clause}",
        dedup_params,
    )

    chunk_size = 512
    current_key: tuple[str, str | None, str] | None = None
    # Store: (action_id, uid, rule_name, action_type, action_data)
    current_items: list[tuple[int, str, str | None, str, str | None]] = []
    current_rule_name: str | None = None

    def _has_capability(name: str) -> bool:
        if dry_run or client is None:
            return False
        caps = getattr(client, "capabilities", ())
        desired = name.upper()
        for cap in caps:
            if isinstance(cap, bytes):
                cap_text = cap.decode("ascii", "ignore").upper()
            else:
                cap_text = str(cap).upper()
            if cap_text == desired:
                return True
        return False

    supports_uid_move = _has_capability("MOVE")

    def log_imap_call(
        message: str,
        *,
        op_label: str,
        status: str,
        response,
        folder: str,
        target: str | None = None,
        uid: str | None = None,
    ) -> None:
        if not verbose:
            return
        details = _format_imap_details(response)
        context: dict[str, str] = {"folder": folder, "status": status, "details": details}
        if target is not None:
            context["target"] = target
        if uid is not None:
            context["uid"] = uid
        suffix_parts: list[str] = []
        if uid is not None:
            suffix_parts.append(f"UID {uid}")
        if target is not None:
            suffix_parts.append(f"→ {target}")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        logger.log(
            "DEBUG",
            message,
            context,
            console=f"      ↪ {op_label} {status}{details}{suffix}",
        )

    def _execute_batch_set_keywords(
        folder: str,
        uid_keyword_map: dict[str, list[str]],
    ) -> dict[str, tuple[str, Any]]:
        """
        Batch set keywords on multiple messages with different keywords.

        Groups UIDs by keyword set and executes one STORE per keyword group,
        dramatically reducing IMAP operations.

        Args:
            folder: Folder containing messages
            uid_keyword_map: Dict mapping UID -> list of keywords

        Returns:
            Dict mapping UID -> (status, response)
        """
        assert client is not None
        results: dict[str, tuple[str, Any]] = {}

        # Select folder once for all operations
        sel_typ, sel_resp = client.select(f'"{folder}"')
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{folder}"',
            status=sel_typ,
            response=sel_resp,
            folder=folder,
        )
        if sel_typ != "OK":
            raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

        # Group UIDs by keyword set for efficient batching
        keyword_set_to_uids: dict[tuple, list[str]] = {}
        for uid, keywords in uid_keyword_map.items():
            key = tuple(sorted(keywords))
            if key not in keyword_set_to_uids:
                keyword_set_to_uids[key] = []
            keyword_set_to_uids[key].append(uid)

        # Execute one STORE per keyword set, chunked to avoid oversized IMAP commands.
        # Most IMAP servers enforce an ~8 KB command-line limit; a UID is ≤10 digits so
        # 500 UIDs per chunk ≈ 5 KB of UID data, comfortably under the limit.
        _STORE_CHUNK = 500
        for keyword_tuple, uids in keyword_set_to_uids.items():
            keywords = list(keyword_tuple)
            flags_str = " ".join(keywords)
            for i in range(0, len(uids), _STORE_CHUNK):
                chunk = uids[i : i + _STORE_CHUNK]
                uid_str = ",".join(chunk)
                typ, resp = client.uid("STORE", uid_str, "+FLAGS", f"({flags_str})")
                log_imap_call(
                    "imap_uid_store_batch_set",
                    op_label="UID STORE +FLAGS (batch)",
                    status=typ,
                    response=resp,
                    folder=folder,
                )
                for uid in chunk:
                    results[uid] = (typ, resp)

        return results

    def _execute_batch_remove_keywords(
        folder: str,
        uid_keyword_map: dict[str, list[str]],
    ) -> dict[str, tuple[str, Any]]:
        """
        Batch remove keywords from multiple messages with different keywords.

        Groups UIDs by keyword set and executes one STORE per keyword group,
        dramatically reducing IMAP operations.

        Args:
            folder: Folder containing messages
            uid_keyword_map: Dict mapping UID -> list of keywords to remove

        Returns:
            Dict mapping UID -> (status, response)
        """
        assert client is not None
        results: dict[str, tuple[str, Any]] = {}

        # Select folder once for all operations
        sel_typ, sel_resp = client.select(f'"{folder}"')
        log_imap_call(
            "imap_select",
            op_label=f'SELECT "{folder}"',
            status=sel_typ,
            response=sel_resp,
            folder=folder,
        )
        if sel_typ != "OK":
            raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

        # Group UIDs by keyword set for efficient batching
        keyword_set_to_uids: dict[tuple, list[str]] = {}
        for uid, keywords in uid_keyword_map.items():
            key = tuple(sorted(keywords))
            if key not in keyword_set_to_uids:
                keyword_set_to_uids[key] = []
            keyword_set_to_uids[key].append(uid)

        # Execute one STORE per keyword set, chunked to avoid oversized IMAP commands.
        _STORE_CHUNK = 500
        for keyword_tuple, uids in keyword_set_to_uids.items():
            keywords = list(keyword_tuple)
            flags_str = " ".join(keywords)
            for i in range(0, len(uids), _STORE_CHUNK):
                chunk = uids[i : i + _STORE_CHUNK]
                uid_str = ",".join(chunk)
                typ, resp = client.uid("STORE", uid_str, "-FLAGS", f"({flags_str})")
                log_imap_call(
                    "imap_uid_store_batch_remove",
                    op_label="UID STORE -FLAGS (batch)",
                    status=typ,
                    response=resp,
                    folder=folder,
                )
                for uid in chunk:
                    results[uid] = (typ, resp)

        return results

    def _execute_set_keywords(
        folder: str,
        uid: str,
        keywords: list[str],
    ) -> tuple[str, Any]:
        """
        Set (add) IMAP keywords/flags on a message.

        Args:
            folder: Source folder containing the message
            uid: Message UID
            keywords: List of keywords/flags to set

        Returns:
            Tuple of (status, response) from IMAP STORE command
        """
        assert client is not None
        try:
            # Select the folder
            sel_typ, sel_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=sel_typ,
                response=sel_resp,
                folder=folder,
                uid=uid,
            )
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            # Build flags string - ensure proper formatting
            flags_str = " ".join(keywords)

            # Execute STORE command to add keywords
            typ, resp = client.uid("STORE", uid, "+FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_set_keywords",
                op_label="UID STORE +FLAGS",
                status=typ,
                response=resp,
                folder=folder,
                uid=uid,
            )

            if typ == "OK":
                console_msg = f"   🏷️  Set keywords on {folder}/{uid}: {keywords}" if verbose else None
                logger.log(
                    "INFO",
                    "execute_set_keywords_done",
                    {"folder": folder, "uid": uid, "keywords": keywords},
                    console=console_msg,
                )
                # Send action success notification
                logger.log(
                    "INFO",
                    "execute_action_success",
                    {"action_type": "set_keywords", "folder": folder, "uid": uid, "keywords": keywords},
                )
            else:
                error_msg = _format_imap_details(resp)
                logger.log(
                    "ERROR",
                    "execute_set_keywords_failed",
                    {
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "status": typ,
                        "details": error_msg,
                    },
                )
                # Send action failure notification
                logger.log(
                    "ERROR",
                    "execute_action_failed",
                    {
                        "action_type": "set_keywords",
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "error": error_msg,
                    },
                )

            return typ, resp

        except Exception as exc:
            logger.log(
                "ERROR",
                "execute_set_keywords_exception",
                {"folder": folder, "uid": uid, "keywords": keywords, "error": str(exc)},
            )
            raise

    def _execute_remove_keywords(
        folder: str,
        uid: str,
        keywords: list[str],
    ) -> tuple[str, Any]:
        """
        Remove IMAP keywords/flags from a message.

        Args:
            folder: Source folder containing the message
            uid: Message UID
            keywords: List of keywords/flags to remove

        Returns:
            Tuple of (status, response) from IMAP STORE command
        """
        assert client is not None
        try:
            # Select the folder
            sel_typ, sel_resp = client.select(f'"{folder}"')
            log_imap_call(
                "imap_select",
                op_label=f'SELECT "{folder}"',
                status=sel_typ,
                response=sel_resp,
                folder=folder,
                uid=uid,
            )
            if sel_typ != "OK":
                raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

            # Build flags string - ensure proper formatting
            flags_str = " ".join(keywords)

            # Execute STORE command to remove keywords
            typ, resp = client.uid("STORE", uid, "-FLAGS", f"({flags_str})")
            log_imap_call(
                "imap_uid_store_remove_keywords",
                op_label="UID STORE -FLAGS",
                status=typ,
                response=resp,
                folder=folder,
                uid=uid,
            )

            if typ == "OK":
                console_msg = f"   🏷️  Removed keywords from {folder}/{uid}: {keywords}" if verbose else None
                logger.log(
                    "INFO",
                    "execute_remove_keywords_done",
                    {"folder": folder, "uid": uid, "keywords": keywords},
                    console=console_msg,
                )
                # Send action success notification
                logger.log(
                    "INFO",
                    "execute_action_success",
                    {"action_type": "remove_keywords", "folder": folder, "uid": uid, "keywords": keywords},
                )
            else:
                error_msg = _format_imap_details(resp)
                logger.log(
                    "ERROR",
                    "execute_remove_keywords_failed",
                    {
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "status": typ,
                        "details": error_msg,
                    },
                )
                # Send action failure notification
                logger.log(
                    "ERROR",
                    "execute_action_failed",
                    {
                        "action_type": "remove_keywords",
                        "folder": folder,
                        "uid": uid,
                        "keywords": keywords,
                        "error": error_msg,
                    },
                )

            return typ, resp

        except Exception as exc:
            logger.log(
                "ERROR",
                "execute_remove_keywords_exception",
                {"folder": folder, "uid": uid, "keywords": keywords, "error": str(exc)},
            )
            raise

    def _do_reconnect() -> bool:
        """Reconnect to the IMAP server after a connection loss.

        Updates ``client`` and ``supports_uid_move`` in the enclosing scope so
        all subsequent IMAP calls in this execute_actions() invocation use the
        fresh connection.  Returns True on success, False otherwise.
        """
        nonlocal client, supports_uid_move
        if reconnect_fn is None or dry_run:
            return False
        for attempt in range(1, 4):
            try:
                logger.log(
                    "WARN",
                    "imap_reconnecting",
                    {"attempt": attempt},
                    console=f"   🔄 SSL connection lost — reconnecting (attempt {attempt}/3)...",
                )
                client = reconnect_fn()
                supports_uid_move = _has_capability("MOVE")
                logger.log(
                    "INFO",
                    "imap_reconnected",
                    {"attempt": attempt},
                    console="   ✅ Reconnected to IMAP server",
                )
                return True
            except Exception as conn_exc:
                logger.log(
                    "ERROR",
                    "imap_reconnect_failed",
                    {"attempt": attempt, "error": str(conn_exc)},
                    console=f"   ❌ Reconnect attempt {attempt} failed: {conn_exc}",
                )
                if attempt < 3:
                    time.sleep(5 * attempt)
        return False

    def flush_group() -> None:
        nonlocal current_key, current_items, current_rule_name
        if current_key is None or not current_items:
            return

        folder, target, action_type = current_key
        display_target = target or "(no target)"
        total_for_group = group_totals.get(current_key, len(current_items))
        folders_bar.set_postfix_str(f"{folder} → {display_target}")

        # Extract metadata from first item
        # current_items: (action_id, uid, rule_name, action_type, action_data)
        rule_name = current_items[0][2] if current_items else None

        # Handle keyword actions (set_keywords, remove_keywords)
        if action_type in ("set_keywords", "remove_keywords"):
            if dry_run:
                for a_id, uid, _, _, action_data in current_items:
                    keywords = []
                    if action_data:
                        try:
                            data = json.loads(action_data)
                            keywords = data.get("keywords", [])
                        except json.JSONDecodeError:
                            pass

                    actions_bar.update(1)
                    if verbose:
                        logger.log(
                            "INFO",
                            "dry_action_preview",
                            {"folder": folder, "uid": uid, "action_type": action_type, "keywords": keywords},
                            console=f"   📝 Would {action_type} on {folder}/{uid}: {keywords}",
                        )
            else:
                assert client is not None

                # Select the folder once for all per-message STORE operations.
                sel_typ, sel_resp = client.select(f'"{folder}"')
                log_imap_call(
                    "imap_select",
                    op_label=f'SELECT "{folder}"',
                    status=sel_typ,
                    response=sel_resp,
                    folder=folder,
                )
                if sel_typ != "OK":
                    for a_id, uid, _, _, _ in current_items:
                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                            (now_iso(), a_id),
                        )
                        stats["failed"] += 1
                        actions_bar.update(1)
                    db.commit()
                    logger.log(
                        "ERROR",
                        f"{action_type}_select_failed",
                        {"folder": folder, "status": sel_typ},
                        console=f"   ❌ Cannot select {folder} for {action_type}",
                    )
                    folders_bar.update(1)
                    current_items = []
                    current_key = None
                    return

                store_op = "+FLAGS" if action_type == "set_keywords" else "-FLAGS"

                for a_id, uid, _, _, action_data in current_items:
                    keywords = []
                    if action_data:
                        try:
                            data = json.loads(action_data)
                            keywords = data.get("keywords", [])
                        except json.JSONDecodeError:
                            logger.log(
                                "WARN",
                                "action_data_parse_failed",
                                {"folder": folder, "uid": uid, "action_id": a_id},
                            )

                    if not keywords:
                        logger.log(
                            "WARN",
                            f"{action_type}_empty",
                            {"folder": folder, "uid": uid},
                        )
                        db.execute(
                            "UPDATE actions SET status='skipped', executed_at=? WHERE id=?",
                            (now_iso(), a_id),
                        )
                        stats["skipped"] += 1
                        actions_bar.update(1)
                        continue

                    flags_str = " ".join(keywords)
                    try:
                        typ, resp = client.uid("STORE", uid, store_op, f"({flags_str})")
                        log_imap_call(
                            f"imap_uid_store_{action_type}",
                            op_label=f"UID STORE {store_op}",
                            status=typ,
                            response=resp,
                            folder=folder,
                            uid=uid,
                        )
                        if typ == "OK":
                            db.execute(
                                "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                                (now_iso(), a_id),
                            )
                            stats["done"] += 1
                            if verbose:
                                logger.log(
                                    "INFO",
                                    f"{action_type}_done",
                                    {"folder": folder, "uid": uid, "keywords": keywords},
                                    console=f"   🏷️  {action_type} {folder}/{uid}: {keywords}",
                                )
                        else:
                            error_detail = _format_imap_details(resp)
                            db.execute(
                                "UPDATE actions SET status='failed', executed_at=?, error_message=? WHERE id=?",
                                (now_iso(), error_detail, a_id),
                            )
                            stats["failed"] += 1
                            logger.log(
                                "ERROR",
                                f"{action_type}_failed",
                                {"folder": folder, "uid": uid, "keywords": keywords, "error": error_detail},
                            )
                    except imaplib.IMAP4.error as exc:
                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=?, error_message=? WHERE id=?",
                            (now_iso(), str(exc), a_id),
                        )
                        stats["failed"] += 1
                        logger.log(
                            "ERROR",
                            f"{action_type}_imap_error",
                            {"folder": folder, "uid": uid, "error": str(exc)},
                        )

                    actions_bar.update(1)

                db.commit()

            folders_bar.update(1)
            current_items = []
            current_key = None
            return

        # Handle move actions (original logic)
        uids = [uid for _, uid, _, _, _ in current_items]

        # Skip move actions that have no target folder — attempting IMAP COPY/MOVE with
        # target=None causes slow per-message IMAP failures and effectively stalls execution.
        if not target:
            for a_id, _, _, _, _ in current_items:
                db.execute(
                    "UPDATE actions SET status='skipped', executed_at=?, error_message=? WHERE id=?",
                    (now_iso(), "No target folder specified", a_id),
                )
                stats["skipped"] += 1
                actions_bar.update(1)
            if not dry_run:
                db.commit()
            logger.log(
                "WARN",
                "execute_no_target_skipped",
                {"folder": folder, "count": len(current_items)},
                console=f"   ⊘ Skipped {len(current_items)} actions in {folder}: no target folder",
            )
            folders_bar.update(1)
            current_items = []
            current_key = None
            return

        # Backup messages before moving (if requested and not in dry-run)
        backup_result: BackupResult | None = None
        if backup_moved and not dry_run and client is not None:
            assert backup_dir is not None
            backup_result = backup_messages(
                client=client,
                folder=folder,
                uids=uids,
                backup_dir=backup_dir,
                backup_type="pre_move",
                logger=logger,
                show_progress=show_progress,
                rule_name=rule_name,
                target_folder=target,
            )

            # Check if backup succeeded - if not, we should not proceed
            if backup_result.backed_up == 0 and len(uids) > 0:
                logger.log(
                    "ERROR",
                    "backup_failed_abort",
                    {"folder": folder, "target": target, "uid_count": len(uids)},
                    console=f"❌ Backup failed for {folder}. Skipping moves for safety.",
                )
                # Mark actions as failed
                for a_id, _, _, _, _ in current_items:
                    db.execute(
                        "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                        (now_iso(), a_id),
                    )
                db.commit()
                stats["failed"] += len(current_items)
                folders_bar.update(1)
                current_items = []
                current_key = None
                return

            if backup_result.failed > 0:
                logger.log(
                    "WARN",
                    "backup_partial",
                    {
                        "folder": folder,
                        "backed_up": backup_result.backed_up,
                        "failed": backup_result.failed,
                    },
                    console=f"⚠️  Partial backup: {backup_result.backed_up}/{len(uids)} succeeded",
                )

            # Track backup stats
            stats["backed_up"] = stats.get("backed_up", 0) + backup_result.backed_up
            stats["backup_failed"] = stats.get("backup_failed", 0) + backup_result.failed

        if dry_run:
            if show_progress:
                msgs_bar = tqdm(
                    total=total_for_group,
                    desc=f"   🚚 Moving {folder}",
                    unit="msg",
                    dynamic_ncols=True,
                    leave=False,
                    position=2,
                    disable=not show_progress,
                )
                msgs_bar.update(len(uids))
                msgs_bar.close()
            logger.log(
                "INFO",
                "dry_action_group",
                {"folder": folder, "target": target, "count": len(uids)},
                console=f"🧪 Dry run: {folder} → {display_target} ({len(uids)})",
            )
            for uid in uids:
                actions_bar.update(1)
                if verbose:
                    logger.log(
                        "INFO",
                        "dry_action_preview",
                        {"folder": folder, "target": target, "uid": uid},
                        console=f"   📝 Would move {folder}/{uid} → {display_target}",
                    )
        else:
            assert client is not None  # for type checkers
            msgs_bar = tqdm(
                total=total_for_group,
                desc=f"   🚚 Moving {folder}",
                unit="msg",
                dynamic_ncols=True,
                leave=False,
                position=2,
                disable=not show_progress,
            )
            try:
                try:
                    sel_typ, sel_resp = client.select(f'"{folder}"')
                except imaplib.IMAP4.error as _sel_exc:
                    if _is_connection_dead(_sel_exc) and _do_reconnect():
                        sel_typ, sel_resp = client.select(f'"{folder}"')
                    else:
                        raise
                log_imap_call(
                    "imap_select",
                    op_label=f'SELECT "{folder}"',
                    status=sel_typ,
                    response=sel_resp,
                    folder=folder,
                )
                if sel_typ != "OK":
                    raise imaplib.IMAP4.error(f"Cannot open folder {folder}")

                mismatch = _uidvalidity_mismatch(db, client, folder)
                if mismatch is not None:
                    cached_uv, live_uv = mismatch
                    error_msg = (
                        f"UIDVALIDITY changed for {folder} "
                        f"(cached {cached_uv}, server {live_uv}); "
                        "cached UIDs are stale — rebuild the cache"
                    )
                    for a_id, _, _, _, _ in current_items:
                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=?, error_message=? WHERE id=?",
                            (now_iso(), error_msg, a_id),
                        )
                        actions_bar.update(1)
                    db.commit()
                    stats["failed"] += len(current_items)
                    logger.log(
                        "ERROR",
                        "execute_uidvalidity_mismatch",
                        {
                            "folder": folder,
                            "cached": cached_uv,
                            "live": live_uv,
                            "count": len(current_items),
                        },
                        console=(
                            f"🛑 {folder}: UIDVALIDITY changed (cached {cached_uv}, "
                            f"server {live_uv}) — {len(current_items)} actions failed; rebuild cache"
                        ),
                    )
                    raise imaplib.IMAP4.error(error_msg)

                target_ready = target is None
                successful_moves: list[tuple[int, str]] = []  # Track (action_id, uid) for pre-EXPUNGE verification

                def _record_success(action_id: int, uid_value: str) -> None:
                    """Mark action as successful and track for later verification."""
                    if verify_moves and target and not dry_run:
                        # Prime the message-id cache while the header row still
                        # exists — _verify_move runs after the DELETE below and
                        # would otherwise always skip with missing_message_id.
                        _cached_message_id(folder, uid_value)
                        successful_moves.append((action_id, uid_value))
                    db.execute(
                        "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                        (now_iso(), action_id),
                    )
                    removed = db.execute(
                        "DELETE FROM headers WHERE folder=? AND uid=?",
                        (folder, uid_value),
                    ).rowcount
                    stats["done"] += 1

                    console_msg = f"   ✅ Moved {folder}/{uid_value} → {display_target}" if verbose else None
                    logger.log(
                        "INFO",
                        "execute_uid_done",
                        {"folder": folder, "target": target, "uid": uid_value},
                        console=console_msg,
                    )
                    # Send action success notification
                    logger.log(
                        "INFO",
                        "execute_action_success",
                        {"action_type": "move", "folder": folder, "uid": uid_value, "target": target},
                    )
                    if removed:
                        logger.log(
                            "INFO",
                            "execute_header_removed",
                            {"folder": folder, "uid": uid_value},
                            console=(
                                f"      🧹 Removed cached header for {folder}/{uid_value}"
                                if verbose
                                else None
                            ),
                        )

                def _verify_move(action_id: int, uid_value: str) -> None:
                    """Verify a successful move before EXPUNGE to prevent accidental deletion."""
                    message_id = _cached_message_id(folder, uid_value)
                    if not message_id:
                        logger.log(
                            "DEBUG",
                            "execute_verify_missing_message_id",
                            {
                                "folder": folder,
                                "target": target,
                                "uid": uid_value,
                            },
                        )
                        return

                    verify_errors: list[str] = []
                    source_found = False
                    source_status = "OK"

                    try:
                        # Ensure source folder is selected for verification
                        sel_typ, sel_resp = client.select(f'"{folder}"')
                        if sel_typ != "OK":
                            source_found = True
                            source_status = "ERROR"
                            verify_errors.append(f"Cannot select {folder}: {sel_typ}")
                        else:
                            source_found, source_status, _ = _uid_search_mailbox(
                                folder,
                                message_id,
                                target=target,
                                uid=uid_value,
                            )
                    except Exception as search_exc:
                        source_found = True
                        source_status = "ERROR"
                        verify_errors.append(str(search_exc))

                    dest_found = True
                    dest_status = "OK"

                    try:
                        dest_found, dest_status = _verify_destination_mailbox(
                            folder,
                            target,
                            message_id,
                            uid=uid_value,
                        )
                    except Exception as dest_exc:
                        dest_found = False
                        dest_status = "ERROR"
                        verify_errors.append(str(dest_exc))

                    issues: list[str] = []
                    if source_status != "OK":
                        issues.append("source_search_failed")
                    if source_found:
                        issues.append("source_present")
                    if dest_status != "OK":
                        issues.append("destination_search_failed")
                    if not dest_found:
                        issues.append("destination_missing")

                    if issues:
                        db.execute(
                            "UPDATE actions SET status=?, executed_at=? WHERE id=?",
                            ("failed", now_iso(), action_id),
                        )
                        stats["failed"] += 1
                        stats["done"] -= 1

                        context = {
                            "folder": folder,
                            "target": target,
                            "uid": uid_value,
                            "message_id": message_id,
                            "issues": issues,
                        }
                        if verify_errors:
                            context["errors"] = verify_errors

                        console_warn = None
                        if verbose:
                            console_warn = (
                                f"   ⚠️ Verification failed for {folder}/{uid_value} → {display_target}"
                            )
                        logger.log(
                            "WARN",
                            "execute_verify_failed",
                            context,
                            console=console_warn,
                        )

                # --- Batch move attempt (O(1) round-trips for the happy path) ---
                # Pre-separate same-folder skips so the batch UID set is clean.
                items_to_move = []
                for a_id, uid, rn, at, ad in current_items:
                    if target and folder == target:
                        db.execute(
                            "UPDATE actions SET status='skipped', executed_at=?, error_message=? WHERE id=?",
                            (now_iso(), 'Already in target folder', a_id),
                        )
                        stats["skipped"] += 1
                        actions_bar.update(1)
                        if verbose:
                            logger.log(
                                "INFO",
                                "skipped_redundant_move",
                                {"folder": folder, "uid": uid, "target": target},
                                console=f"      ⊘ {folder}/{uid} already in {target}",
                            )
                    else:
                        items_to_move.append((a_id, uid, rn, at, ad))

                # Batch move, processed in chunks to avoid oversized IMAP commands.
                # Most IMAP servers enforce an ~8 KB command-line limit; 500 UIDs ≤ ~5 KB.
                _MOVE_CHUNK = 500
                batch_succeeded = False
                batch_fallback: list = []  # items whose chunk failed → per-message retry
                batch_store_retry: list = []  # COPY landed but STORE failed → retry STORE only

                if items_to_move and target and not dry_run:
                    method = "UID MOVE" if supports_uid_move else "UID COPY+STORE"
                    chunks = [
                        items_to_move[i : i + _MOVE_CHUNK]
                        for i in range(0, len(items_to_move), _MOVE_CHUNK)
                    ]
                    batch_done_count = 0
                    for chunk in chunks:
                        uid_set = ",".join(uid for _, uid, _, _, _ in chunk if uid)
                        if not uid_set:
                            continue
                        chunk_ok = False
                        chunk_copied = False  # COPY landed in target; only the STORE may still be owed
                        try:
                            if supports_uid_move:
                                b_typ, b_resp = client.uid("MOVE", uid_set, f'"{target}"')
                                log_imap_call(
                                    "imap_uid_move_batch",
                                    op_label="UID MOVE (batch)",
                                    status=b_typ,
                                    response=b_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid_set,
                                )
                                if b_typ != "OK" and not target_ready and _should_try_create_folder(b_resp):
                                    c_typ, c_resp = client.create(f'"{target}"')
                                    log_imap_call("imap_create", op_label="CREATE", status=c_typ,
                                                  response=c_resp, folder=folder, target=target)
                                    if c_typ == "OK":
                                        target_ready = True
                                        logger.log("INFO", "create_missing_target",
                                                   {"folder": folder, "target": target},
                                                   console=f"   📁 Created missing folder {target}")
                                        b_typ, b_resp = client.uid("MOVE", uid_set, f'"{target}"')
                                        log_imap_call("imap_uid_move_batch", op_label="UID MOVE (batch, retry)",
                                                      status=b_typ, response=b_resp,
                                                      folder=folder, target=target, uid=uid_set)
                                if b_typ == "OK":
                                    if not target_ready:
                                        target_ready = True
                                    chunk_ok = True
                            else:
                                b_typ, b_resp = client.uid("COPY", uid_set, f'"{target}"')
                                log_imap_call(
                                    "imap_uid_copy_batch",
                                    op_label="UID COPY (batch)",
                                    status=b_typ,
                                    response=b_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid_set,
                                )
                                if b_typ != "OK" and not target_ready and _should_try_create_folder(b_resp):
                                    c_typ, c_resp = client.create(f'"{target}"')
                                    log_imap_call("imap_create", op_label="CREATE", status=c_typ,
                                                  response=c_resp, folder=folder, target=target)
                                    if c_typ == "OK":
                                        target_ready = True
                                        logger.log("INFO", "create_missing_target",
                                                   {"folder": folder, "target": target},
                                                   console=f"   📁 Created missing folder {target}")
                                        b_typ, b_resp = client.uid("COPY", uid_set, f'"{target}"')
                                        log_imap_call("imap_uid_copy_batch", op_label="UID COPY (batch, retry)",
                                                      status=b_typ, response=b_resp,
                                                      folder=folder, target=target, uid=uid_set)
                                if b_typ == "OK":
                                    if not target_ready:
                                        target_ready = True
                                    chunk_copied = True
                                    s_typ, s_resp = client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                                    log_imap_call(
                                        "imap_uid_store_batch",
                                        op_label="UID STORE +FLAGS (batch)",
                                        status=s_typ,
                                        response=s_resp,
                                        folder=folder,
                                        target=target,
                                        uid=uid_set,
                                    )
                                    if s_typ == "OK":
                                        chunk_ok = True

                        except imaplib.IMAP4.error as batch_exc:
                            if _is_invalid_mailbox_name_error(str(batch_exc)):
                                logger.log(
                                    "ERROR",
                                    "execute_batch_invalid_mailbox",
                                    {"folder": folder, "target": target, "error": str(batch_exc)},
                                    console=f"❌ Invalid folder name '{target}': rename the rule target to avoid '.' in the folder name",
                                )
                                for a_id, uid, _, _, _ in chunk:
                                    db.execute(
                                        "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                        (now_iso(), a_id),
                                    )
                                    stats["failed"] += 1
                                batch_fallback.clear()
                                batch_succeeded = True  # suppress per-message fallback loop
                                break
                            if _is_connection_dead(batch_exc) and _do_reconnect():
                                try:
                                    client.select(f'"{folder}"')
                                except Exception:
                                    pass
                            logger.log(
                                "WARN",
                                "execute_batch_move_failed",
                                {"folder": folder, "target": target, "error": str(batch_exc)},
                                console=f"   ⚠️ Batch move chunk failed, retrying per-message: {batch_exc}",
                            )
                            chunk_ok = False

                        if chunk_ok:
                            for a_id, uid, _, _, _ in chunk:
                                _record_success(a_id, uid)
                                msgs_bar.update(1)
                                actions_bar.update(1)
                            batch_done_count += len(chunk)
                        elif chunk_copied:
                            # Re-running the full move would COPY again and
                            # duplicate the chunk in the target; only the
                            # STORE +\Deleted gets retried per-message below.
                            batch_store_retry.extend(chunk)
                        else:
                            batch_fallback.extend(chunk)

                    if batch_done_count:
                        logger.log(
                            "INFO",
                            "execute_batch_move_done",
                            {
                                "folder": folder,
                                "target": target,
                                "count": batch_done_count,
                                "fallback": len(batch_fallback),
                                "method": method,
                            },
                            console=(
                                f"   ✅ Batch {method}: "
                                f"{batch_done_count} messages → {display_target}"
                                + (f" ({len(batch_fallback)} retrying per-msg)" if batch_fallback else "")
                            ),
                        )

                    # STORE-only retry for chunks that were copied but not flagged.
                    for a_id, uid, _, _, _ in batch_store_retry:
                        store_ok = False
                        store_error = ""
                        try:
                            s_typ, s_resp = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                            log_imap_call(
                                "imap_uid_store_retry",
                                op_label="UID STORE +FLAGS (retry)",
                                status=s_typ,
                                response=s_resp,
                                folder=folder,
                                target=target,
                                uid=uid,
                            )
                            store_ok = s_typ == "OK"
                            if not store_ok:
                                store_error = _imap_response_text(s_resp)
                        except imaplib.IMAP4.error as store_exc:
                            store_error = str(store_exc)

                        if store_ok:
                            _record_success(a_id, uid)
                        else:
                            error_msg = (
                                f"Copied to {target} but STORE \\Deleted failed"
                                f"{': ' + store_error if store_error else ''} — "
                                "message now exists in both source and target"
                            )
                            db.execute(
                                "UPDATE actions SET status='failed', executed_at=?, error_message=? WHERE id=?",
                                (now_iso(), error_msg, a_id),
                            )
                            stats["failed"] += 1
                            logger.log(
                                "ERROR",
                                "execute_move_store_retry_failed",
                                {"folder": folder, "target": target, "uid": uid, "error": store_error},
                            )
                        msgs_bar.update(1)
                        actions_bar.update(1)

                    batch_succeeded = len(batch_fallback) == 0

                # Per-message fallback: items whose batch chunk failed (or dry-run path)
                _per_msg_items = batch_fallback if (items_to_move and target and not dry_run) else items_to_move
                for a_id, uid, _, _, _ in ([] if batch_succeeded else _per_msg_items):
                    # Guard: Skip if email is already in target folder (dry-run path)
                    if target and folder == target:
                        db.execute(
                            "UPDATE actions SET status='skipped', executed_at=?, error_message=? WHERE id=?",
                            (now_iso(), 'Already in target folder', a_id),
                        )
                        stats["skipped"] += 1
                        actions_bar.update(1)
                        if verbose:
                            logger.log(
                                "INFO",
                                "skipped_redundant_move",
                                {"folder": folder, "uid": uid, "target": target},
                                console=f"      ⊘ {folder}/{uid} already in {target}",
                            )
                        continue

                    deleted_flagged = False
                    try:
                        move_typ = "NO"
                        move_resp = None
                        if supports_uid_move and target:
                            try:
                                move_typ, move_resp = client.uid(
                                    "MOVE", uid, f'"{target}"'
                                )
                            except Exception as move_exc:
                                log_imap_call(
                                    "imap_uid_move",
                                    op_label="UID MOVE",
                                    status="ERROR",
                                    response=[str(move_exc)],
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            else:
                                log_imap_call(
                                    "imap_uid_move",
                                    op_label="UID MOVE",
                                    status=move_typ,
                                    response=move_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                                if move_typ == "OK":
                                    if target and not target_ready:
                                        target_ready = True
                                    _record_success(a_id, uid)
                                    continue

                        typ1, copy_resp = client.uid("COPY", uid, f'"{target}"')
                        log_imap_call(
                            "imap_uid_copy",
                            op_label="UID COPY",
                            status=typ1,
                            response=copy_resp,
                            folder=folder,
                            target=target,
                            uid=uid,
                        )
                        if (
                            typ1 != "OK"
                            and target
                            and not target_ready
                            and _should_try_create_folder(copy_resp)
                        ):
                            if verbose:
                                logger.log(
                                    "WARN",
                                    "imap_copy_missing_target",
                                    {
                                        "folder": folder,
                                        "target": target,
                                        "uid": uid,
                                        "status": typ1,
                                        "details": _format_imap_details(copy_resp),
                                    },
                                    console=(
                                        f"      ↪ UID COPY retry required {typ1}{_format_imap_details(copy_resp)}"
                                        f" (UID {uid} → {target})"
                                    ),
                                )
                            create_typ, create_resp = client.create(f'"{target}"')
                            log_imap_call(
                                "imap_create",
                                op_label="CREATE",
                                status=create_typ,
                                response=create_resp,
                                folder=folder,
                                target=target,
                            )
                            if create_typ != "OK":
                                create_details = _format_imap_details(create_resp)
                                raise imaplib.IMAP4.error(
                                    f"CREATE {target} failed{create_details}"
                                )
                            target_ready = True
                            logger.log(
                                "INFO",
                                "create_missing_target",
                                {"folder": folder, "target": target},
                                console=f"   📁 Created missing folder {target}",
                            )
                            typ1, copy_resp = client.uid("COPY", uid, f'"{target}"')
                            log_imap_call(
                                "imap_uid_copy",
                                op_label="UID COPY",
                                status=typ1,
                                response=copy_resp,
                                folder=folder,
                                target=target,
                                uid=uid,
                            )

                        if typ1 != "OK":
                            details = _format_imap_details(copy_resp)
                            raise imaplib.IMAP4.error(f"UID COPY failed{details}")

                        if target and not target_ready:
                            target_ready = True

                        typ2, store_resp = client.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                        log_imap_call(
                            "imap_uid_store",
                            op_label="UID STORE +FLAGS",
                            status=typ2,
                            response=store_resp,
                            folder=folder,
                            target=target,
                            uid=uid,
                        )
                        if typ2 != "OK":
                            raise imaplib.IMAP4.error("UID STORE +FLAGS \\Deleted failed")
                        deleted_flagged = True

                        _record_success(a_id, uid)

                    except imaplib.IMAP4.error as exc:
                        if deleted_flagged:
                            try:
                                cleanup_typ, cleanup_resp = client.uid(
                                    "STORE", uid, "-FLAGS", "(\\Deleted)"
                                )
                                log_imap_call(
                                    "imap_uid_store_cleanup",
                                    op_label="UID STORE -FLAGS",
                                    status=cleanup_typ,
                                    response=cleanup_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            except Exception:  # pragma: no cover - best effort cleanup
                                pass
                        message = str(exc)
                        if _is_invalid_mailbox_name_error(message):
                            db.execute(
                                "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                (now_iso(), a_id),
                            )
                            db.commit()
                            stats["failed"] += 1
                            logger.log(
                                "ERROR",
                                "invalid_mailbox_name",
                                {"uid": uid, "folder": folder, "target": target, "error": message},
                                console=f"❌ Invalid folder name '{target}': rename the rule target to avoid '.' in the folder name",
                            )
                            continue
                        if (
                            "no such message" in message.lower()
                            or "uid command error" in message.lower()
                            or "failed" in message.lower()
                        ):
                            if strict:
                                db.execute(
                                    "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                                    (now_iso(), a_id),
                                )
                                db.commit()
                                logger.log(
                                    "ERROR",
                                    "message_missing_strict_abort",
                                    {
                                        "uid": uid,
                                        "folder": folder,
                                        "target": target,
                                        "error": str(exc),
                                    },
                                    console=f"💥 STRICT: missing UID {uid} in {folder} — aborting",
                                )
                                # Log failure for GOTIFY notification
                                logger.log(
                                    "WARN",
                                    "imap_move_failed",
                                    {"folder": folder, "target": target, "uid": uid, "error": str(exc)}
                                )
                                raise
                            db.execute(
                                "UPDATE actions SET status='skipped', executed_at=? WHERE id=?",
                                (now_iso(), a_id),
                            )
                            try:
                                db.execute(
                                    "DELETE FROM headers WHERE uid=? AND folder=?",
                                    (uid, folder),
                                )
                                logger.log("INFO", "cache_cleanup", {"uid": uid, "folder": folder})
                            except Exception as cleanup_exc:  # pragma: no cover - best effort cleanup
                                logger.log(
                                    "WARN",
                                    "cache_cleanup_failed",
                                    {"uid": uid, "folder": folder, "error": str(cleanup_exc)},
                                )
                            stats["skipped"] += 1
                            logger.log(
                                "WARN",
                                "message_missing_skipped",
                                {
                                    "uid": uid,
                                    "folder": folder,
                                    "target": target,
                                    "error": str(exc),
                                },
                                console=(
                                    f"   ⚠️ Skipped missing {folder}/{uid}: {exc}"
                                    if verbose
                                    else None
                                ),
                            )
                            continue

                        # Reconnect on dead SSL/socket connection and reset UID to
                        # pending so it is retried on the next run.  Subsequent UIDs
                        # in this loop will then use the fresh connection.
                        if _is_connection_dead(exc) and _do_reconnect():
                            try:
                                client.select(f'"{folder}"')
                            except Exception:
                                pass
                            db.execute(
                                "UPDATE actions SET status='pending' WHERE id=?",
                                (a_id,),
                            )
                            db.commit()
                            logger.log(
                                "WARN",
                                "imap_reconnect_uid_requeued",
                                {"uid": uid, "folder": folder, "target": target},
                                console=f"   🔄 Requeued {folder}/{uid} after reconnect",
                            )
                            continue

                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                            (now_iso(), a_id),
                        )
                        stats["failed"] += 1
                        error_str = str(exc)
                        logger.log(
                            "ERROR",
                            "execute_failed",
                            {"uid": uid, "folder": folder, "target": target, "error": error_str},
                            console=f"❌ {folder}/{uid}: {exc}",
                        )
                        # Log failure for GOTIFY notification
                        logger.log(
                            "WARN",
                            "imap_move_failed",
                            {"folder": folder, "target": target, "uid": uid, "error": error_str}
                        )
                        # Send action failure notification for feature parity
                        logger.log(
                            "ERROR",
                            "execute_action_failed",
                            {"action_type": "move", "folder": folder, "uid": uid, "target": target, "error": error_str}
                        )
                    except Exception:
                        if deleted_flagged:
                            try:
                                cleanup_typ, cleanup_resp = client.uid(
                                    "STORE", uid, "-FLAGS", "(\\Deleted)"
                                )
                                log_imap_call(
                                    "imap_uid_store_cleanup",
                                    op_label="UID STORE -FLAGS",
                                    status=cleanup_typ,
                                    response=cleanup_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid,
                                )
                            except Exception:  # pragma: no cover - best effort cleanup
                                pass
                        raise
                    finally:
                        msgs_bar.update(1)
                        actions_bar.update(1)

                # CRITICAL FIX: Verify all successful moves BEFORE EXPUNGE
                # This prevents messages from being deleted if verification fails
                verified_uids: set[str] = set()  # UIDs that passed verification
                if successful_moves and verify_moves:
                    for action_id, uid_value in successful_moves:
                        try:
                            _verify_move(action_id, uid_value)
                            verified_uids.add(uid_value)
                        except Exception as verify_exc:  # pragma: no cover
                            logger.log(
                                "ERROR",
                                "execute_verify_exception",
                                {
                                    "action_id": action_id,
                                    "uid": uid_value,
                                    "error": str(verify_exc),
                                },
                                console=f"❌ Verification failed for {folder}/{uid_value}: {verify_exc}",
                            )
                            # Remove \Deleted flag for failed verification to prevent accidental expunge
                            try:
                                cleanup_typ, cleanup_resp = client.uid(
                                    "STORE", uid_value, "-FLAGS", "(\\Deleted)"
                                )
                                log_imap_call(
                                    "imap_uid_store_cleanup_failed_verify",
                                    op_label="UID STORE -FLAGS (verification failed)",
                                    status=cleanup_typ,
                                    response=cleanup_resp,
                                    folder=folder,
                                    target=target,
                                    uid=uid_value,
                                )
                                logger.log(
                                    "INFO",
                                    "execute_verify_cleanup_flag",
                                    {"folder": folder, "uid": uid_value, "action": "restored from deletion"},
                                    console=f"   🔄 Restored {folder}/{uid_value} (verification failed)",
                                )
                            except Exception as cleanup_exc:  # pragma: no cover
                                logger.log(
                                    "WARN",
                                    "execute_verify_cleanup_flag_failed",
                                    {
                                        "folder": folder,
                                        "uid": uid_value,
                                        "error": str(cleanup_exc),
                                    },
                                )
                    db.commit()  # Commit any verification results

                db.commit()  # Commit move results before expunge so they survive a timeout

                # Use CLOSE (not EXPUNGE) to silently purge \Deleted messages.
                # EXPUNGE sends a * EXPUNGE response for every renumbered message in the mailbox —
                # on a 500k-message INBOX this is extremely slow. CLOSE achieves the same purge
                # without per-message notifications. The next flush_group() re-selects the folder.
                logger.log("DEBUG", "imap_close_start", {"folder": folder, "target": target})
                try:
                    cls_typ, cls_resp = client.close()
                    log_imap_call(
                        "imap_close",
                        op_label="CLOSE",
                        status=cls_typ,
                        response=cls_resp,
                        folder=folder,
                        target=target,
                    )
                except Exception as cls_exc:  # pragma: no cover - best effort cleanup
                    logger.log("WARN", "imap_close_failed", {"folder": folder, "error": str(cls_exc)})
                logger.log(
                    "INFO",
                    "execute_folder_done",
                    {"folder": folder, "target": target, "moved": len(uids)},
                    console=f"✅ {folder}: handled {len(uids)} → {display_target}",
                )

            except Exception as exc:
                if strict:
                    raise
                logger.log(
                    "ERROR",
                    "execute_group_failed",
                    {"folder": folder, "target": target, "error": str(exc)},
                    console=f"💥 Group failed: {folder} → {target}: {exc}",
                )
            finally:
                msgs_bar.close()

        folders_bar.update(1)
        current_items = []
        current_key = None

    while True:
        rows = select_cur.fetchmany(chunk_size)
        if not rows:
            break
        for a_id, uid, folder, _raw_target, rule_name, _priority, _created_at, action_type, action_data in rows:
            target = _encode_mailbox_utf7(_raw_target) if _raw_target else _raw_target
            # Group by (folder, target, action_type) to prevent mixing moves with keyword actions
            key = (folder, target, action_type)
            if current_key is not None and key != current_key:
                flush_group()
            if key != current_key:
                current_key = key
                current_rule_name = rule_name
            current_items.append((a_id, uid, rule_name, action_type, action_data))

    flush_group()
    folders_bar.close()
    actions_bar.close()

    # Backup all cached messages if requested (after moves complete)
    if backup_all and not dry_run and client is not None:
        assert backup_dir is not None
        logger.log(
            "INFO",
            "backup_all_start",
            console="\n💾 Backing up all cached messages...",
        )

        backup_results = backup_all_cached_messages(
            client=client,
            db=db,
            backup_dir=backup_dir,
            folders=list(folders) if folders else None,
            logger=logger,
            show_progress=show_progress,
        )

        total_backed_up = sum(r.backed_up for r in backup_results.values())
        total_failed = sum(r.failed for r in backup_results.values())
        stats["backed_up"] = stats.get("backed_up", 0) + total_backed_up
        stats["backup_failed"] = stats.get("backup_failed", 0) + total_failed

    timer.stop()
    timer.count = stats["done"]

    # Build summary message
    summary_parts = [
        "\n📊 Summary — Execute Actions\n",
        f"   📦  Actions executed: {stats['done']}\n",
        f"   ⚠️  Skipped (missing): {stats['skipped']}\n",
        f"   🚫  Suppressed (duplicates): {stats['suppressed']}\n",
        f"   💥  Failed: {stats['failed']}\n",
    ]

    # Add backup stats if applicable
    if "backed_up" in stats:
        summary_parts.append(f"   💾  Messages backed up: {stats['backed_up']}\n")
    if stats.get("backup_failed", 0) > 0:
        summary_parts.append(f"   ⚠️  Backup failures: {stats['backup_failed']}\n")

    summary_parts.extend([
        f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n",
        f"   {'🔒 STRICT' if strict else '✅ Completed'} {'(dry-run)' if dry_run else ''}\n",
    ])

    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "execute",
            **stats,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console="".join(summary_parts),
    )
    return timer, stats


