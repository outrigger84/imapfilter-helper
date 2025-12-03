"""Resume capability for streaming execution."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Set

from core.logging_utils import JsonLogger


class ResumeLog:
    """Track which messages have been processed for resume capability."""

    def __init__(self, log_file: Path):
        """Initialize resume log."""
        self.log_file = Path(log_file)
        self.processed: dict[str, Set[str]] = {}  # folder -> set of UIDs
        self._load()

    def _load(self) -> None:
        """Load resume state from disk."""
        if not self.log_file.exists():
            return

        try:
            with self.log_file.open("r") as f:
                data = json.load(f)
                # Convert lists back to sets
                self.processed = {
                    folder: set(uids) for folder, uids in data.items()
                }
        except Exception:
            # If corrupted, start fresh
            self.processed = {}

    def _save(self) -> None:
        """Save resume state to disk."""
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        # Convert sets to lists for JSON
        data = {
            folder: sorted(list(uids)) for folder, uids in self.processed.items()
        }
        with self.log_file.open("w") as f:
            json.dump(data, f, indent=2)

    def is_processed(self, folder: str, uid: str) -> bool:
        """Check if a message has already been processed."""
        return uid in self.processed.get(folder, set())

    def mark_processed(self, folder: str, uid: str) -> None:
        """Mark a message as processed."""
        if folder not in self.processed:
            self.processed[folder] = set()
        self.processed[folder].add(uid)
        self._save()

    def mark_processed_batch(self, updates: dict[str, list[str]]) -> None:
        """Mark multiple messages as processed (more efficient for batches)."""
        for folder, uids in updates.items():
            if folder not in self.processed:
                self.processed[folder] = set()
            self.processed[folder].update(uids)
        self._save()

    def clear(self) -> None:
        """Clear all resume state."""
        self.processed.clear()
        if self.log_file.exists():
            self.log_file.unlink()

    def stats(self) -> dict[str, int]:
        """Get statistics about processed messages."""
        return {folder: len(uids) for folder, uids in self.processed.items()}


def create_resume_log(
    base_log_file: Path,
    *,
    logger: JsonLogger,
    session_id: str = "default",
) -> ResumeLog:
    """
    Create a resume log for a streaming session.

    Args:
        base_log_file: Base log file path
        logger: JsonLogger instance
        session_id: Session identifier (for multiple concurrent streams)

    Returns:
        ResumeLog instance
    """
    # Create session-specific log file
    stem = base_log_file.stem
    suffix = base_log_file.suffix
    resume_file = base_log_file.parent / f"{stem}-resume-{session_id}{suffix}"

    resume_log = ResumeLog(resume_file)

    if resume_log.stats():
        logger.log(
            "INFO",
            "stream_resume_loaded",
            {"session": session_id, "stats": resume_log.stats()},
            console=f"📋 Resuming from previous session (processed: {sum(resume_log.stats().values())} messages)",
        )
    else:
        logger.log(
            "INFO",
            "stream_resume_new",
            {"session": session_id},
            console="📋 Starting new streaming session",
        )

    return resume_log
