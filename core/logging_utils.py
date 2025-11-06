"""Logging utilities for the IMAPFilter helper."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def now_iso() -> str:
    """Return a timezone-aware ISO8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


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
        entry: Dict[str, Any] = {"timestamp": now_iso(), "level": level, "message": message}
        if context:
            entry["context"] = context
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if console:
            from tqdm import tqdm  # Imported lazily to avoid global side effects

            tqdm.write(console)
