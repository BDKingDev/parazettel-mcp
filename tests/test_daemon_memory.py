"""Tests for the daemon's per-request memory-logging diagnostics."""

from unittest.mock import MagicMock

from parazettel_mcp.daemon import server as daemon_server
from parazettel_mcp.daemon.server import ParazettelDaemonServer, _process_memory_mb


def _make_daemon() -> ParazettelDaemonServer:
    """A daemon with mock services so no DB/network is touched."""
    return ParazettelDaemonServer(
        "127.0.0.1",
        0,
        zettel_service=MagicMock(),
        search_service=MagicMock(),
    )


def test_process_memory_mb_returns_tuple_or_none():
    """The probe returns (working_set_MB, commit_MB|None) or None — never raises."""
    mem = _process_memory_mb()
    assert mem is None or (
        isinstance(mem, tuple)
        and len(mem) == 2
        and isinstance(mem[0], float)
        and (mem[1] is None or isinstance(mem[1], float))
    )


def test_record_request_memory_logs_every_n(monkeypatch, caplog):
    """Memory is logged only on the Nth request, with a running count."""
    monkeypatch.setattr(daemon_server, "_MEMORY_LOG_EVERY_N_REQUESTS", 3)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (123.0, 456.0))
    daemon = _make_daemon()

    with caplog.at_level("INFO", logger="parazettel_mcp.daemon.server"):
        daemon._record_request_memory()
        daemon._record_request_memory()
        assert not any("daemon memory" in r.getMessage() for r in caplog.records)
        daemon._record_request_memory()  # 3rd request -> logs

    assert daemon._request_count == 3
    logged = [r.getMessage() for r in caplog.records if "daemon memory" in r.getMessage()]
    assert len(logged) == 1
    assert "after 3 requests" in logged[0]
    assert "commit=456 MB" in logged[0]


def test_record_request_memory_silent_when_probe_unavailable(monkeypatch, caplog):
    """A None memory reading logs nothing but still advances the counter."""
    monkeypatch.setattr(daemon_server, "_MEMORY_LOG_EVERY_N_REQUESTS", 1)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: None)
    daemon = _make_daemon()

    with caplog.at_level("INFO", logger="parazettel_mcp.daemon.server"):
        daemon._record_request_memory()

    assert daemon._request_count == 1
    assert not any("daemon memory" in r.getMessage() for r in caplog.records)
