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
from parazettel_mcp.daemon.client import DaemonUnavailableError


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
    original_server_transport = config.server_transport
    original_server_host = config.server_host
    original_server_port = config.server_port
    original_backend_mode = config.backend_mode
    original_daemon_host = config.daemon_host
    original_daemon_port = config.daemon_port
    yield
    config.notes_dir = original_notes_dir
    config.graph_db_path = original_graph_db_path
    config.server_transport = original_server_transport
    config.server_host = original_server_host
    config.server_port = original_server_port
    config.backend_mode = original_backend_mode
    config.daemon_host = original_daemon_host
    config.daemon_port = original_daemon_port


def test_parse_args_reads_env_defaults(monkeypatch):
    """parse_args should use Parazettel env vars as defaults."""
    monkeypatch.setenv("PARAZETTEL_NOTES_DIR", "env-notes")
    monkeypatch.setenv("PARAZETTEL_GRAPH_DB_PATH", "env-graph.kuzu")
    monkeypatch.setenv("PARAZETTEL_DATABASE_PATH", "env-legacy.db")
    monkeypatch.setenv("PARAZETTEL_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PARAZETTEL_MCP_TRANSPORT", "sse")
    monkeypatch.setenv("PARAZETTEL_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("PARAZETTEL_MCP_PORT", "9001")
    monkeypatch.setenv("PARAZETTEL_BACKEND_MODE", "daemon")
    monkeypatch.setenv("PARAZETTEL_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("PARAZETTEL_DAEMON_PORT", "9101")
    monkeypatch.setattr(sys, "argv", ["parazettel"])

    args = main_module.parse_args()

    assert args.notes_dir == "env-notes"
    assert args.graph_db_path == "env-graph.kuzu"
    assert args.database_path == "env-legacy.db"
    assert args.log_level == "DEBUG"
    assert args.transport == "sse"
    assert args.host == "0.0.0.0"
    assert args.port == 9001
    assert args.backend_mode == "daemon"
    assert args.daemon_host == "127.0.0.1"
    assert args.daemon_port == 9101


def test_update_config_updates_paths():
    """update_config should rewrite the global notes/graph_db paths."""
    args = argparse.Namespace(
        notes_dir="custom-notes",
        graph_db_path="custom-graph.kuzu",
        database_path=None,
        log_level="INFO",
        transport="sse",
        host="0.0.0.0",
        port=9100,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=9101,
    )

    main_module.update_config(args)

    assert str(config.notes_dir) == "custom-notes"
    assert str(config.graph_db_path) == "custom-graph.kuzu"
    assert config.server_transport == "sse"
    assert config.server_host == "0.0.0.0"
    assert config.server_port == 9100
    assert config.backend_mode == "daemon"
    assert config.daemon_host == "127.0.0.1"
    assert config.daemon_port == 9101


def test_update_config_accepts_legacy_database_path_alias():
    """update_config should translate legacy --database-path values to graph.kuzu."""
    args = argparse.Namespace(
        notes_dir=None,
        graph_db_path=None,
        database_path="custom-db/parazettel.db",
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="direct",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )

    main_module.update_config(args)

    assert str(config.graph_db_path).replace("\\", "/").endswith(
        "custom-db/graph.kuzu"
    )


def test_update_config_accepts_sse_transport_settings():
    """update_config should persist SSE transport host/port settings."""
    args = argparse.Namespace(
        notes_dir=None,
        graph_db_path=None,
        database_path=None,
        log_level="INFO",
        transport="sse",
        host="127.0.0.1",
        port=8766,
        backend_mode="direct",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8767,
    )

    main_module.update_config(args)

    assert config.server_transport == "sse"
    assert config.server_host == "127.0.0.1"
    assert config.server_port == 8766


def test_update_config_accepts_daemon_settings():
    """update_config should persist daemon backend host/port settings."""
    args = argparse.Namespace(
        notes_dir=None,
        graph_db_path=None,
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8768,
    )

    main_module.update_config(args)

    assert config.backend_mode == "daemon"
    assert config.daemon_host == "127.0.0.1"
    assert config.daemon_port == 8768


def test_main_initializes_and_runs_server(monkeypatch, workspace_temp_dir):
    """main should set up logging, initialize the graph DB, and run the server."""
    notes_dir = workspace_temp_dir / "notes"
    graph_db_path = workspace_temp_dir / "db" / "test_graph.kuzu"
    args = argparse.Namespace(
        notes_dir=str(notes_dir),
        graph_db_path=str(graph_db_path),
        log_level="DEBUG",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="direct",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
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
    server.run.assert_called_once_with("stdio")
    server.close.assert_called_once_with()


def test_main_exits_when_server_creation_fails(monkeypatch, workspace_temp_dir):
    """main should exit with code 1 when server creation fails."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="direct",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
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
        transport="sse",
        host="127.0.0.1",
        port=8765,
        backend_mode="direct",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
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
    server.run.assert_called_once_with("sse")
    server.close.assert_called_once_with()


def test_main_runs_daemon_when_requested(monkeypatch, workspace_temp_dir):
    """main should start the dedicated daemon instead of the MCP facade."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=True,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )
    setup_logging = MagicMock()
    daemon = MagicMock()
    daemon_factory = MagicMock(return_value=daemon)

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ParazettelDaemonServer", daemon_factory)

    main_module.main()

    daemon_factory.assert_called_once_with("127.0.0.1", 8766)
    daemon.serve_forever.assert_called_once_with()
    daemon.shutdown.assert_called_once_with()


def test_main_daemon_backend_autostarts_daemon_when_unavailable(
    monkeypatch, workspace_temp_dir
):
    """Daemon-backed MCP startup should launch the daemon if health is unavailable."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )
    setup_logging = MagicMock()
    server = MagicMock()
    server_factory = MagicMock(return_value=server)
    daemon_client = MagicMock()
    daemon_client.health.side_effect = [
        DaemonUnavailableError("down"),
        {"ok": True},
    ]
    popen = MagicMock()

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ZettelkastenMcpServer", server_factory)
    monkeypatch.setattr(main_module, "DaemonRpcClient", lambda base_url: daemon_client)
    monkeypatch.setattr(main_module, "_spawn_daemon_process", lambda _args: popen)
    monkeypatch.setattr(main_module.time, "sleep", lambda _: None)

    main_module.main()

    assert daemon_client.health.call_count == 2
    server_factory.assert_called_once_with()
    server.run.assert_called_once_with("stdio")
    server.close.assert_called_once_with()


def test_main_daemon_backend_skips_spawn_when_daemon_is_healthy(
    monkeypatch, workspace_temp_dir
):
    """Daemon-backed MCP startup should not spawn another daemon if health passes."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )
    setup_logging = MagicMock()
    server = MagicMock()
    server_factory = MagicMock(return_value=server)
    daemon_client = MagicMock()
    daemon_client.health.return_value = {"ok": True}
    spawn = MagicMock()

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "ZettelkastenMcpServer", server_factory)
    monkeypatch.setattr(main_module, "DaemonRpcClient", lambda base_url: daemon_client)
    monkeypatch.setattr(main_module, "_spawn_daemon_process", spawn)

    main_module.main()

    spawn.assert_not_called()
    daemon_client.health.assert_called_once_with()
    server.run.assert_called_once_with("stdio")


def test_main_exits_when_daemon_backend_cannot_start(
    monkeypatch, workspace_temp_dir
):
    """Daemon-backed MCP startup should fail clearly if the daemon never comes up."""
    args = argparse.Namespace(
        notes_dir=str(workspace_temp_dir / "notes"),
        graph_db_path=str(workspace_temp_dir / "db" / "test_graph.kuzu"),
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )
    setup_logging = MagicMock()
    daemon_client = MagicMock()
    daemon_client.health.side_effect = DaemonUnavailableError("down")
    spawn = MagicMock()
    ticks = iter([0.0, 0.0, 11.0])

    monkeypatch.setattr(main_module, "parse_args", lambda: args)
    monkeypatch.setattr(main_module, "setup_logging", setup_logging)
    monkeypatch.setattr(main_module, "DaemonRpcClient", lambda base_url: daemon_client)
    monkeypatch.setattr(main_module, "_spawn_daemon_process", spawn)
    monkeypatch.setattr(main_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(main_module.time, "time", lambda: next(ticks))

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 1
    spawn.assert_called_once()


def test_spawn_daemon_process_hides_windows_console(monkeypatch):
    """Windows daemon auto-start should use hidden detached process flags."""
    args = argparse.Namespace(
        notes_dir="notes",
        graph_db_path="graph.kuzu",
        database_path=None,
        log_level="INFO",
        transport="stdio",
        host="127.0.0.1",
        port=8765,
        backend_mode="daemon",
        run_daemon=False,
        daemon_host="127.0.0.1",
        daemon_port=8766,
    )
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)

    main_module._spawn_daemon_process(args)

    creationflags = captured["kwargs"]["creationflags"]
    assert creationflags & getattr(main_module.subprocess, "DETACHED_PROCESS", 0)
    assert creationflags & getattr(
        main_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    assert creationflags & getattr(main_module.subprocess, "CREATE_NO_WINDOW", 0)
