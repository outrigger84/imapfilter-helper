"""Execute queued actions.

Package split of the former single-module core/executor.py. The public API
is unchanged: import execute_actions and friends from core.executor.
"""
from core.executor.conflicts import resolve_pending_conflicts
from core.executor.serial import execute_actions
from core.executor.parallel import (
    _count_unique_source_folders,
    execute_actions_parallel,
    should_use_parallel_mode,
)

__all__ = [
    "execute_actions",
    "execute_actions_parallel",
    "resolve_pending_conflicts",
    "should_use_parallel_mode",
]
