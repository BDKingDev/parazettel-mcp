"""Tests for the local Parazettel daemon and thin client."""

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.daemon.client import (
    DaemonBusyError,
    DaemonRpcClient,
    DaemonUnavailableError,
    RemoteServiceProxy,
)
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
    assert health["pid"] > 0


def test_rerank_rpc_round_trip(daemon_server):
    """The dedup reranker is reachable over RPC and returns the scores.

    The reranker lives in the daemon's ZettelService now; the facade reaches it
    via this RPC instead of importing fastembed itself. Inject a fake scorer into
    the live daemon's service and confirm the score list crosses the boundary.
    """
    daemon_server.zettel_service._reranker = SimpleNamespace(
        score=lambda _query, docs: [float(len(d)) for d in docs]
    )
    proxy = RemoteServiceProxy(
        DaemonRpcClient(daemon_server.base_url, timeout_seconds=10), "zettel_service"
    )
    assert proxy.rerank("query", ["ab", "abcd"]) == [2.0, 4.0]


def test_rerank_rpc_surfaces_reranker_error_as_reranker_error(daemon_server):
    """A daemon-side RerankerError relays back to the client as RerankerError.

    The facade's dedup path catches RerankerError specifically (fail loud, no
    silent BM25 fallback), so the type must survive the RPC boundary — which it
    does only because client.ERROR_REGISTRY maps it.
    """
    from parazettel_mcp.services.reranker import RerankerError

    daemon_server.zettel_service._reranker = None  # disabled -> rerank raises
    proxy = RemoteServiceProxy(
        DaemonRpcClient(daemon_server.base_url, timeout_seconds=10), "zettel_service"
    )
    with pytest.raises(RerankerError):
        proxy.rerank("query", ["a"])


def test_daemon_rejects_non_loopback_bind():
    """The daemon should refuse non-loopback bind hosts."""
    with pytest.raises(ValueError, match="loopback hosts"):
        ParazettelDaemonServer("0.0.0.0", 8766)


class _FakeHealthResponse:
    """Minimal context-manager response with a health-shaped JSON body."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok": true, "version": "test"}'


def test_rpc_recovers_via_on_unavailable_then_retries(monkeypatch):
    """A first connection failure triggers one (re)start + retry, then succeeds."""
    from urllib import error as urlerror

    import parazettel_mcp.daemon.client as client_mod

    state = {"calls": 0, "restarts": 0}

    def fake_urlopen(req, timeout=None):
        state["calls"] += 1
        if state["calls"] == 1:
            raise urlerror.URLError("connection refused")
        return _FakeHealthResponse()

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)

    def on_unavailable():
        state["restarts"] += 1
        return True

    client = DaemonRpcClient("http://127.0.0.1:8766", on_unavailable=on_unavailable)
    result = client.health()

    assert result["ok"] is True
    assert state["restarts"] == 1  # restarted exactly once
    assert state["calls"] == 2  # original request + one retry


def test_rpc_without_callback_raises_immediately(monkeypatch):
    from urllib import error as urlerror

    import parazettel_mcp.daemon.client as client_mod

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urlerror.URLError("down")

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)
    client = DaemonRpcClient("http://127.0.0.1:8766")  # no on_unavailable

    with pytest.raises(DaemonUnavailableError):
        client.health()
    assert calls["n"] == 1  # no retry without a restart callback


def test_rpc_retries_only_once_then_gives_up(monkeypatch):
    """If the daemon stays down, retry exactly once — never loop forever."""
    from urllib import error as urlerror

    import parazettel_mcp.daemon.client as client_mod

    state = {"calls": 0, "restarts": 0}

    def fake_urlopen(req, timeout=None):
        state["calls"] += 1
        raise urlerror.URLError("still down")

    monkeypatch.setattr(client_mod.request, "urlopen", fake_urlopen)

    def on_unavailable():
        state["restarts"] += 1
        return True

    client = DaemonRpcClient("http://127.0.0.1:8766", on_unavailable=on_unavailable)
    with pytest.raises(DaemonUnavailableError):
        client.health()
    assert state["restarts"] == 1
    assert state["calls"] == 2  # original + a single retry


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


def test_daemon_task_queries_reflect_live_task_writes(daemon_server):
    """Task queries should see new and reassigned tasks without requiring rebuild."""
    client = DaemonRpcClient(daemon_server.base_url)
    area = _create_area(client)
    primary_project = client.call(
        "zettel_service",
        "create_project_note",
        kwargs={
            "title": "Daemon Task Project",
            "content": "Primary project",
            "area_id": area.id,
            "source": NoteSource.MANUAL,
        },
    )
    secondary_project = client.call(
        "zettel_service",
        "create_project_note",
        kwargs={
            "title": "Daemon Reassigned Project",
            "content": "Secondary project",
            "area_id": area.id,
            "source": NoteSource.MANUAL,
        },
    )

    task = client.call(
        "zettel_service",
        "create_task",
        kwargs={
            "title": "Daemon Visible Task",
            "content": "Should appear immediately in task queries",
            "project_id": primary_project.id,
            "status": "ready",
            "source": NoteSource.MANUAL,
        },
    )

    fetched_tasks = client.call(
        "zettel_service",
        "get_tasks",
        kwargs={"project_id": primary_project.id},
    )
    project_tasks = client.call(
        "zettel_service",
        "get_project_tasks",
        args=[primary_project.id],
    )

    assert any(found.id == task.id for found in fetched_tasks)
    assert any(found.id == task.id for found in project_tasks)

    updated = client.call(
        "zettel_service",
        "update_task",
        kwargs={"note_id": task.id, "project_id": secondary_project.id},
    )
    assert updated.project_id == secondary_project.id

    old_project_tasks = client.call(
        "zettel_service",
        "get_project_tasks",
        args=[primary_project.id],
    )
    new_project_tasks = client.call(
        "zettel_service",
        "get_project_tasks",
        args=[secondary_project.id],
    )

    assert all(found.id != task.id for found in old_project_tasks)
    assert any(found.id == task.id for found in new_project_tasks)


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


def test_daemon_client_can_rebuild_index(daemon_server):
    """Rebuild RPC should succeed through the daemon and preserve notes."""
    client = DaemonRpcClient(daemon_server.base_url, timeout_seconds=30.0)
    area = _create_area(client)
    created = client.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Daemon Rebuild",
            "content": "Survives a daemon rebuild",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )

    # The rebuild is async: the trigger returns immediately, the work runs on a
    # background worker, and we wait for maintenance to clear before reading back.
    start = client.call("zettel_service", "rebuild_index")
    assert start == {"status": "started"}
    assert _wait_for(
        lambda: client.health()["maintenance_reason"] is None, timeout=30.0
    )
    status = client.call("zettel_service", "rebuild_status")
    assert status["status"] == "success"

    fetched = client.call("zettel_service", "get_note", args=[created.id])
    assert fetched is not None
    assert fetched.id == created.id


def test_daemon_link_queries_reflect_live_note_links(daemon_server):
    """Linked-note and orphan queries should reflect note updates without rebuild."""
    client = DaemonRpcClient(daemon_server.base_url)
    area = _create_area(client)
    source = client.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Daemon Link Source",
            "content": "Source note",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )
    target = client.call(
        "zettel_service",
        "create_note",
        kwargs={
            "title": "Daemon Link Target",
            "content": "Target note",
            "note_type": NoteType.PERMANENT,
            "source": NoteSource.MANUAL,
            "area_id": area.id,
        },
    )

    linked_before = client.call(
        "zettel_service", "get_linked_notes", args=[source.id, "outgoing"]
    )
    orphaned_before = client.call("search_service", "find_orphaned_notes")
    assert all(found.id != target.id for found in linked_before)
    assert all(found.id != source.id for found in orphaned_before)
    assert all(found.id != target.id for found in orphaned_before)

    client.call(
        "zettel_service",
        "create_link",
        kwargs={
            "source_id": source.id,
            "target_id": target.id,
            "link_type": "reference",
            "bidirectional": True,
        },
    )

    outgoing = client.call(
        "zettel_service",
        "get_linked_notes",
        args=[source.id, "outgoing"],
    )
    incoming = client.call(
        "zettel_service",
        "get_linked_notes",
        args=[target.id, "incoming"],
    )
    orphaned_after = client.call("search_service", "find_orphaned_notes")

    assert any(found.id == target.id for found in outgoing)
    assert any(found.id == source.id for found in incoming)
    assert all(found.id != source.id for found in orphaned_after)
    assert all(found.id != target.id for found in orphaned_after)


def _wait_for(predicate, timeout=5.0, interval=0.02):
    """Poll predicate() until truthy or timeout; return its last value."""
    deadline = time.time() + timeout
    value = predicate()
    while not value and time.time() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


def test_rebuild_index_runs_async_and_rejects_concurrent_calls(daemon_server, monkeypatch):
    """The rebuild trigger returns immediately (async) while the work runs on a
    background worker; concurrent calls are rejected cleanly until it finishes."""
    rebuild_client = DaemonRpcClient(daemon_server.base_url, timeout_seconds=5.0)
    other_client = DaemonRpcClient(daemon_server.base_url, timeout_seconds=1.0)
    started = threading.Event()
    release = threading.Event()

    def slow_rebuild():
        started.set()
        assert release.wait(timeout=5.0)
        return None

    monkeypatch.setattr(daemon_server.zettel_service, "rebuild_index", slow_rebuild)

    # The trigger returns immediately with a "started" status — it does NOT block
    # for the duration of the rebuild (that was the timeout that read as a crash).
    start = rebuild_client.call("zettel_service", "rebuild_index")
    assert start == {"status": "started"}
    assert started.wait(timeout=5.0)  # the background worker actually began

    health = rebuild_client.health()
    assert health["maintenance_reason"] == "rebuild_index"
    assert rebuild_client.call("zettel_service", "rebuild_status")["status"] == "running"

    # A second trigger does not start a duplicate — it reports the running status.
    second = rebuild_client.call("zettel_service", "rebuild_index")
    assert second.get("already_running") is True
    assert second.get("status") == "running"

    # Other calls are rejected with a DaemonBusyError (not a bare RuntimeError that
    # reads like a crash); the message names the maintenance in human terms while
    # still carrying the raw reason token.
    with pytest.raises(DaemonBusyError) as excinfo:
        other_client.call("zettel_service", "get_all_tags")
    message = str(excinfo.value)
    assert "rebuilding the search index" in message
    assert "rebuild_index" in message

    # Let the worker finish; maintenance clears and status flips to success.
    release.set()
    assert _wait_for(lambda: rebuild_client.health()["maintenance_reason"] is None)
    final = rebuild_client.call("zettel_service", "rebuild_status")
    assert final["status"] == "success"
    assert final["error"] is None


def test_rebuild_status_reports_worker_failure(daemon_server, monkeypatch):
    """A rebuild that raises is captured into status (not crashed) and maintenance
    is cleared so the daemon keeps serving."""
    client = DaemonRpcClient(daemon_server.base_url, timeout_seconds=5.0)

    def boom():
        raise RuntimeError("kaboom during rebuild")

    monkeypatch.setattr(daemon_server.zettel_service, "rebuild_index", boom)

    assert client.call("zettel_service", "rebuild_index") == {"status": "started"}
    assert _wait_for(lambda: client.health()["maintenance_reason"] is None)

    status = client.call("zettel_service", "rebuild_status")
    assert status["status"] == "error"
    assert "kaboom during rebuild" in status["error"]
    # Daemon still serves normal calls after a failed rebuild.
    assert isinstance(client.call("zettel_service", "get_all_tags"), list)


def test_maintenance_busy_message_falls_back_to_raw_reason():
    """An unknown maintenance reason is still named (raw token), never bare 'busy'."""
    from parazettel_mcp.daemon.server import _maintenance_busy_message

    known = _maintenance_busy_message("rebuild_index")
    assert "rebuilding the search index" in known
    assert "rebuild_index" in known

    unknown = _maintenance_busy_message("compacting_store")
    assert "compacting_store" in unknown
    assert "Try the request again" in unknown


def test_daemon_busy_error_is_registered_for_client_decode():
    """The client can reconstruct a remote DaemonBusyError as its own type."""
    from parazettel_mcp.daemon.client import ERROR_REGISTRY

    assert ERROR_REGISTRY["DaemonBusyError"] is DaemonBusyError


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

    with pytest.raises(DaemonUnavailableError, match="daemon is unavailable") as excinfo:
        client.health()

    message = str(excinfo.value)
    # The error must tell the user how to bring the daemon back up.
    assert "python -m parazettel_mcp.main --run-daemon" in message
    assert "--daemon-status" in message


def test_daemon_shutdown_endpoint_stops_server(daemon_server):
    """The daemon should expose a clean shutdown endpoint for lifecycle management."""
    client = DaemonRpcClient(daemon_server.base_url)

    response = client.shutdown()

    assert response["ok"] is True

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            client.health()
        except DaemonUnavailableError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Daemon did not stop after shutdown request")


def test_daemon_idle_timeout_stops_unused_server(test_config):
    """Idle daemon instances should shut themselves down after the configured timeout."""
    server = ParazettelDaemonServer("127.0.0.1", 0, idle_timeout_seconds=0.2)
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

    client = DaemonRpcClient(server.base_url, timeout_seconds=0.2)
    assert client.health()["ok"] is True

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            client.health()
        except DaemonUnavailableError:
            break
        time.sleep(0.05)
    else:
        server.shutdown()
        raise AssertionError("Idle daemon did not stop in time")

    thread.join(timeout=5)


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


def test_mcp_server_uses_short_health_timeout_and_longer_rpc_timeout(monkeypatch):
    """Daemon-backed MCP should keep health checks short without starving RPC calls."""
    original_backend_mode = config.backend_mode
    original_daemon_host = config.daemon_host
    original_daemon_port = config.daemon_port
    original_transport = config.server_transport
    original_rpc_timeout = config.daemon_rpc_timeout_seconds
    client_calls = []
    mock_mcp = MagicMock()

    def fake_tool_decorator(*args, **kwargs):
        def wrapper(func):
            return func

        return wrapper

    mock_mcp.tool = fake_tool_decorator

    class FakeClient:
        def __init__(self, base_url: str, timeout_seconds: float = 5.0, **kwargs):
            client_calls.append((base_url, timeout_seconds))

        def health(self):
            return {"ok": True}

    config.backend_mode = "daemon"
    config.daemon_host = "127.0.0.1"
    config.daemon_port = 8766
    config.server_transport = "stdio"
    config.daemon_rpc_timeout_seconds = 123.0

    monkeypatch.setattr(
        "parazettel_mcp.server.mcp_server.FastMCP",
        lambda *args, **kwargs: mock_mcp,
    )
    monkeypatch.setattr("parazettel_mcp.server.mcp_server.DaemonRpcClient", FakeClient)

    server = ZettelkastenMcpServer()
    try:
        assert client_calls == [
            ("http://127.0.0.1:8766", 5.0),
            ("http://127.0.0.1:8766", 123.0),
        ]
    finally:
        server.close()
        config.backend_mode = original_backend_mode
        config.daemon_host = original_daemon_host
        config.daemon_port = original_daemon_port
        config.server_transport = original_transport
        config.daemon_rpc_timeout_seconds = original_rpc_timeout


# --- Single-owner port binding (daemon flapping regression) -----------------
#
# On Windows the stdlib HTTPServer's SO_REUSEADDR let a second daemon co-bind
# the daemon port and steal connections from the live one, so two daemons
# "served" 8766 at once (the loser fell back to a read-only graph) and clients
# flapped. The daemon now binds exclusively so a second start loses cleanly.


def test_daemon_http_server_keeps_posix_reuse_but_not_windows():
    """SO_REUSEADDR is kept on POSIX (TIME_WAIT reuse) but dropped on Windows
    (where it permits address stealing)."""
    from parazettel_mcp.daemon.server import _ExclusiveThreadingHTTPServer

    assert _ExclusiveThreadingHTTPServer.allow_reuse_address == (os.name != "nt")


def test_second_daemon_loses_the_port_race():
    """A second daemon binding an already-bound port must fail, never co-bind.

    Reproduces the flapping bug: before the fix, this second bind SUCCEEDED on
    Windows (port theft). Mock services keep it a pure socket-bind test (no Kuzu).
    """
    first = ParazettelDaemonServer(
        "127.0.0.1", 0, zettel_service=MagicMock(), search_service=MagicMock()
    )
    first.bind()
    try:
        _host, port = first.server_address
        second = ParazettelDaemonServer(
            "127.0.0.1", port, zettel_service=MagicMock(), search_service=MagicMock()
        )
        # Match the address-in-use error cross-platform (EADDRINUSE / WinError
        # 10048) so an unrelated OSError can't pass the test.
        with pytest.raises(OSError, match=r"(?i)(address.*in use|10048)"):
            second.bind()
    finally:
        if first._httpd is not None:
            first._httpd.server_close()


def test_serve_forever_closes_socket_if_initialize_fails():
    """A warmup failure must close the bound socket, not leave a dead listener.

    serve_forever() binds before initialize(); if initialize() raises, the
    socket must be closed and the handle cleared so callers/tests can't observe
    a live-looking server_address for a server that never started serving.
    """
    zs = MagicMock()
    zs.initialize.side_effect = RuntimeError("warmup boom")
    daemon = ParazettelDaemonServer(
        "127.0.0.1", 0, zettel_service=zs, search_service=MagicMock()
    )
    with pytest.raises(RuntimeError, match="warmup boom"):
        daemon.serve_forever()
    assert daemon._httpd is None  # socket closed + handle cleared on the failure path


def test_daemon_bind_is_idempotent(test_config):
    """Calling bind() twice keeps the same bound socket (serve_forever re-calls
    it after main already bound)."""
    server = ParazettelDaemonServer(
        "127.0.0.1", 0, zettel_service=MagicMock(), search_service=MagicMock()
    )
    server.bind()
    httpd = server._httpd
    try:
        server.bind()
        assert server._httpd is httpd
    finally:
        if server._httpd is not None:
            server._httpd.server_close()
