"""Logging utilities used throughout the helper application."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Mapping, MutableMapping, Optional

from tqdm import tqdm

from .config import get_log_path


def now_iso() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def log(
    level: str,
    message: str,
    context: Optional[Mapping[str, Any]] = None,
    *,
    console: Optional[str] = None,
    config: MutableMapping[str, Any] | None = None,
) -> None:
    """Write a structured log entry to the configured log file.

    Args:
        level: Severity level such as ``"INFO"`` or ``"ERROR"``.
        message: Short, machine-friendly identifier for the event.
        context: Optional mapping of additional structured details.
        console: Optional human-readable string to echo via ``tqdm.write``.
        config: Optional configuration override when writing the log.
    """
    entry = {"timestamp": now_iso(), "level": level, "message": message}
    if context:
        entry["context"] = context

    log_path = get_log_path(config)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if console:
        tqdm.write(console)


class PhaseTimer:
    """Utility for timing named phases and computing throughput."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.start = perf_counter()
        self.end: float | None = None
        self.count = 0

    def stop(self) -> None:
        """Stop the timer."""
        self.end = perf_counter()

    @property
    def elapsed(self) -> float:
        """Return the elapsed time in seconds."""
        reference = self.end if self.end is not None else perf_counter()
        return reference - self.start

    def rate(self) -> float:
        """Return the throughput based on ``self.count``."""
        elapsed = self.elapsed
        return self.count / elapsed if elapsed > 0 else 0.0

    def fmt(self) -> str:
        """Return a human-friendly representation of the elapsed time."""
        total_seconds = int(self.elapsed)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} h {minutes} m {seconds} s"
        if minutes:
            return f"{minutes} m {seconds} s"
        return f"{seconds} s"


__all__ = ["PhaseTimer", "log", "now_iso"]
