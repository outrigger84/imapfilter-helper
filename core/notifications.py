"""Notification adapters for external services like GOTIFY and Telegram."""
from __future__ import annotations

import queue
import requests
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

from core.executor.helpers import TRASH_LIKE_TARGETS

# Per-event notifications suppressed when NotificationDispatcher is
# constructed with summary_mode=True -- replaced by one digest per phase
# (see the "phase_summary" and "execute_summary" branches in dispatch()).
# "stream_summary" is included because stream_execute() also logs a richer
# "phase_summary" (phase="stream-execute") covering the same run -- without
# suppression, summary mode would send two notifications for one stream run.
_SUMMARY_SUPPRESSED_EVENTS = (
    "rule_match", "execute_action_success", "execute_action_failed", "stream_summary",
)

# Cap how many rules are listed in the evaluate digest, to keep it readable
# on a phone and well within Telegram's message-length limit.
_DIGEST_MAX_RULES = 8


class GotifyNotifier:
    """Send notifications to a GOTIFY instance."""

    def __init__(self, base_url: str, token: str, max_timeout_failures: int = 3):
        """
        Initialize GOTIFY notifier.

        Args:
            base_url: GOTIFY instance URL (e.g., http://gotify.example.com)
            token: GOTIFY application token for authentication
            max_timeout_failures: Number of consecutive timeouts before auto-disabling (default: 3)
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.endpoint = urljoin(self.base_url + "/", "message")
        self._timeout_count = 0
        self._max_timeout_failures = max_timeout_failures
        self._disabled = False

    def send(
        self,
        title: str,
        message: str,
        priority: int = 0,
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send a notification to GOTIFY.

        Args:
            title: Notification title
            message: Notification message body
            priority: Priority level (default 0, higher = more urgent)
            extras: Additional metadata to include in notification

        Returns:
            True if notification sent successfully, False otherwise
        """
        # Skip if GOTIFY has been auto-disabled due to repeated timeouts
        if self._disabled:
            return False

        try:
            payload = {
                "title": title,
                "message": message,
                "priority": priority,
            }

            if extras:
                payload["extras"] = {"client::display": {"contentType": "text/plain"}}
                for key, value in extras.items():
                    payload["extras"][f"imapfilter::{key}"] = value

            response = requests.post(
                self.endpoint,
                json=payload,
                params={"token": self.token},
                timeout=5,
            )
            response.raise_for_status()
            # Reset timeout counter on successful notification
            self._timeout_count = 0
            return True

        except requests.exceptions.Timeout as e:
            # Count timeout exceptions separately
            self._timeout_count += 1
            if self._timeout_count >= self._max_timeout_failures:
                self._disabled = True
                print(
                    f"GOTIFY auto-disabled after {self._timeout_count} consecutive timeouts"
                )
            else:
                print(
                    f"GOTIFY timeout ({self._timeout_count}/{self._max_timeout_failures}): {e}"
                )
            return False

        except requests.exceptions.RequestException as e:
            # For non-timeout errors, log but don't count toward threshold
            print(f"Failed to send GOTIFY notification: {e}")
            return False


class TelegramNotifier:
    """Send notifications via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str, max_timeout_failures: int = 3):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._timeout_count = 0
        self._max_timeout_failures = max_timeout_failures
        self._disabled = False
        self._retry_after_until: float = 0.0

    def send(
        self,
        title: str,
        message: str,
        priority: int = 0,
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self._disabled:
            return False

        if time.monotonic() < self._retry_after_until:
            return False

        text = f"<b>{title}</b>\n{message}"
        try:
            response = requests.post(
                self.endpoint,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=5,
            )

            if response.status_code == 429:
                retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                self._retry_after_until = time.monotonic() + retry_after
                print(f"Telegram rate-limited — retry after {retry_after}s")
                return False

            response.raise_for_status()
            self._timeout_count = 0
            return True

        except requests.exceptions.Timeout as e:
            self._timeout_count += 1
            if self._timeout_count >= self._max_timeout_failures:
                self._disabled = True
                print(f"Telegram auto-disabled after {self._timeout_count} consecutive timeouts")
            else:
                print(f"Telegram timeout ({self._timeout_count}/{self._max_timeout_failures}): {e}")
            return False

        except requests.exceptions.RequestException as e:
            print(f"Failed to send Telegram notification: {e}")
            return False


def _rule_target_digest_lines(by_rule_target: dict) -> list[str]:
    """Render a rule -> target -> count breakdown as digest lines, capped to
    _DIGEST_MAX_RULES rules (busiest first). Shared by the evaluate/stream
    "phase_summary" digest and the "execute_summary" digest so both read the
    same way."""
    rule_totals = sorted(
        ((name, sum(targets.values())) for name, targets in by_rule_target.items()),
        key=lambda kv: -kv[1],
    )
    shown = rule_totals[:_DIGEST_MAX_RULES]
    lines = []
    for rule_name, _total in shown:
        lines.append(f"🏷️ {rule_name}")
        targets = sorted(by_rule_target[rule_name].items(), key=lambda kv: -kv[1])
        for target, count in targets:
            if target == "(no action)":
                lines.append(f"   ⏸️ (no action yet): {count}")
            else:
                emoji = "🗑️" if target.strip().lower() in TRASH_LIKE_TARGETS else "📂"
                lines.append(f"   {emoji} → {target or '(no target)'}: {count}")
    remaining = len(rule_totals) - len(shown)
    if remaining > 0:
        lines.append(f"… and {remaining} more rule(s)")
    return lines


def _exec_summary_lines(context: dict, *, key_prefix: str = "") -> tuple[list[str], int]:
    """Moved/Deleted/Failed/Skipped counts plus the rule/target breakdown,
    shared by execute_summary, run_summary, and eval_execute_summary.

    run_summary and eval_execute_summary log their execute-phase stats
    flattened under an "exec_" prefix (to keep them alongside the
    cache/evaluate stats in the same context dict) -- key_prefix lets those
    two reuse this same renderer instead of duplicating it.
    """
    done = context.get(f"{key_prefix}done", 0)
    done_trash = context.get(f"{key_prefix}done_trash", 0)
    moved = max(done - done_trash, 0)
    failed = context.get(f"{key_prefix}failed", 0)
    skipped = context.get(f"{key_prefix}skipped", 0)
    lines = [
        f"📂 Moved: {moved}\n🗑️ Deleted: {done_trash}\n"
        f"❌ Failed: {failed}\n⏭️ Skipped: {skipped}"
    ]
    by_rule_target: dict = context.get(f"{key_prefix}matches_by_rule_and_target", {})
    lines.extend(_rule_target_digest_lines(by_rule_target))
    return lines, failed


class NotificationDispatcher:
    """Dispatch notifications based on event type."""

    def __init__(
        self,
        gotify_notifier: Optional[GotifyNotifier] = None,
        telegram_notifier: Optional[TelegramNotifier] = None,
        telegram_min_priority: int = 0,
        summary_mode: bool = False,
    ):
        self.gotify = gotify_notifier
        self.telegram = telegram_notifier
        self._telegram_min_priority = telegram_min_priority
        self._summary_mode = summary_mode

    def dispatch(
        self,
        level: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Dispatch notification based on event type and level.

        Args:
            level: Log level (INFO, WARN, ERROR)
            message: Event type/identifier
            context: Event context data
        """
        if not self.gotify and not self.telegram:
            return

        if self._summary_mode and message in _SUMMARY_SUPPRESSED_EVENTS:
            return

        if self.gotify and self.gotify._disabled:
            print(f"⚠️  GOTIFY is disabled due to repeated timeouts. Event '{message}' will not be sent.")
        if self.telegram and self.telegram._disabled:
            print(f"⚠️  Telegram is disabled due to repeated timeouts. Event '{message}' will not be sent.")

        # Only send notifications for important events
        notify_events = {
            "rule_match": ("🎯 Rule Matched", "info"),
            "imap_move_success": ("📬 Email Moved", "success"),
            "imap_move_failed": ("⚠️ Email Move Failed", "warn"),
            "execute_action_success": ("✅ Action Executed", "success"),
            "execute_action_failed": ("❌ Action Failed", "warn"),
            "execute_pending_count": ("⚙️ Executing Actions", "info"),
            "run_summary": ("🎉 Sync Complete", "success"),
            "eval_execute_summary": ("🔄 Eval-Execute Complete", "success"),
            "stream_summary": ("🌊 Stream Complete", "success"),
            "cache_folder_done": ("📁 Folder Cached", "info"),
            "cache_summary": ("🗄️ Cache Complete", "success"),
            "execute_summary": ("🏁 Execute Complete", "success"),
            "phase_summary": ("📊 Evaluate Summary", "info"),
            # mbox-import events
            "classify_done": ("🔍 MBOX Classified", "info"),
            "folder_upload_done": ("☁️ Folder Uploaded", "info"),
            "mbox_import_done": ("📦 Import Complete", "success"),
        }

        if message not in notify_events:
            return

        # "phase_summary" is shared by the cache/evaluate/stream phases with
        # different context shapes (see core/rule_engine.py, cache_builder.py,
        # stream_executor.py). Only evaluate and stream-execute carry a
        # matches_by_rule_and_target breakdown and are meaningful as a digest,
        # and only in summary mode -- otherwise this event stays silent
        # exactly as it does today (never dispatched).
        if message == "phase_summary" and (
            not self._summary_mode
            or (context or {}).get("phase") not in ("evaluate", "stream-execute")
        ):
            return

        title, event_type = notify_events[message]
        context = context or {}
        if message == "phase_summary" and context.get("phase") == "stream-execute":
            title = "📊 Stream Summary"

        # Build notification message and priority
        if message == "rule_match":
            rule_name = context.get("rule", "Unknown")
            folder = context.get("folder", "")
            action = context.get("action_type", "move")
            target = context.get("target", "")
            age_gate = context.get("age_gate")

            if action == "move":
                target_emoji = "🗑️" if target.strip().lower() in TRASH_LIKE_TARGETS else "📂"
                body = f"🏷️ Rule: {rule_name}\n{target_emoji} {folder} → {target}"
            else:
                body = f"🏷️ Rule: {rule_name}\n📁 {folder}\n⚙️ Action: {action}"

            if age_gate:
                body += f"\n⏳ Matched bracket: {age_gate}"

            priority = 5 if context.get("dry_run") else 3

        elif message == "imap_move_success":
            folder = context.get("folder", "")
            target = context.get("target", "")
            body = f"📤 {folder} → {target} 📥"
            priority = 2

        elif message == "imap_move_failed":
            folder = context.get("folder", "")
            target = context.get("target", "")
            error = context.get("error", "Unknown error")
            body = f"💥 {folder} → {target}\nError: {error}"
            priority = 5

        elif message == "execute_action_success":
            action_type = context.get("action_type", "unknown")
            folder = context.get("folder", "")
            uid = context.get("uid", "")

            if action_type == "move":
                target = context.get("target", "")
                body = f"📦 Move: {folder}/{uid} → {target}"
            else:
                body = f"⚙️ Action: {action_type}\n📍 Location: {folder}/{uid}"
            priority = 1

        elif message == "execute_action_failed":
            action_type = context.get("action_type", "unknown")
            folder = context.get("folder", "")
            uid = context.get("uid", "")
            error = context.get("error", "Unknown error")

            if action_type == "move":
                target = context.get("target", "")
                body = f"💥 Move: {folder}/{uid} → {target}\nError: {error}"
            else:
                body = f"💥 Action: {action_type}\n📍 Location: {folder}/{uid}\nError: {error}"
            priority = 4

        elif message == "execute_pending_count":
            count = context.get("count", 0)
            body = f"⏳ Processing {count} actions"
            priority = 1

        elif message == "run_summary":
            folders = context.get("folders", 0)
            messages = context.get("messages", 0)
            matches = context.get("matches", 0)
            exec_lines, failed = _exec_summary_lines(context, key_prefix="exec_")
            lines = [f"🗂️ Folders: {folders}\n✉️ Messages: {messages}\n🎯 Matches: {matches}"]
            lines.extend(exec_lines)
            body = "\n".join(lines)
            priority = 3 if failed > 0 else 2

        elif message == "eval_execute_summary":
            rules = context.get("rules", 0)
            matches = context.get("matches", 0)
            exec_lines, failed = _exec_summary_lines(context, key_prefix="exec_")
            lines = [f"🧩 Rules: {rules}\n🎯 Matches: {matches}"]
            lines.extend(exec_lines)
            body = "\n".join(lines)
            priority = 3 if failed > 0 else 2

        elif message == "stream_summary":
            # Only reached when summary_mode is False -- summary_mode
            # suppresses this in favor of the richer "phase_summary" digest.
            matched = context.get("matched", 0)
            done = context.get("stream_done", 0)
            failed = context.get("stream_failed", 0)
            skipped = context.get("stream_skipped", 0)
            body = (
                f"🎯 Matched: {matched}\n✅ Moved: {done}\n"
                f"❌ Failed: {failed}\n⏭️ Skipped: {skipped}"
            )
            priority = 3 if failed > 0 else 2

        elif message == "cache_folder_done":
            folder = context.get("folder", "")
            messages = context.get("messages", 0)
            body = f"📁 Folder: {folder}\n✉️ Messages: {messages}"
            priority = 2

        elif message == "cache_summary":
            folders = context.get("folders", 0)
            messages = context.get("messages", 0)
            elapsed = context.get("elapsed_sec", 0)
            body = f"📁 Folders: {folders}\n✉️ Messages: {messages}\n⏱️ Duration: {elapsed:.1f}s"
            priority = 2

        elif message == "execute_summary":
            lines, failed = _exec_summary_lines(context)
            body = "\n".join(lines)
            priority = 3 if failed > 0 else 2

        elif message == "phase_summary":
            total_matches = context.get("matches", 0)
            by_rule_target: dict = context.get("matches_by_rule_and_target", {})
            lines = [f"🎯 Total matches: {total_matches}"]
            lines.extend(_rule_target_digest_lines(by_rule_target))
            body = "\n".join(lines)
            priority = 2

        elif message == "classify_done":
            total = context.get("total", 0)
            matched = context.get("matched", 0)
            unmatched = context.get("unmatched", 0)
            folders = context.get("folders", 0)
            body = (
                f"✉️ Messages: {total}\n🎯 Matched by rules: {matched}\n"
                f"❔ Unmatched: {unmatched}\n📁 Target folders: {folders}"
            )
            priority = 2

        elif message == "folder_upload_done":
            folder = context.get("folder", "")
            uploaded = context.get("uploaded", 0)
            failed = context.get("failed", 0)
            body = f"📁 Folder: {folder}\n☁️ Uploaded: {uploaded}"
            if failed:
                body += f"\n❌ Failed: {failed}"
            priority = 4 if failed > 0 else 2

        elif message == "mbox_import_done":
            uploaded = context.get("total_uploaded", 0)
            failed = context.get("total_failed", 0)
            body = f"📦 Uploaded: {uploaded} messages"
            if failed:
                body += f"\n❌ Failed: {failed} messages"
            priority = 4 if failed > 0 else 2

        else:
            return

        # Send to all configured notifiers
        if self.gotify:
            print(f"📤 Sending GOTIFY notification: {title} (event: {message})")
            success = self.gotify.send(
                title=title,
                message=body,
                priority=priority,
                extras={"event": message, "level": level},
            )
            if success:
                print(f"✅ GOTIFY notification sent: {title}")
            else:
                print(f"❌ GOTIFY notification failed: {title}")

        if self.telegram and priority >= self._telegram_min_priority:
            print(f"📤 Sending Telegram notification: {title} (event: {message})")
            success = self.telegram.send(title=title, message=body, priority=priority)
            if success:
                print(f"✅ Telegram notification sent: {title}")
            else:
                print(f"❌ Telegram notification failed: {title}")


class AsyncNotificationDispatcher:
    """Non-blocking wrapper around NotificationDispatcher using a background thread queue."""

    def __init__(self, dispatcher: NotificationDispatcher):
        self._dispatcher = dispatcher
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def dispatch(
        self,
        level: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._queue.put_nowait((level, message, context))

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            level, message, context = item
            self._dispatcher.dispatch(level, message, context)
            self._queue.task_done()

    def shutdown(self, timeout: float = 10.0) -> None:
        """Flush remaining queued notifications and stop the background thread."""
        self._queue.put(None)
        self._thread.join(timeout=timeout)
