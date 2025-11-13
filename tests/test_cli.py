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
    args = argparse.Namespace(
        cmd="build-cache",
        all_folders=False,
        folder=None,
        limit=None,
        order=None,
        backup=False,
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

    def fake_build_cache(client, database, folders, **kwargs):
        seen["folders"] = list(folders)
        seen["limit"] = kwargs.get("limit")
        seen["order"] = kwargs.get("order")
        seen["backup_enabled"] = kwargs.get("backup_enabled")
        seen["backup_dir"] = kwargs.get("backup_dir")
        return None, len(folders), 0

    def fail_list_all_folders(client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called")

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fail_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == [cli.DEFAULT_INBOX]
    assert seen["limit"] is None
    assert seen["order"] == "newest"
    assert seen["backup_enabled"] is False
    assert seen["backup_dir"] == cfg.paths.backup_dir
    assert fake_client.logged_out is True


def test_handle_build_cache_enables_backup(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="build-cache",
        all_folders=True,
        folder=None,
        limit=None,
        order=None,
        backup=True,
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
        seen["folders"] = list(folders)
        seen["backup_enabled"] = kwargs.get("backup_enabled")
        seen["backup_dir"] = kwargs.get("backup_dir")
        return None, len(folders), 0

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fake_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == ["INBOX", "Archive"]
    assert seen["backup_enabled"] is True
    assert seen["backup_dir"] == cfg.paths.backup_dir
    assert fake_client.logged_out is True


def test_handle_evaluate_sets_dry_run(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="evaluate",
        dry_run=True,
        verbose=False,
        debug_headers=False,
        all_folders=False,
        folder=None,
        limit=None,
        order=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
    )
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
    assert seen["folders"] is None


def test_handle_evaluate_with_specific_folders(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="evaluate",
        dry_run=False,
        verbose=True,
        debug_headers=False,
        all_folders=False,
        folder=["Archive/2024", "Archive/2023"],
        limit=None,
        order=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
    )
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
    assert seen["verbose"] is True
    assert seen["scope"] == "all"
    assert seen["folders"] == ["Archive/2024", "Archive/2023"]


def test_handle_execute_dry_run(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="execute",
        dry_run=True,
        strict=False,
        verbose=False,
        limit=5,
        all_folders=False,
        folder=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
    )
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
    assert seen["folders"] is None


def test_handle_execute_with_folder(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="execute",
        dry_run=False,
        strict=True,
        verbose=True,
        limit=None,
        all_folders=False,
        folder=["Projects/Archive"],
        cache_limit=None,
        cache_order=None,
        backup=False,
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

    def fake_execute_actions(client, database, **kwargs):
        seen.update(kwargs)
        assert client is fake_client
        return None, {"done": 0}

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_execute(args, cfg, db, logger)

    assert result == 0
    assert fake_client.logged_out is True
    assert seen["folders"] == ["Projects/Archive"]
    assert seen["strict"] is True


def test_handle_run_all_summarises(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=True,
        strict=True,
        all_folders=True,
        folder=None,
        verbose=False,
        debug_headers=False,
        limit=None,
        cache_limit=2,
        cache_order="oldest",
        backup=True,
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
        seen["cache_limit"] = kwargs.get("limit")
        seen["cache_order"] = kwargs.get("order")
        seen["cache_backup"] = kwargs.get("backup_enabled")
        seen["cache_backup_dir"] = kwargs.get("backup_dir")
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
    assert seen["cache_limit"] == 2
    assert seen["cache_order"] == "oldest"
    assert seen["cache_backup"] is True
    assert seen["cache_backup_dir"] == cfg.paths.backup_dir
    assert seen["evaluate_called"] is True
    assert seen["execute_called"] is True
    assert seen["evaluate_kwargs"]["verbose"] is False
    assert seen["evaluate_kwargs"]["debug_headers"] is False
    assert seen["evaluate_kwargs"]["scope"] == "all"
    assert seen["evaluate_kwargs"]["folders"] is None
    assert seen["execute_kwargs"]["verbose"] is False
    assert seen["execute_kwargs"]["limit"] is None
    assert seen["execute_kwargs"]["folders"] is None


def test_handle_build_cache_uses_specific_folder(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="build-cache", all_folders=False, folder="Archive/2024")
    args.limit = None
    args.order = "random"
    args.backup = False
    seen = {}

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        seen["secrets_path"] = path
        return FakeClient()

    def fake_build_cache(client, database, folders, **kwargs):
        seen["folders"] = list(folders)
        seen["order"] = kwargs.get("order")
        seen["backup_enabled"] = kwargs.get("backup_enabled")
        return None, len(folders), 0

    def fail_list_all_folders(client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called")

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fail_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == ["Archive/2024"]
    assert seen["order"] == "random"
    assert seen["backup_enabled"] is False


def test_handle_run_all_uses_specific_folder(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=False,
        strict=False,
        all_folders=False,
        folder=["Archive/2024"],
        verbose=False,
        debug_headers=False,
        limit=10,
        cache_limit=None,
        cache_order=None,
        backup=False,
    )
    seen = {}

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        seen["secrets_path"] = path
        return FakeClient()

    def fake_build_cache(client, database, folders, **kwargs):
        seen["cache_folders"] = list(folders)
        seen["cache_kwargs"] = kwargs
        return None, len(folders), 1

    def fake_load_rules(path, log):
        return []

    def fake_evaluate_rules(database, rules, **kwargs):
        seen["evaluate_kwargs"] = kwargs
        return None, 0, 0

    def fake_execute_actions(client, database, **kwargs):
        seen["execute_kwargs"] = kwargs
        return None, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    def fail_list_all_folders(client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called")

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "list_all_folders", fail_list_all_folders)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_run_all(args, cfg, db, logger)

    assert result == 0
    assert seen["cache_folders"] == ["Archive/2024"]
    assert seen["cache_kwargs"]["limit"] is None
    assert seen["cache_kwargs"]["order"] == "newest"
    assert seen["cache_kwargs"]["backup_enabled"] is False
    assert seen["cache_kwargs"]["backup_dir"] == cfg.paths.backup_dir
    assert seen["evaluate_kwargs"]["verbose"] is False
    assert seen["execute_kwargs"]["limit"] == 10
    assert seen["evaluate_kwargs"]["folders"] == ["Archive/2024"]
    assert seen["evaluate_kwargs"]["scope"] == "all"
    assert seen["execute_kwargs"]["folders"] == ["Archive/2024"]


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


def test_handle_clear_cache(cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="clear-cache")

    db.execute(
        "INSERT INTO folders (name, parent, updated_at) VALUES (?, ?, ?)",
        ("Archive", None, "2024-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO headers (uid, folder, data, updated_at) VALUES (?, ?, ?, ?)",
        ("1", "Archive", "{}", "2024-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("1", "Archive", "rule", "INBOX", 100, "pending", "2024-01-01T00:00:00Z"),
    )
    db.commit()

    result = cli.handle_clear_cache(args, cfg, db, logger)

    assert result == 0

    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM headers")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT COUNT(*) FROM actions")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT COUNT(*) FROM folders")
    assert cur.fetchone()[0] == 0
