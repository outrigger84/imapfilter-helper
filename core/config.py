"""Configuration helpers for the IMAPFilter helper."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class PathsConfig:
    base_dir: Path
    rules_dir: Path = field(init=False)
    secrets_file: Path = field(init=False)
    db_file: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        self.rules_dir = self.base_dir / "rules"
        self.secrets_file = self.base_dir / "secrets.json"
        self.db_file = self.base_dir / "cache.db"
        self.log_file = self.base_dir / "imapfilter-helper.log"


@dataclass
class LoggingConfig:
    show_progress: bool = True


@dataclass
class ExecutorConfig:
    default_run_scope: str = "inbox"
    dry_run: bool = False
    strict: bool = False


@dataclass
class AppConfig:
    paths: PathsConfig
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)


def build_default_config(base_dir: Optional[Path] = None) -> AppConfig:
    """Return the default configuration for the application."""
    resolved = Path(base_dir or Path.cwd()).resolve()
    return AppConfig(paths=PathsConfig(base_dir=resolved))
