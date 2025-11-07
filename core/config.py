"""Configuration helpers for the IMAPFilter helper."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Default filesystem layout -------------------------------------------------

DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path("data")
DEFAULT_RULES_PATH = Path("rules")
DEFAULT_SECRETS_PATH = DEFAULT_DATA_DIR / "secrets.json"
DEFAULT_CACHE_PATH = DEFAULT_DATA_DIR / "cache.db"
DEFAULT_LOG_PATH = DEFAULT_DATA_DIR / "imapfilter-helper.log"


@dataclass
class PathsConfig:
    base_dir: Path
    data_dir: Path = field(init=False)
    rules_dir: Path = field(init=False)
    secrets_file: Path = field(init=False)
    db_file: Path = field(init=False)
    log_file: Path = field(init=False)

    def __post_init__(self) -> None:
        base = Path(self.base_dir).resolve()
        self.base_dir = base
        self.data_dir = base / DEFAULT_DATA_DIR
        self.rules_dir = base / DEFAULT_RULES_PATH
        self.secrets_file = base / DEFAULT_SECRETS_PATH
        self.db_file = base / DEFAULT_CACHE_PATH
        self.log_file = base / DEFAULT_LOG_PATH


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
    resolved = Path(base_dir).resolve() if base_dir else DEFAULT_BASE_DIR
    return AppConfig(paths=PathsConfig(base_dir=resolved))
