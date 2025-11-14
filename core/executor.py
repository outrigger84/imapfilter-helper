"""Execute queued actions."""
from __future__ import annotations

import json
import imaplib
from email.parser import HeaderParser
from email.policy import default
from typing import Dict, Iterable, Sequence

from tqdm import tqdm

from core.logging_utils import JsonLogger, PhaseTimer, now_iso


HEADER_PARSER = HeaderParser(policy=default)


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


def execute_actions(
    client: imaplib.IMAP4 | None,
    db,
    *,
    show_progress: bool,
    dry_run: bool,
    strict: bool,
    logger: JsonLogger,
    verbose: bool = False,
    limit: int | None = None,
    folders: Sequence[str] | None = None,
    verify_moves: bool = False,
) -> tuple[PhaseTimer, Dict[str, int]]:
    if not dry_run and client is None:
        raise ValueError("An IMAP client is required when not running in dry-run mode")

    timer = PhaseTimer("execute")

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

    pending_cur = db.cursor()
    pending_cur.execute(
        "SELECT COUNT(*) FROM actions WHERE status='pending'" + folder_filter,
        folder_params,
    )
    pending_total = pending_cur.fetchone()[0] or 0
    if pending_total == 0:
        logger.log("INFO", "execute_nothing", {"dry_run": dry_run}, console="ℹ️ No pending actions")
        return timer, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    distinct_cur = db.cursor()
    distinct_cur.execute(
        "SELECT COUNT(DISTINCT uid) FROM actions WHERE status='pending'" + folder_filter,
        folder_params,
    )
    distinct_uids = distinct_cur.fetchone()[0] or 0
    suppressed = pending_total - distinct_uids

    order_clause = "ORDER BY folder, target, priority DESC, created_at ASC, id ASC"
    dedup_cte = """
        WITH ranked AS (
            SELECT
                id,
                uid,
                folder,
                target,
                rule_name,
                priority,
                created_at,
                ROW_NUMBER() OVER (
                    PARTITION BY uid
                    ORDER BY priority DESC, created_at ASC, id ASC
                ) AS rn
            FROM actions
            WHERE status='pending'
    )
"""
    if folder_filter:
        dedup_cte = dedup_cte.replace(
            "WHERE status='pending'",
            f"WHERE status='pending'{folder_filter}",
        )
    selection_source = "ranked WHERE rn=1"
    limit_param: tuple[int, ...] = ()
    if limit is not None:
        dedup_cte += (
            "    , limited AS (\n"
            "        SELECT id, uid, folder, target, rule_name, priority, created_at\n"
            "        FROM ranked\n"
            "        WHERE rn=1\n"
            f"        {order_clause}\n"
            "        LIMIT ?\n"
            "    )\n"
        )
        selection_source = "limited"
        limit_param = (int(limit),)

    dedup_params = folder_params + limit_param

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

    folders_bar = tqdm(
        total=len(group_totals) if group_totals else None,
        desc="📦 Executing folders",
        unit="folder",
        dynamic_ncols=True,
        leave=True,
        position=0,
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
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        value = parsed.get("Message-ID") or parsed.get("Message-Id")
                        if isinstance(value, str) and value.strip():
                            message_id = value.strip()
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
        + "SELECT id, uid, folder, target, rule_name, priority, created_at "
        f"FROM {selection_source} "
        f"{order_clause}",
        dedup_params,
    )

    chunk_size = 512
    current_key: tuple[str, str | None] | None = None
    current_items: list[tuple[int, str]] = []

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

    def flush_group() -> None:
        nonlocal current_key, current_items
        if current_key is None or not current_items:
            return

        folder, target = current_key
        display_target = target or "(no target)"
        total_for_group = group_totals.get(current_key, len(current_items))
        folders_bar.set_postfix_str(f"{folder} → {display_target}")
        uids = [uid for _, uid in current_items]

        if dry_run:
            if show_progress:
                msgs_bar = tqdm(
                    total=total_for_group,
                    desc=f"   🚚 Moving {folder}",
                    unit="msg",
                    dynamic_ncols=True,
                    leave=False,
                    position=1,
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
            if verbose:
                for uid in uids:
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
                position=1,
                disable=not show_progress,
            )
            try:
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

                target_ready = target is None

                def _finalize_success(action_id: int, uid_value: str) -> bool:
                    if verify_moves and target and not dry_run:
                        message_id = _cached_message_id(folder, uid_value)
                        if message_id:
                            verify_errors: list[str] = []
                            source_found = False
                            source_status = "OK"
                            try:
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
                                return False
                        else:
                            if verbose:
                                logger.log(
                                    "DEBUG",
                                    "execute_verify_missing_message_id",
                                    {
                                        "folder": folder,
                                        "target": target,
                                        "uid": uid_value,
                                    },
                                )
                    db.execute(
                        "UPDATE actions SET status='done', executed_at=? WHERE id=?",
                        (now_iso(), action_id),
                    )
                    stats["done"] += 1
                    console_msg: str | None = None
                    if verbose:
                        console_msg = f"   ✅ Moved {folder}/{uid_value} → {display_target}"
                    logger.log(
                        "INFO",
                        "execute_uid_done",
                        {"folder": folder, "target": target, "uid": uid_value},
                        console=console_msg,
                    )
                    return True

                for a_id, uid in current_items:
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
                                    _finalize_success(a_id, uid)
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

                        if not _finalize_success(a_id, uid):
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
                                deleted_flagged = False
                            continue

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
                        message = str(exc).lower()
                        if (
                            "no such message" in message
                            or "uid command error" in message
                            or "failed" in message
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

                        db.execute(
                            "UPDATE actions SET status='failed', executed_at=? WHERE id=?",
                            (now_iso(), a_id),
                        )
                        stats["failed"] += 1
                        logger.log(
                            "ERROR",
                            "execute_failed",
                            {"uid": uid, "folder": folder, "target": target, "error": str(exc)},
                            console=f"❌ {folder}/{uid}: {exc}",
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

                try:
                    exp_typ, exp_resp = client.expunge()
                    log_imap_call(
                        "imap_expunge",
                        op_label="EXPUNGE",
                        status=exp_typ,
                        response=exp_resp,
                        folder=folder,
                        target=target,
                    )
                except Exception:  # pragma: no cover - best effort cleanup
                    pass

                db.commit()
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
        for a_id, uid, folder, target, _rule_name, _priority, _created_at in rows:
            key = (folder, target)
            if current_key is not None and key != current_key:
                flush_group()
            if key != current_key:
                current_key = key
            current_items.append((a_id, uid))

    flush_group()
    folders_bar.close()

    timer.stop()
    timer.count = stats["done"]
    logger.log(
        "INFO",
        "phase_summary",
        {
            "phase": "execute",
            **stats,
            "elapsed_sec": timer.elapsed,
            "rate": timer.rate(),
        },
        console=(
            "\n📊 Summary — Execute Actions\n"
            f"   📦  Actions executed: {stats['done']}\n"
            f"   ⚠️  Skipped (missing): {stats['skipped']}\n"
            f"   🚫  Suppressed (duplicates): {stats['suppressed']}\n"
            f"   💥  Failed: {stats['failed']}\n"
            f"   ⏱️  Duration: {timer.fmt()} ({timer.rate():.1f} msg/s)\n"
            f"   {'🔒 STRICT' if strict else '✅ Completed'} {'(dry-run)' if dry_run else ''}\n"
        ),
    )
    return timer, stats
