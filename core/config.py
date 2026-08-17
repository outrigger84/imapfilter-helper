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


# Path Configuration ---------------------------------------------------------


@dataclass
class PathsConfig:
    base_dir: Path
    data_dir: Path = field(init=False)
    rules_dir: Path = field(init=False)
    secrets_file: Path = field(init=False)
    db_file: Path = field(init=False)
    log_file: Path = field(init=False)
    backup_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        base = Path(self.base_dir).resolve()
        self.base_dir = base
        self.data_dir = base / DEFAULT_DATA_DIR
        self.rules_dir = base / DEFAULT_RULES_PATH
        self.secrets_file = base / DEFAULT_SECRETS_PATH
        self.db_file = base / DEFAULT_CACHE_PATH
        self.log_file = base / DEFAULT_LOG_PATH
        self.backup_dir = self.data_dir / "backups"

    @property
    def cache_db(self) -> Path:
        """Alias for db_file for convenience."""
        return self.db_file


@dataclass
class LoggingConfig:
    show_progress: bool = True
    verbose: bool = False


@dataclass
class CacheConfig:
    limit: Optional[int] = None
    order: str = "newest"
    parallel_workers: int = 1  # Default worker count for auto-detection


@dataclass
class ExecutorConfig:
    default_run_scope: str = "all"
    dry_run: bool = False
    strict: bool = False
    limit: Optional[int] = None
    verify_moves: bool = False
    parallel_workers: Optional[int] = 0  # None=auto-detect, 0=sequential, N>0=parallel with N workers
    max_retries: int = 2  # Number of retry attempts for parallel execution
    retry_delay_base: float = 5.0  # Initial retry delay in seconds


@dataclass
class AppConfig:
    paths: PathsConfig
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)


def build_default_config(
    base_dir: Optional[Path] = None,
    cache_override: Optional[Path] = None
) -> AppConfig:
    """
    Return the default configuration for the application.

    Args:
        base_dir: Base directory for application paths
        cache_override: Optional path to cache database (overrides default)

    Returns:
        AppConfig with resolved paths
    """
    resolved = Path(base_dir).resolve() if base_dir else DEFAULT_BASE_DIR
    cfg = AppConfig(paths=PathsConfig(base_dir=resolved))

    # Override cache path if specified
    if cache_override:
        cfg.paths.db_file = Path(cache_override).resolve()

    return cfg
