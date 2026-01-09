"""Notification adapters for external services like GOTIFY."""
from __future__ import annotations

import json
import requests
from typing import Any, Dict, Optional
from urllib.parse import urljoin


class GotifyNotifier:
    """Send notifications to a GOTIFY instance."""

    def __init__(self, base_url: str, token: str):
        """
        Initialize GOTIFY notifier.

        Args:
            base_url: GOTIFY instance URL (e.g., http://gotify.example.com)
            token: GOTIFY application token for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.endpoint = urljoin(self.base_url + "/", "message")

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
            return True

        except requests.exceptions.RequestException as e:
            # Log the error but don't raise to avoid disrupting mail processing
            print(f"Failed to send GOTIFY notification: {e}")
            return False


class NotificationDispatcher:
    """Dispatch notifications based on event type."""

    def __init__(self, gotify_notifier: Optional[GotifyNotifier] = None):
        """
        Initialize dispatcher.

        Args:
            gotify_notifier: GOTIFY notifier instance (optional)
        """
        self.gotify = gotify_notifier

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
        if not self.gotify:
            return

        # Only send notifications for important events
        notify_events = {
            "rule_match": ("Rule Matched", "info"),
            "imap_move_success": ("Email Moved", "success"),
            "imap_move_failed": ("Email Move Failed", "warn"),
            "execute_pending_count": ("Executing Actions", "info"),
            "run_summary": ("Sync Complete", "success"),
            "stream_summary": ("Stream Complete", "success"),
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

        else:
            return

        # Send the notification
        self.gotify.send(
            title=title,
            message=body,
            priority=priority,
            extras={
                "event": message,
                "level": level,
            },
        )
