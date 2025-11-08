from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "tqdm" not in sys.modules:  # pragma: no cover - test support
    tqdm_stub = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **_kwargs):
            self._iterable = list(iterable or [])

        def __iter__(self):
            return iter(self._iterable)

        def set_postfix_str(self, *_args, **_kwargs):
            return None

        def update(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    def _write(*_args, **_kwargs):
        return None

    def _tqdm(iterable=None, **kwargs):
        return _DummyTqdm(iterable, **kwargs)

    _tqdm.write = _write  # type: ignore[attr-defined]

    tqdm_stub.tqdm = _tqdm
    tqdm_stub.write = _write
    sys.modules["tqdm"] = tqdm_stub

from core import cli
from core.config import build_default_config
from core.database import init_db
from core.logging_utils import JsonLogger


@pytest.fixture()
def cli_context(tmp_path: Path):
    cfg = build_default_config(tmp_path)
    cli._ensure_layout(cfg)
    logger = JsonLogger(cfg.paths.log_file)
    db = init_db(cfg.paths.db_file, logger=logger)
    try:
        yield cfg, db, logger
    finally:
        db.close()


def test_handle_build_cache_uses_default_inbox(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="build-cache", all_folders=False)
    seen = {}

    class FakeClient:
        def __init__(self) -> None:
            self.logged_out = False

        def logout(self) -> None:
            self.logged_out = True

    fake_client = FakeClient()

    def fake_login(path, log):
        seen["secrets_path"] = path
        return fake_client

    def fake_build_cache(client, database, folders, **kwargs):
        seen["folders"] = list(folders)
        return None, len(folders), 0

    def fail_list_all_folders(client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called")

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fail_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == [cli.DEFAULT_INBOX]
    assert fake_client.logged_out is True


def test_handle_evaluate_sets_dry_run(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="evaluate", dry_run=True, verbose=False, debug_headers=False)
    seen = {}

    def fake_load_rules(path, log):
        seen["rules_dir"] = path
        return []

    def fake_evaluate_rules(database, rules, **kwargs):
        seen.update(kwargs)
        return None, 0, 0

    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)

    result = cli.handle_evaluate(args, cfg, db, logger)

    assert result == 0
    assert cfg.executor.dry_run is True
    assert seen["dry_run"] is True
    assert seen["scope"] == cfg.executor.default_run_scope
    assert seen["verbose"] is False
    assert seen["debug_headers"] is False


def test_handle_execute_dry_run(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="execute", dry_run=True, strict=False, verbose=False, limit=5)
    seen = {}

    def fake_execute_actions(client, database, **kwargs):
        seen.update(kwargs)
        assert client is None
        return None, {"done": 0}

    def fail_login(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("imap_login should not be called in dry-run")

    monkeypatch.setattr(cli, "imap_login", fail_login)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_execute(args, cfg, db, logger)

    assert result == 0
    assert cfg.executor.dry_run is True
    assert seen["dry_run"] is True
    assert seen["strict"] is False
    assert seen["verbose"] is False
    assert seen["limit"] == 5


def test_handle_run_all_summarises(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=True,
        strict=True,
        all_folders=True,
        verbose=False,
        debug_headers=False,
        limit=None,
    )
    seen = {}

    class FakeClient:
        def __init__(self) -> None:
            self.logged_out = False

        def logout(self) -> None:
            self.logged_out = True

    fake_client = FakeClient()

    def fake_login(path, log):
        seen["secrets_path"] = path
        return fake_client

    def fake_list_all_folders(_client):
        return ["INBOX", "Archive"]

    def fake_build_cache(client, database, folders, **kwargs):
        seen["cache_folders"] = list(folders)
        return None, len(folders), 2

    def fake_load_rules(path, log):
        seen["rules_dir"] = path
        return [{"name": "noop", "conditions": []}]

    def fake_evaluate_rules(database, rules, **kwargs):
        seen["evaluate_called"] = True
        seen["evaluate_kwargs"] = kwargs
        return None, len(rules), 1

    def fake_execute_actions(client, database, **kwargs):
        seen["execute_called"] = True
        seen["execute_kwargs"] = kwargs
        return None, {"done": 1, "skipped": 0, "failed": 0, "suppressed": 0}

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "list_all_folders", fake_list_all_folders)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_run_all(args, cfg, db, logger)

    assert result == 0
    assert fake_client.logged_out is True
    assert seen["cache_folders"] == ["INBOX", "Archive"]
    assert seen["evaluate_called"] is True
    assert seen["execute_called"] is True
    assert seen["evaluate_kwargs"]["verbose"] is False
    assert seen["evaluate_kwargs"]["debug_headers"] is False
    assert seen["execute_kwargs"]["verbose"] is False
    assert seen["execute_kwargs"]["limit"] is None


def test_handle_clear_pending_removes_actions(cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="clear-pending")

    db.executemany(
        "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("uid-1", "INBOX", "rule-a", "Archive", 100, "pending", "2024-01-01T00:00:00Z"),
            ("uid-2", "INBOX", "rule-b", "Archive", 100, "pending", "2024-01-02T00:00:00Z"),
            ("uid-3", "INBOX", "rule-c", "Archive", 100, "done", "2024-01-03T00:00:00Z"),
        ],
    )
    db.commit()

    result = cli.handle_clear_pending(args, cfg, db, logger)

    assert result == 0

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM actions WHERE status='pending'")
    (remaining,) = cur.fetchone()
    assert remaining == 0

    cur.execute("SELECT COUNT(*) FROM actions WHERE status!='pending'")
    (others,) = cur.fetchone()
    assert others == 1
