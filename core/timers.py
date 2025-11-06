"""Timing utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter


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
