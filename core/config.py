"""Configuration helpers for the IMAPFilter helper."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Default filesystem layout -------------------------------------------------

DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = Path("data")
DEFAULT_RULES_PATH = Path("rules")
DEFAULT_SECRETS_PATH = DEFAULT_DATA_DIR / "secrets.json"
DEFAULT_CACHE_PATH = DEFAULT_DATA_DIR / "cache.db"
DEFAULT_LOG_PATH = DEFAULT_DATA_DIR / "imapfilter-helper.log"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIR / "config.json"


# Keyword Configuration ------------------------------------------------------


@dataclass
class KeywordConfig:
    """Configuration for IMAP keywords and flags."""

    predefined_keywords: List[Dict[str, str]] = field(default_factory=list)
    age_presets: List[Dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _get_default_keywords() -> List[Dict[str, str]]:
        """Return default keyword configuration."""
        return [
            {"name": "newsletter", "type": "custom", "description": "Marketing emails"},
            {"name": "work", "type": "custom", "description": "Work-related emails"},
            {"name": "receipts", "type": "custom", "description": "Purchase receipts"},
            {"name": r"\Seen", "type": "system", "description": "Message has been read"},
            {"name": r"\Flagged", "type": "system", "description": "Message is flagged"},
            {"name": r"\Answered", "type": "system", "description": "Message has been answered"},
            {"name": r"\Deleted", "type": "system", "description": "Marked for deletion"},
            {"name": r"\Draft", "type": "system", "description": "Draft message"},
        ]

    @staticmethod
    def _get_default_age_presets() -> List[Dict[str, Any]]:
        """Return default age preset configuration."""
        return [
            {"label": "7 days", "days": 7},
            {"label": "30 days", "days": 30},
            {"label": "90 days", "days": 90},
            {"label": "365 days (1 year)", "days": 365},
        ]

    @classmethod
    def load_from_file(cls, config_path: Path) -> "KeywordConfig":
        """
        Load keyword configuration from JSON file.

        Falls back to defaults if file doesn't exist or is invalid.

        Args:
            config_path: Path to config.json file

        Returns:
            KeywordConfig instance with loaded or default values
        """
        try:
            if not config_path.exists():
                # Return defaults if file doesn't exist
                return cls(
                    predefined_keywords=cls._get_default_keywords(),
                    age_presets=cls._get_default_age_presets(),
                )

            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            keywords = data.get("keywords", {}).get("predefined", [])
            age_presets = data.get("age_presets", [])

            # Use defaults if data is missing or empty
            if not keywords:
                keywords = cls._get_default_keywords()
            if not age_presets:
                age_presets = cls._get_default_age_presets()

            return cls(predefined_keywords=keywords, age_presets=age_presets)

        except (json.JSONDecodeError, IOError, KeyError) as e:
            # Fall back to defaults on any error
            print(f"Warning: Could not load config from {config_path}: {e}")
            print("Using default configuration.")
            return cls(
                predefined_keywords=cls._get_default_keywords(),
                age_presets=cls._get_default_age_presets(),
            )

    def get_system_flags(self) -> List[str]:
        """
        Get list of system flag names (those starting with backslash).

        Returns:
            List of system flag names (e.g., ['\\Seen', '\\Flagged'])
        """
        return [
            kw["name"]
            for kw in self.predefined_keywords
            if kw.get("type") == "system" and kw["name"].startswith("\\")
        ]

    def get_custom_keywords(self) -> List[str]:
        """
        Get list of custom keyword names (non-system keywords).

        Returns:
            List of custom keyword names (e.g., ['newsletter', 'work'])
        """
        return [
            kw["name"]
            for kw in self.predefined_keywords
            if kw.get("type") == "custom"
        ]

    def get_all_keywords(self) -> List[str]:
        """
        Get list of all keyword names (both system and custom).

        Returns:
            List of all keyword names
        """
        return [kw["name"] for kw in self.predefined_keywords]

    def validate_keyword(self, keyword: str) -> Tuple[bool, str]:
        """
        Validate keyword format and name.

        IMAP keywords must follow these rules:
        - Custom keywords: alphanumeric, may contain hyphens/underscores
        - System flags: must start with backslash (\\)
        - Cannot be empty
        - Cannot contain spaces

        Args:
            keyword: Keyword name to validate

        Returns:
            Tuple of (is_valid, error_message)
            If valid, error_message is empty string
        """
        if not keyword:
            return False, "Keyword cannot be empty"

        if " " in keyword:
            return False, "Keyword cannot contain spaces"

        # System flags
        if keyword.startswith("\\"):
            # Must be a known system flag
            valid_system_flags = [
                r"\Seen",
                r"\Answered",
                r"\Flagged",
                r"\Deleted",
                r"\Draft",
                r"\Recent",
            ]
            if keyword not in valid_system_flags:
                return (
                    False,
                    f"Unknown system flag. Valid flags: {', '.join(valid_system_flags)}",
                )
            return True, ""

        # Custom keywords - alphanumeric, hyphens, underscores only
        if not keyword.replace("-", "").replace("_", "").isalnum():
            return (
                False,
                "Custom keywords must contain only letters, numbers, hyphens, and underscores",
            )

        return True, ""


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
    config_file: Path = field(init=False)

    def __post_init__(self) -> None:
        base = Path(self.base_dir).resolve()
        self.base_dir = base
        self.data_dir = base / DEFAULT_DATA_DIR
        self.rules_dir = base / DEFAULT_RULES_PATH
        self.secrets_file = base / DEFAULT_SECRETS_PATH
        self.db_file = base / DEFAULT_CACHE_PATH
        self.log_file = base / DEFAULT_LOG_PATH
        self.backup_dir = self.data_dir / "backups"
        self.config_file = base / DEFAULT_CONFIG_PATH

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
    parallel_workers: int = 5  # Default worker count for auto-detection


@dataclass
class ExecutorConfig:
    default_run_scope: str = "all"
    dry_run: bool = False
    strict: bool = False
    limit: Optional[int] = None
    verify_moves: bool = False
    parallel_workers: Optional[int] = None  # None=auto-detect, 0=sequential, N>0=parallel with N workers
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
