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
        return types.SimpleNamespace(elapsed=0.0), len(folders), 0

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
        return types.SimpleNamespace(elapsed=0.0), len(folders), 0

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fake_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == ["INBOX", "Archive"]
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
    assert set(seen["folders"]) == {"Archive/2024", "Archive/2023"}


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
        dry_run=False,
        strict=True,
        all_folders=True,
        folder=None,
        verbose=False,
        debug_headers=False,
        action_limit=None,
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
    assert seen["evaluate_called"] is True
    assert seen["execute_called"] is True
    assert seen["evaluate_kwargs"]["verbose"] is False
    assert seen["evaluate_kwargs"]["debug_headers"] is False
    assert seen["evaluate_kwargs"]["scope"] == "all"
    assert seen["evaluate_kwargs"]["folders"] is None
    assert seen["execute_kwargs"]["verbose"] is False
    assert seen["execute_kwargs"]["limit"] is None
    assert seen["execute_kwargs"]["folders"] is None


def test_handle_run_all_dry_run_skips_login(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=True,
        strict=False,
        all_folders=True,
        folder=None,
        verbose=True,
        debug_headers=False,
        action_limit=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
    )
    seen = {}

    def fail_login(*_args, **_kwargs):  # pragma: no cover - defensive
        raise AssertionError("imap_login should not be called for dry-run")

    def fake_list_all_folders(_client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called without client")

    def fake_build_cache(client, database, folders, **kwargs):
        assert client is None
        seen["cache_folders"] = list(folders)
        seen["cache_kwargs"] = kwargs
        return None, len(folders), 0

    def fake_load_rules(path, log):
        seen["rules_dir"] = path
        return [{"name": "noop", "conditions": []}]

    def fake_evaluate_rules(database, rules, **kwargs):
        seen["evaluate_called"] = True
        seen["evaluate_kwargs"] = kwargs
        return None, len(rules), 0

    def fake_execute_actions(client, database, **kwargs):
        assert client is None
        seen["execute_called"] = True
        seen["execute_kwargs"] = kwargs
        return None, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    monkeypatch.setattr(cli, "imap_login", fail_login)
    monkeypatch.setattr(cli, "list_all_folders", fake_list_all_folders)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_run_all(args, cfg, db, logger)

    assert result == 0
    assert seen["cache_folders"] == ["INBOX"]
    assert seen["cache_kwargs"]["limit"] is None
    assert seen["cache_kwargs"]["order"] == "newest"
    assert seen["evaluate_called"] is True
    assert seen["execute_called"] is True
    assert seen["evaluate_kwargs"]["verbose"] is True
    assert seen["execute_kwargs"]["dry_run"] is True


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
        return types.SimpleNamespace(elapsed=0.0), len(folders), 0

    def fail_list_all_folders(client):  # pragma: no cover - defensive
        raise AssertionError("list_all_folders should not be called")

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "list_all_folders", fail_list_all_folders)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["folders"] == ["Archive/2024"]
    assert seen["order"] == "random"


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
        action_limit=10,
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


def test_handle_compact_cache(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(cmd="compact-cache")
    seen = {}

    def fake_compact_cache(database, **kwargs):
        seen["db"] = database
        seen["logger"] = kwargs.get("logger")
        return None, 2, 2

    monkeypatch.setattr(cli, "compact_cache", fake_compact_cache)

    result = cli.handle_compact_cache(args, cfg, db, logger)

    assert result == 0
    assert seen["db"] is db
    assert seen["logger"] is logger


def test_parser_accepts_per_folder_on_phase_commands():
    parser = cli.build_parser()
    for cmd in ["build-cache", "evaluate", "execute", "run-all", "eval-execute"]:
        parsed = parser.parse_args([cmd, "--per-folder"])
        assert parsed.per_folder is True


def test_parser_rejects_per_folder_on_stream():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["stream", "--per-folder"])


def test_handle_build_cache_per_folder(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="build-cache",
        all_folders=False,
        folder=["Beta", "Alpha"],
        limit=None,
        order=None,
        backup=False,
        per_folder=True,
    )
    calls = []

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        return FakeClient()

    def fake_build_cache(client, database, folders, **kwargs):
        calls.append(list(folders))
        return types.SimpleNamespace(elapsed=0.0), len(folders), 1

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)

    result = cli.handle_build_cache(args, cfg, db, logger)

    assert result == 0
    assert calls == [["Alpha"], ["Beta"]]


def test_handle_evaluate_per_folder(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="evaluate",
        dry_run=True,
        verbose=False,
        debug_headers=False,
        all_folders=False,
        folder=["Beta", "Alpha"],
        limit=None,
        per_folder=True,
    )
    calls = []
    load_calls = []

    def fake_load_rules(path, log):
        load_calls.append(path)
        return []

    def fake_evaluate_rules(database, rules, **kwargs):
        calls.append((list(kwargs["folders"]), kwargs["scope"]))
        return None, 0, 0

    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)

    result = cli.handle_evaluate(args, cfg, db, logger)

    assert result == 0
    assert calls == [(["Alpha"], "all"), (["Beta"], "all")]
    assert len(load_calls) == 1


def test_handle_execute_per_folder_aggregates_stats(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="execute",
        dry_run=True,
        strict=False,
        verbose=False,
        limit=None,
        all_folders=False,
        folder=["Beta", "Alpha"],
        per_folder=True,
    )
    calls = []
    records = []
    orig_log = logger.log

    def spy_log(level, message, context=None, console=None):
        records.append((message, context))
        return orig_log(level, message, context, console=console)

    def fake_execute_actions(client, database, **kwargs):
        folder = kwargs["folders"][0]
        calls.append(folder)
        stats = {"Alpha": {"done": 1, "failed": 0, "skipped": 2}, "Beta": {"done": 2, "failed": 1, "skipped": 0}}
        return None, stats[folder]

    monkeypatch.setattr(logger, "log", spy_log)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_execute(args, cfg, db, logger)

    assert result == 0
    assert calls == ["Alpha", "Beta"]
    summary = next(ctx for message, ctx in records if message == "execute_summary")
    assert summary["done"] == 3
    assert summary["failed"] == 1
    assert summary["skipped"] == 2


def test_handle_execute_per_folder_respects_global_limit(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="execute",
        dry_run=False,
        strict=False,
        verbose=False,
        limit=1,
        all_folders=False,
        folder=["Beta", "Alpha"],
        per_folder=True,
    )
    calls = []

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        return FakeClient()

    def fake_execute_actions(client, database, **kwargs):
        calls.append((kwargs["folders"][0], kwargs["limit"]))
        return None, {"done": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_execute(args, cfg, db, logger)

    assert result == 0
    assert calls == [("Alpha", 1)]


def test_handle_execute_per_folder_dry_run_limit_counts_pending(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="execute",
        dry_run=True,
        strict=False,
        verbose=False,
        limit=2,
        all_folders=False,
        folder=["Beta", "Alpha"],
        per_folder=True,
    )
    db.executemany(
        "INSERT INTO actions (uid, folder, rule_name, target, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("1", "Alpha", "rule", "Junk", 100, "pending", "2024-01-01T00:00:00Z"),
            ("2", "Alpha", "rule", "Junk", 100, "pending", "2024-01-01T00:00:00Z"),
            ("3", "Beta", "rule", "Junk", 100, "pending", "2024-01-01T00:00:00Z"),
        ],
    )
    db.commit()
    calls = []

    def fake_execute_actions(client, database, **kwargs):
        calls.append((kwargs["folders"][0], kwargs["limit"]))
        # Dry-run leaves actions pending and reports zero stats
        return None, {"done": 0, "failed": 0, "skipped": 0}

    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_execute(args, cfg, db, logger)

    assert result == 0
    # Alpha's 2 pending actions exhaust the global limit; Beta is never reached
    assert calls == [("Alpha", 2)]


def test_handle_run_all_per_folder_interleaves(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=False,
        strict=False,
        all_folders=True,
        folder=None,
        verbose=False,
        debug_headers=False,
        action_limit=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
        per_folder=True,
    )
    calls = []
    logins = []

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        logins.append(path)
        return FakeClient()

    def fake_list_all_folders(_client):
        return ["Beta", "Alpha"]

    def fake_build_cache(client, database, folders, **kwargs):
        calls.append(f"build:{folders[0]}")
        return None, 1, 1

    def fake_load_rules(path, log):
        return [{"name": "noop", "conditions": []}]

    def fake_evaluate_rules(database, rules, **kwargs):
        calls.append(f"eval:{kwargs['folders'][0]}")
        return None, len(rules), 0

    def fake_execute_actions(client, database, **kwargs):
        calls.append(f"exec:{kwargs['folders'][0]}")
        return None, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "list_all_folders", fake_list_all_folders)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_run_all(args, cfg, db, logger)

    assert result == 0
    assert calls == [
        "build:Alpha",
        "eval:Alpha",
        "exec:Alpha",
        "build:Beta",
        "eval:Beta",
        "exec:Beta",
    ]
    assert len(logins) == 1


def test_handle_run_all_per_folder_continues_on_error(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="run-all",
        dry_run=False,
        strict=False,
        all_folders=False,
        folder=["Beta", "Alpha"],
        verbose=False,
        debug_headers=False,
        action_limit=None,
        cache_limit=None,
        cache_order=None,
        backup=False,
        per_folder=True,
    )
    calls = []

    class FakeClient:
        def logout(self) -> None:
            return None

    def fake_login(path, log):
        return FakeClient()

    def fake_build_cache(client, database, folders, **kwargs):
        calls.append(f"build:{folders[0]}")
        return None, 1, 1

    def fake_load_rules(path, log):
        return []

    def fake_evaluate_rules(database, rules, **kwargs):
        folder = kwargs["folders"][0]
        calls.append(f"eval:{folder}")
        if folder == "Alpha":
            raise RuntimeError("boom")
        return None, 0, 0

    def fake_execute_actions(client, database, **kwargs):
        calls.append(f"exec:{kwargs['folders'][0]}")
        return None, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    monkeypatch.setattr(cli, "imap_login", fake_login)
    monkeypatch.setattr(cli, "build_cache", fake_build_cache)
    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_run_all(args, cfg, db, logger)

    assert result == 1
    assert calls == [
        "build:Alpha",
        "eval:Alpha",
        "build:Beta",
        "eval:Beta",
        "exec:Beta",
    ]


def test_handle_eval_execute_per_folder_interleaves(monkeypatch, cli_context):
    cfg, db, logger = cli_context
    args = argparse.Namespace(
        cmd="eval-execute",
        dry_run=True,
        strict=False,
        verbose=False,
        debug_headers=False,
        limit=None,
        all_folders=True,
        folder=None,
        per_folder=True,
    )
    db.executemany(
        "INSERT INTO folders (name, parent, updated_at) VALUES (?, ?, ?)",
        [
            ("Beta", None, "2024-01-01T00:00:00Z"),
            ("Alpha", None, "2024-01-01T00:00:00Z"),
        ],
    )
    db.commit()
    calls = []

    def fake_load_rules(path, log):
        return []

    def fake_evaluate_rules(database, rules, **kwargs):
        calls.append(f"eval:{kwargs['folders'][0]}")
        return None, 0, 0

    def fake_execute_actions(client, database, **kwargs):
        calls.append(f"exec:{kwargs['folders'][0]}")
        return None, {"done": 0, "skipped": 0, "failed": 0, "suppressed": 0}

    monkeypatch.setattr(cli, "load_rules", fake_load_rules)
    monkeypatch.setattr(cli, "evaluate_rules", fake_evaluate_rules)
    monkeypatch.setattr(cli, "execute_actions", fake_execute_actions)

    result = cli.handle_eval_execute(args, cfg, db, logger)

    assert result == 0
    assert calls == ["eval:Alpha", "exec:Alpha", "eval:Beta", "exec:Beta"]
