"""Logging utilities for the IMAPFilter helper."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Optional


def now_iso() -> str:
    """Return a timezone-aware ISO8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def log(
    log_file: Path,
    level: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    console: Optional[str] = None,
) -> None:
    """Append a JSON log entry and optionally echo to the console."""

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: Dict[str, Any] = {"timestamp": now_iso(), "level": level, "message": message}
    if context:
        entry["context"] = context
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if console:
        from tqdm import tqdm  # Imported lazily to avoid global side effects

        tqdm.write(console)


@dataclass
class PhaseTimer:
    """Utility to track elapsed time for a logical phase."""

    phase: str
    start: float = field(default_factory=perf_counter)
    end: float | None = None
    count: int = 0

    def stop(self) -> None:
        self.end = perf_counter()

    @property
    def elapsed(self) -> float:
        return (self.end or perf_counter()) - self.start

    def rate(self) -> float:
        return self.count / self.elapsed if self.elapsed > 0 else 0.0

    def fmt(self) -> str:
        seconds = int(self.elapsed)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours} h {minutes} m {seconds} s"
        if minutes:
            return f"{minutes} m {seconds} s"
        return f"{seconds} s"


@dataclass
class JsonLogger:
    """Simple JSONL logger used by the helper."""

    log_file: Path

    def __post_init__(self) -> None:
        self.log_file = Path(self.log_file)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        level: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        console: Optional[str] = None,
    ) -> None:
        log(self.log_file, level, message, context, console=console)
