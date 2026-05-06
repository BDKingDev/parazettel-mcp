"""Tests for the CLI entrypoint."""

import argparse
import shutil
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock

import pytest

import parazettel_mcp.main as main_module
from parazettel_mcp.config import config


@pytest.fixture
def workspace_temp_dir():
    """Create a writable temp directory inside the repo workspace."""
    base_dir = Path(".tmp") / "test-main" / str(uuid4())
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield base_dir
    finally:
        shutil.rmtree(base_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def restore_config():
    """Restore global config mutations made by main/update_config tests."""
    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    yield
    config.notes_dir = original_notes_dir
    config.graph_db_path = original_graph_db_path


def test_parse_args_reads_env_defaults(monkeypatch):
    """parse_args should use Parazettel env vars as defaults."""
    monkeypatch.setenv("PARAZETTEL_NOTES_DIR", "env-notes")
    monkeypatch.setenv("PARAZETTEL_GRAPH_DB_PATH", "env-graph.kuzu")
    monkeypatch.setenv("PARAZETTEL_DATABASE_PATH", "env-legacy.db")
    monkeypatch.setenv("PARAZETTEL_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(sys, "argv", ["parazettel"])

    args = main_module.parse_args()

    assert args.notes_dir == "env-notes"
    assert args.graph_db_path == "env-graph.kuzu"
    assert args.database_path == "env-legacy.db"
    assert args.log_level == "DEBUG"


def test_update_config_updates_paths():
    """update_config should rewrite the global notes/graph_db paths."""
    args = argparse.Namespace(
        notes_dir="custom-notes",
        graph_db_path="custom-graph.kuzu",
        database_path=None,
        log_level="INFO",
    )

    main_module.update_config(args)

    assert str(config.notes_dir) == "custom-notes"
    assert str(config.graph_db_path) == "custom-graph.kuzu"


def test_update_config_accepts_legacy_database_path_alias():
    """update_config should translate legacy --database-path values to graph.kuzu."""
    args = argparse.Namespace(
        notes_dir=None,
        graph_db_path=None,
        database_path="custom-db/parazettel.db",
        log_level="INFO",
    )

    main_module.update_config(args)

    assert str(config.graph_db_path).replace("\\", "/").endswith(
        "custom-db/graph.kuzu"
    )


def test_main_initializes_and_runs_server(monkeypatch, workspace_temp_dir):
    """main should set up logging, initialize the graph DB, and run the server."""
    notes_dir = workspace_temp_dir / "notes"
    graph_db_path = workspace_temp_dir / "db" / "test_graph.kuzu"
    args = argparse.Namespace(
        notes_dir=str(notes_dir),
        graph_db_path=str(graph_db_path),
        log_level="DEBUG",
    )
    server = MagicMock()
    setup_logging = MagicMock()
    server_factory = MagicMock(return_value=server)

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ZettelkastenMcpServer", server_factory)

    main_module.main()

    assert notes_dir.exists()
    setup_logging.assert_called_once_with("DEBUG")
    server_factory.assert_called_once_with()
    server.run.assert_called_once_with()
    server.close.assert_called_once_with()


def test_main_exits_when_server_creation_fails(monkeypatch, workspace_temp_dir):
    """main should exit with code 1 when server creation fails."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        log_level="INFO",
    )
    setup_logging = MagicMock()
    server_factory = MagicMock(side_effect=RuntimeError("server init failed"))

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ZettelkastenMcpServer", server_factory)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 1
    setup_logging.assert_called_once_with("INFO")
    server_factory.assert_called_once_with()


def test_main_closes_server_when_run_fails(monkeypatch, workspace_temp_dir):
    """main should close the server on runtime failures."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        log_level="WARNING",
    )
    server = MagicMock()
    server.run.side_effect = RuntimeError("run failed")
    setup_logging = MagicMock()
    server_factory = MagicMock(return_value=server)

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ZettelkastenMcpServer", server_factory)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 1
    server.run.assert_called_once_with()
    server.close.assert_called_once_with()
