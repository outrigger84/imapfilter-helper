"""Configuration helpers for the IMAP filter helper."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, MutableMapping

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = PACKAGE_ROOT.parent

CONFIG_DEFAULTS: Dict[str, Any] = {
    "paths": {
        "rules_dir": str(BASE_DIR / "rules"),
        "secrets_file": str(BASE_DIR / "secrets.json"),
        "db_file": str(BASE_DIR / "cache.db"),
        "log_file": str(BASE_DIR / "imapfilter-helper.log"),
    },
    "logging": {"show_progress": True},
    "executor": {
        "default_run_scope": "inbox",
        "dry_run": False,
        "strict": False,
    },
}

_ACTIVE_CONFIG: MutableMapping[str, Any] | None = None


def get_default_config() -> Dict[str, Any]:
    """Return a deep copy of the default configuration."""
    return deepcopy(CONFIG_DEFAULTS)


def set_active_config(config: MutableMapping[str, Any]) -> None:
    """Store the provided configuration for runtime access."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = config


def get_active_config() -> MutableMapping[str, Any]:
    """Return the configuration currently in use."""
    if _ACTIVE_CONFIG is not None:
        return _ACTIVE_CONFIG
    return get_default_config()


def _resolve_path(path_value: str | Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        return (BASE_DIR / path).resolve()
    return path


def get_log_path(config: MutableMapping[str, Any] | None = None) -> Path:
    """Return the path to the log file, ensuring it is absolute."""
    cfg = config or get_active_config()
    paths = cfg.get("paths", {})
    log_path = paths.get("log_file")
    if not log_path:
        return BASE_DIR / "imapfilter-helper.log"
    return _resolve_path(log_path)


__all__ = [
    "BASE_DIR",
    "CONFIG_DEFAULTS",
    "get_active_config",
    "get_default_config",
    "get_log_path",
    "set_active_config",
]
