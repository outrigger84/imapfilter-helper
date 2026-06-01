"""Notification adapters for external services like GOTIFY and Telegram."""
from __future__ import annotations

import requests
from typing import Any, Dict, Optional
from urllib.parse import urljoin


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

    def send(
        self,
        title: str,
        message: str,
        priority: int = 0,
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self._disabled:
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


class NotificationDispatcher:
    """Dispatch notifications based on event type."""

    def __init__(
        self,
        gotify_notifier: Optional[GotifyNotifier] = None,
        telegram_notifier: Optional[TelegramNotifier] = None,
    ):
        self.gotify = gotify_notifier
        self.telegram = telegram_notifier

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

        if self.gotify and self.gotify._disabled:
            print(f"⚠️  GOTIFY is disabled due to repeated timeouts. Event '{message}' will not be sent.")
        if self.telegram and self.telegram._disabled:
            print(f"⚠️  Telegram is disabled due to repeated timeouts. Event '{message}' will not be sent.")

        # Only send notifications for important events
        notify_events = {
            "rule_match": ("Rule Matched", "info"),
            "imap_move_success": ("Email Moved", "success"),
            "imap_move_failed": ("Email Move Failed", "warn"),
            "execute_action_success": ("Action Executed", "success"),
            "execute_action_failed": ("Action Failed", "warn"),
            "execute_pending_count": ("Executing Actions", "info"),
            "run_summary": ("Sync Complete", "success"),
            "stream_summary": ("Stream Complete", "success"),
            "cache_folder_done": ("Folder Cached", "info"),
            "cache_summary": ("Cache Complete", "success"),
            "execute_summary": ("Execute Complete", "success"),
            # mbox-import events
            "classify_done": ("MBOX Classified", "info"),
            "folder_upload_done": ("Folder Uploaded", "info"),
            "mbox_import_done": ("Import Complete", "success"),
        }

        if message not in notify_events:
            return

        title, event_type = notify_events[message]
        context = context or {}

        # Build notification message and priority
        if message == "rule_match":
            rule_name = context.get("rule", "Unknown")
            folder = context.get("folder", "")
            action = context.get("action_type", "move")
            target = context.get("target", "")

            if action == "move":
                body = f"Rule: {rule_name}\nFolder: {folder} → {target}"
            else:
                body = f"Rule: {rule_name}\nFolder: {folder}\nAction: {action}"

            priority = 5 if context.get("dry_run") else 3

        elif message == "imap_move_success":
            folder = context.get("folder", "")
            target = context.get("target", "")
            body = f"{folder} → {target}"
            priority = 2

        elif message == "imap_move_failed":
            folder = context.get("folder", "")
            target = context.get("target", "")
            error = context.get("error", "Unknown error")
            body = f"{folder} → {target}\nError: {error}"
            priority = 5

        elif message == "execute_action_success":
            action_type = context.get("action_type", "unknown")
            folder = context.get("folder", "")
            uid = context.get("uid", "")

            if action_type == "move":
                target = context.get("target", "")
                body = f"Move: {folder}/{uid} → {target}"
            elif action_type == "set_keywords":
                keywords = context.get("keywords", [])
                keywords_str = ", ".join(keywords) if keywords else "none"
                body = f"Set Keywords: {folder}/{uid}\nKeywords: {keywords_str}"
            elif action_type == "remove_keywords":
                keywords = context.get("keywords", [])
                keywords_str = ", ".join(keywords) if keywords else "none"
                body = f"Remove Keywords: {folder}/{uid}\nKeywords: {keywords_str}"
            else:
                body = f"Action: {action_type}\nLocation: {folder}/{uid}"
            priority = 1

        elif message == "execute_action_failed":
            action_type = context.get("action_type", "unknown")
            folder = context.get("folder", "")
            uid = context.get("uid", "")
            error = context.get("error", "Unknown error")

            if action_type == "move":
                target = context.get("target", "")
                body = f"Move: {folder}/{uid} → {target}\nError: {error}"
            elif action_type in ("set_keywords", "remove_keywords"):
                keywords = context.get("keywords", [])
                keywords_str = ", ".join(keywords) if keywords else "none"
                body = f"{action_type.replace('_', ' ').title()}: {folder}/{uid}\nKeywords: {keywords_str}\nError: {error}"
            else:
                body = f"Action: {action_type}\nLocation: {folder}/{uid}\nError: {error}"
            priority = 4

        elif message == "execute_pending_count":
            count = context.get("count", 0)
            body = f"Processing {count} actions"
            priority = 1

        elif message in ("run_summary", "stream_summary"):
            stats = context.get("stats", {})
            body = f"Total: {stats.get('total', 0)} messages\n"
            body += f"Matched: {stats.get('matched', 0)} rules\n"
            body += f"Executed: {stats.get('executed', 0)} actions"
            priority = 2

        elif message == "cache_folder_done":
            folder = context.get("folder", "")
            messages = context.get("messages", 0)
            body = f"Folder: {folder}\nMessages: {messages}"
            priority = 2

        elif message == "cache_summary":
            folders = context.get("folders", 0)
            messages = context.get("messages", 0)
            elapsed = context.get("elapsed_sec", 0)
            body = f"Folders: {folders}\nMessages: {messages}\nDuration: {elapsed:.1f}s"
            priority = 2

        elif message == "execute_summary":
            done = context.get("done", 0)
            failed = context.get("failed", 0)
            skipped = context.get("skipped", 0)
            body = f"Executed: {done}\nFailed: {failed}\nSkipped: {skipped}"
            priority = 3 if failed > 0 else 2

        elif message == "classify_done":
            total = context.get("total", 0)
            matched = context.get("matched", 0)
            unmatched = context.get("unmatched", 0)
            folders = context.get("folders", 0)
            body = f"Messages: {total}\nMatched by rules: {matched}\nUnmatched: {unmatched}\nTarget folders: {folders}"
            priority = 2

        elif message == "folder_upload_done":
            folder = context.get("folder", "")
            uploaded = context.get("uploaded", 0)
            failed = context.get("failed", 0)
            body = f"Folder: {folder}\nUploaded: {uploaded}"
            if failed:
                body += f"\nFailed: {failed}"
            priority = 4 if failed > 0 else 2

        elif message == "mbox_import_done":
            uploaded = context.get("total_uploaded", 0)
            failed = context.get("total_failed", 0)
            body = f"Uploaded: {uploaded} messages"
            if failed:
                body += f"\nFailed: {failed} messages"
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

        if self.telegram:
            print(f"📤 Sending Telegram notification: {title} (event: {message})")
            success = self.telegram.send(title=title, message=body, priority=priority)
            if success:
                print(f"✅ Telegram notification sent: {title}")
            else:
                print(f"❌ Telegram notification failed: {title}")
