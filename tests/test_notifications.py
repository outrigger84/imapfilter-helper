"""Tests for NotificationDispatcher, including --notification-summary's
digest mode (suppressing per-event notifications in favor of one digest
per phase) and the trash-aware execute_summary breakdown."""
from __future__ import annotations

from core.notifications import NotificationDispatcher


class FakeTelegram:
    """Minimal stand-in for TelegramNotifier that records what was sent."""

    _disabled = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, int]] = []

    def send(self, title: str, message: str, priority: int = 0, extras=None) -> bool:
        self.sent.append((title, message, priority))
        return True


def _dispatcher(summary_mode: bool) -> tuple[NotificationDispatcher, FakeTelegram]:
    telegram = FakeTelegram()
    dispatcher = NotificationDispatcher(telegram_notifier=telegram, summary_mode=summary_mode)
    return dispatcher, telegram


def test_summary_mode_suppresses_rule_match():
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "rule_match", {
        "rule": "The Economist", "folder": "The Economist", "action_type": "move",
        "target": "Deleted Messages", "dry_run": True, "age_gate": "age > 365 days",
    })
    assert telegram.sent == []


def test_summary_mode_suppresses_execute_action_events():
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "execute_action_success", {
        "action_type": "move", "folder": "INBOX", "uid": "1", "target": "Archive",
    })
    dispatcher.dispatch("ERROR", "execute_action_failed", {
        "action_type": "move", "folder": "INBOX", "uid": "2", "target": "Archive", "error": "boom",
    })
    assert telegram.sent == []


def test_non_summary_mode_still_sends_rule_match():
    dispatcher, telegram = _dispatcher(summary_mode=False)
    dispatcher.dispatch("INFO", "rule_match", {
        "rule": "The Economist", "folder": "The Economist", "action_type": "move",
        "target": "Deleted Messages", "dry_run": True, "age_gate": None,
    })
    assert len(telegram.sent) == 1
    title, body, _priority = telegram.sent[0]
    assert "Rule Matched" in title
    assert "The Economist" in body


def test_phase_summary_only_sent_in_summary_mode():
    context = {
        "phase": "evaluate",
        "matches": 2,
        "matches_by_rule_and_target": {
            "The Economist": {"The Economist": 1, "Deleted Messages": 1},
        },
    }

    dispatcher_off, telegram_off = _dispatcher(summary_mode=False)
    dispatcher_off.dispatch("INFO", "phase_summary", context)
    assert telegram_off.sent == []  # unchanged default behavior: silent

    dispatcher_on, telegram_on = _dispatcher(summary_mode=True)
    dispatcher_on.dispatch("INFO", "phase_summary", context)
    assert len(telegram_on.sent) == 1
    title, body, _priority = telegram_on.sent[0]
    assert "Evaluate Summary" in title
    assert "The Economist" in body
    assert "Deleted Messages: 1" in body
    assert "🎯 Total matches: 2" in body


def test_phase_summary_ignores_cache_phase_even_in_summary_mode():
    """phase_summary is shared with cache_builder.py -- its shape has no
    matches_by_rule_and_target breakdown, so it's never a valid digest."""
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "phase_summary", {"phase": "cache", "folders": 3, "messages": 100})
    assert telegram.sent == []


def test_phase_summary_covers_stream_execute_phase_too():
    """stream_execute() logs phase_summary with phase='stream-execute' and
    its own matches_by_rule_and_target -- same digest renderer as evaluate."""
    context = {
        "phase": "stream-execute",
        "matches": 5,
        "matches_by_rule_and_target": {"Cal Responses": {"Deleted Messages": 5}},
    }
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "phase_summary", context)
    assert len(telegram.sent) == 1
    _title, body, _priority = telegram.sent[0]
    assert "Cal Responses" in body
    assert "Deleted Messages: 5" in body


def test_phase_summary_title_is_phase_aware():
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "phase_summary", {
        "phase": "stream-execute", "matches": 1,
        "matches_by_rule_and_target": {"Rule": {"Target": 1}},
    })
    title, _body, _priority = telegram.sent[0]
    assert title == "📊 Stream Summary"


def test_stream_summary_suppressed_in_favor_of_phase_summary_digest():
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "stream_summary", {"matched": 5, "stream_done": 5})
    assert telegram.sent == []


def test_stream_summary_sent_with_real_numbers_outside_summary_mode():
    dispatcher, telegram = _dispatcher(summary_mode=False)
    dispatcher.dispatch("INFO", "stream_summary", {
        "matched": 5, "stream_done": 4, "stream_failed": 1, "stream_skipped": 2,
    })
    assert len(telegram.sent) == 1
    _title, body, _priority = telegram.sent[0]
    assert "Matched: 5" in body
    assert "Moved: 4" in body
    assert "Failed: 1" in body
    assert "Skipped: 2" in body


def test_phase_summary_shows_no_op_bracket_matches():
    """A fired bracket with an empty 'do' (e.g. '2FA codes: stay in INBOX
    today') is a real decision and must appear in the digest, not silently
    vanish because no actual action ran."""
    context = {
        "phase": "evaluate",
        "matches": 3,
        "matches_by_rule_and_target": {
            "Cal Responses": {"(no action)": 3},
        },
    }
    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "phase_summary", context)
    assert len(telegram.sent) == 1
    _title, body, _priority = telegram.sent[0]
    assert "⏸️ (no action yet): 3" in body


def test_phase_summary_truncates_long_rule_list():
    by_rule_target = {
        f"Rule {i}": {"Some Folder": 1} for i in range(12)
    }
    context = {"phase": "evaluate", "matches": 12, "matches_by_rule_and_target": by_rule_target}

    dispatcher, telegram = _dispatcher(summary_mode=True)
    dispatcher.dispatch("INFO", "phase_summary", context)

    assert len(telegram.sent) == 1
    _title, body, _priority = telegram.sent[0]
    assert "and 4 more rule(s)" in body


def test_execute_summary_breaks_down_moved_vs_deleted_regardless_of_mode():
    context = {"done": 5, "done_trash": 2, "failed": 1, "skipped": 0}

    for summary_mode in (False, True):
        dispatcher, telegram = _dispatcher(summary_mode=summary_mode)
        dispatcher.dispatch("INFO", "execute_summary", context)
        assert len(telegram.sent) == 1
        _title, body, _priority = telegram.sent[0]
        assert "📂 Moved: 3" in body
        assert "🗑️ Deleted: 2" in body
        assert "❌ Failed: 1" in body


def test_execute_summary_includes_rule_target_breakdown_like_phase_summary():
    """The plain `execute` command should list what actually moved where,
    not just a bare done/failed/skipped count -- mirrors the evaluate
    digest's per-rule breakdown so a long unattended run stays legible."""
    by_rule_target = {
        "Newsletters": {"Archive": 4, "Deleted Messages": 1},
        "Spam Ring A": {"Deleted Messages": 2},
    }
    context = {
        "done": 7, "done_trash": 3, "failed": 0, "skipped": 0,
        "matches_by_rule_and_target": by_rule_target,
    }

    dispatcher, telegram = _dispatcher(summary_mode=False)
    dispatcher.dispatch("INFO", "execute_summary", context)

    assert len(telegram.sent) == 1
    _title, body, _priority = telegram.sent[0]
    assert "📂 Moved: 4" in body
    assert "🗑️ Deleted: 3" in body
    assert "🏷️ Newsletters" in body
    assert "🏷️ Spam Ring A" in body
    assert "📂 → Archive: 4" in body
    assert "🗑️ → Deleted Messages: 2" in body


def test_run_summary_reads_flattened_exec_keys_not_a_nested_stats_dict():
    """Regression test: run_summary used to read context["stats"], but
    handle_run_all logs flattened exec_* keys instead, so this notification
    always rendered zeros. It should now report the real counts and, like
    execute_summary, the per-rule/target breakdown."""
    context = {
        "folders": 5, "messages": 120, "matches": 9,
        "exec_done": 7, "exec_done_trash": 3, "exec_failed": 1, "exec_skipped": 0,
        "exec_matches_by_rule_and_target": {"Newsletters": {"Archive": 4}},
    }

    dispatcher, telegram = _dispatcher(summary_mode=False)
    dispatcher.dispatch("INFO", "run_summary", context)

    assert len(telegram.sent) == 1
    _title, body, priority = telegram.sent[0]
    assert "🗂️ Folders: 5" in body
    assert "✉️ Messages: 120" in body
    assert "🎯 Matches: 9" in body
    assert "📂 Moved: 4" in body
    assert "🗑️ Deleted: 3" in body
    assert "❌ Failed: 1" in body
    assert "🏷️ Newsletters" in body
    assert "📂 → Archive: 4" in body
    assert priority == 3  # bumped because failed > 0


def test_eval_execute_summary_is_dispatched():
    """eval_execute_summary was logged but missing from notify_events, so it
    was silently never sent. It should now fire with the same digest style
    as execute_summary/run_summary."""
    context = {
        "rules": 12, "matches": 4,
        "exec_done": 4, "exec_done_trash": 0, "exec_failed": 0, "exec_skipped": 0,
        "exec_matches_by_rule_and_target": {"Spam Ring A": {"Deleted Messages": 4}},
    }

    dispatcher, telegram = _dispatcher(summary_mode=False)
    dispatcher.dispatch("INFO", "eval_execute_summary", context)

    assert len(telegram.sent) == 1
    _title, body, priority = telegram.sent[0]
    assert "🧩 Rules: 12" in body
    assert "🎯 Matches: 4" in body
    assert "📂 Moved: 4" in body
    assert "🏷️ Spam Ring A" in body
    assert priority == 2  # no failures


def test_no_notifications_means_no_notifier_no_dispatch():
    """--no-notifications leaves the notifier unconstructed entirely (tested
    at the core/cli.py level) -- here we just confirm summary_mode alone,
    with no notifiers configured at all, is a safe no-op."""
    dispatcher = NotificationDispatcher(summary_mode=True)
    # Should not raise even though no gotify/telegram notifier is configured.
    dispatcher.dispatch("INFO", "rule_match", {"rule": "X"})
    dispatcher.dispatch("INFO", "phase_summary", {"phase": "evaluate", "matches": 1})
