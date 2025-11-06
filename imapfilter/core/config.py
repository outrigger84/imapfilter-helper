"""Configuration helpers for imapfilter helper tools."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Dict

BASE_DIR = Path(__file__).resolve().parents[2]

CONFIG_DEFAULTS = {
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

__all__ = [
    "BASE_DIR",
    "CONFIG_DEFAULTS",
    "get_paths",
    "load_config",
    "resolve_path",
]


def _merge_dict(base: MutableMapping[str, Any], updates: Mapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in updates.items():
        if (
            key in base
            and isinstance(base[key], Mapping)
            and isinstance(value, Mapping)
        ):
            _merge_dict(base[key], value)
        else:
            base[key] = value
    return base


def load_config(overrides: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """Return configuration merged with optional overrides."""
    config = deepcopy(CONFIG_DEFAULTS)
    if overrides:
        _merge_dict(config, overrides)
    return config


def get_paths(config: Mapping[str, Any] | None = None) -> Dict[str, Path]:
    """Return resolved path mappings from the provided configuration."""
    cfg = config or CONFIG_DEFAULTS
    paths = cfg.get("paths", {})
    return {name: Path(value) for name, value in paths.items()}


def resolve_path(name: str, config: Mapping[str, Any] | None = None) -> Path:
    """Resolve a single configured path to a :class:`~pathlib.Path`."""
    paths = get_paths(config)
    if name not in paths:
        available = ", ".join(sorted(paths))
        raise KeyError(f"Unknown path '{name}'. Available paths: {available}")
    return paths[name]
