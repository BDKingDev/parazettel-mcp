"""Tests for the local Parazettel daemon and thin client."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.daemon.client import DaemonRpcClient, DaemonUnavailableError
from parazettel_mcp.daemon.server import ParazettelDaemonServer
from parazettel_mcp.models.schema import Note, NoteSource, NoteType
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer


@pytest.fixture
def daemon_server(test_config):
    """Start a local daemon bound to an ephemeral localhost port."""
    server = ParazettelDaemonServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            host, port = server.server_address
            if port != 0:
                break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        server.shutdown()
        raise RuntimeError("Daemon server did not start in time")

    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_daemon_health_reports_ready_state(daemon_server):
    """Health endpoint should report daemon readiness and writable storage."""
    client = DaemonRpcClient(daemon_server.base_url)

    health = client.health()

    assert health["ok"] is True
    assert health["mode"] == "daemon"
    assert health["graph_writable"] is True
    assert health["version"] == config.server_version


def _create_area(client: DaemonRpcClient) -> Note:
    """Create a valid area for routing daemon-created notes."""
    return client.call(
        "zettel_service",
        "create_area_note",
        kwargs={
            "title": "Daemon Area",
            "content": "Area for daemon tests",
            "cadence": None,
        },
    )


def test_daemon_client_round_trips_note_creation_and_lookup(daemon_server):
    """The thin client should create and retrieve notes via daemon-owned services."""
    client = DaemonRpcClient(daemon_server.base_url)
    area = _create_area(client)

    created = client.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Daemon Created",
            "content": "Created through the daemon",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )
    fetched = client.call("zettel_service", "get_note", args=[created.id])

    assert isinstance(created, Note)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.title == "Daemon Created"


def test_daemon_client_decodes_search_results(daemon_server):
    """Search RPC results should decode back into SearchResult objects."""
    client = DaemonRpcClient(daemon_server.base_url)
    area = _create_area(client)
    created = client.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Daemon Search Decode",
            "content": "Search through daemon-backed notes",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )

    results = client.call(
        "search_service",
        "search_combined",
        kwargs={"text": "Daemon Search Decode", "note_type": NoteType.PERMANENT},
    )

    assert results
    assert results[0].note.id == created.id
    assert results[0].score > 0


def test_two_clients_share_one_daemon_without_db_lock(daemon_server):
    """Independent clients should observe shared writes through one daemon."""
    first = DaemonRpcClient(daemon_server.base_url)
    second = DaemonRpcClient(daemon_server.base_url)
    area = _create_area(first)

    created = first.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Shared Through Daemon",
            "content": "One owner process, two clients",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )
    found = second.call("zettel_service", "get_note", args=[created.id])

    assert found is not None
    assert found.id == created.id


def test_daemon_client_reports_connection_failures_cleanly():
    """Missing daemons should raise a user-actionable client error."""
    client = DaemonRpcClient("http://127.0.0.1:65500", timeout_seconds=0.2)

    with pytest.raises(DaemonUnavailableError, match="daemon is unavailable"):
        client.health()


def test_mcp_server_uses_daemon_backend_when_configured(
    daemon_server, test_config
):
    """Configured MCP facade should proxy through the daemon instead of opening the DB."""
    original_backend_mode = config.backend_mode
    original_daemon_host = config.daemon_host
    original_daemon_port = config.daemon_port
    original_transport = config.server_transport
    registered_tools = {}
    mock_mcp = MagicMock()

    def mock_tool_decorator(*args, **kwargs):
        def tool_wrapper(func):
            registered_tools[kwargs.get("name")] = func
            return func

        return tool_wrapper

    mock_mcp.tool = mock_tool_decorator

    config.backend_mode = "daemon"
    host, port = daemon_server.server_address
    config.daemon_host = host
    config.daemon_port = port
    config.server_transport = "stdio"

    with patch("parazettel_mcp.server.mcp_server.FastMCP", return_value=mock_mcp):
        server = ZettelkastenMcpServer()
        try:
            area = daemon_server.zettel_service.create_area_note(
                "Proxy Area",
                "Area for proxy test",
            )
            create_note_func = registered_tools["pzk_create_note"]
            result = create_note_func(
                title="Proxy Created",
                content="Created via daemon-backed MCP facade",
                note_type="permanent",
                tags="proxy, daemon",
                source="manual",
                area_id=area.id,
            )

            assert "successfully" in result
        finally:
            server.close()
            config.backend_mode = original_backend_mode
            config.daemon_host = original_daemon_host
            config.daemon_port = original_daemon_port
            config.server_transport = original_transport
