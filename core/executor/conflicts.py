"""Resolution of conflicting pending actions before execution."""
from __future__ import annotations

from core.logging_utils import JsonLogger, now_iso


# ============================================================================
# End Phase 2 Helper Functions
# ============================================================================


def resolve_pending_conflicts(db, logger: JsonLogger) -> int:
    """
    Cancel pending actions from lower-precedence rules when multiple rules have
    queued actions for the same (uid, folder).

    evaluate_rules is now first-match-wins, so conflicts should not arise from
    new runs. This function cleans up stale conflicts left over from earlier runs
    that used the old all-match behaviour.

    Returns the number of actions cancelled.
    """
    conflict_cur = db.execute(
        """
        SELECT uid, folder
        FROM actions
        WHERE status = 'pending'
        GROUP BY uid, folder
        HAVING COUNT(DISTINCT rule_name) > 1
        """
    )
    conflicts = conflict_cur.fetchall()
    if not conflicts:
        return 0

    cancelled = 0
    for uid, folder in conflicts:
        # The action with the lowest priority number belongs to the winning rule (lower = higher precedence).
        winner_cur = db.execute(
            """
            SELECT rule_name FROM actions
            WHERE uid = ? AND folder = ? AND status = 'pending'
            ORDER BY priority ASC
            LIMIT 1
            """,
            (uid, folder),
        )
        winner_row = winner_cur.fetchone()
        if not winner_row:
            continue
        winning_rule = winner_row[0]

        cancel_cur = db.execute(
            """
            UPDATE actions SET status = 'cancelled', executed_at = ?
            WHERE uid = ? AND folder = ? AND status = 'pending' AND rule_name != ?
            """,
            (now_iso(), uid, folder, winning_rule),
        )
        cancelled += cancel_cur.rowcount

    if cancelled:
        db.commit()
        logger.log(
            "INFO",
            "resolve_conflicts",
            {"cancelled": cancelled},
            console=f"⚖️  Resolved rule conflicts: {cancelled} lower-precedence action(s) cancelled",
        )
    return cancelled


