"""Tests for the daemon's per-request memory-logging diagnostics."""

import time
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


def test_record_request_memory_logs_rss_only_when_no_commit(monkeypatch, caplog):
    """The POSIX-style reading (commit_mb=None) logs rss with no commit figure."""
    monkeypatch.setattr(daemon_server, "_MEMORY_LOG_EVERY_N_REQUESTS", 1)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (789.0, None))
    daemon = _make_daemon()

    with caplog.at_level("INFO", logger="parazettel_mcp.daemon.server"):
        daemon._record_request_memory()

    logged = [r.getMessage() for r in caplog.records if "daemon memory" in r.getMessage()]
    assert len(logged) == 1
    assert "rss=789 MB" in logged[0]
    assert "commit=" not in logged[0]


def test_recycle_when_over_cap_after_idle_grace(monkeypatch):
    """Over the RSS ceiling AND idle past the grace -> recycle; within grace -> wait."""
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 1000 * 1024 * 1024)  # 1000 MB
    monkeypatch.setattr(config, "daemon_memory_recycle_idle_grace_seconds", 20.0)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (1500.0, 1800.0))
    daemon = _make_daemon()

    assert daemon._should_recycle_for_memory(idle_for=30.0) is True
    # Inside the grace window an in-flight request may still be running -> don't cut it.
    assert daemon._should_recycle_for_memory(idle_for=5.0) is False


def test_no_recycle_when_under_cap(monkeypatch):
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 4000 * 1024 * 1024)
    monkeypatch.setattr(config, "daemon_memory_recycle_idle_grace_seconds", 20.0)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (1500.0, 1800.0))
    daemon = _make_daemon()

    assert daemon._should_recycle_for_memory(idle_for=30.0) is False


def test_recycle_disabled_when_cap_zero(monkeypatch):
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 0)
    daemon = _make_daemon()

    assert daemon._should_recycle_for_memory(idle_for=99999.0) is False


def test_no_recycle_when_memory_probe_unavailable(monkeypatch):
    """A probe that returns None (memory unknown) must never trigger a recycle."""
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 100 * 1024 * 1024)
    monkeypatch.setattr(config, "daemon_memory_recycle_idle_grace_seconds", 0.0)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: None)
    daemon = _make_daemon()

    assert daemon._should_recycle_for_memory(idle_for=30.0) is False


def test_monitor_thread_recycles_when_over_memory_even_without_idle_timeout(monkeypatch):
    """End-to-end: the monitor starts (memory cap on though idle timeout is off)
    and calls shutdown once the resident set is over the ceiling."""
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 100 * 1024 * 1024)
    monkeypatch.setattr(config, "daemon_memory_recycle_idle_grace_seconds", 0.0)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (9999.0, 9999.0))
    monkeypatch.setattr(daemon_server, "_IDLE_POLL_INTERVAL_SECONDS", 0.02)

    daemon = _make_daemon()
    daemon._idle_timeout_seconds = 0  # only the memory path can fire
    daemon._httpd = MagicMock()  # so the monitor's "httpd is None" guard passes
    daemon._last_activity = time.monotonic() - 100  # well past the (0s) grace
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "shutdown", lambda: calls.__setitem__("n", calls["n"] + 1))

    daemon._start_idle_monitor()
    try:
        deadline = time.time() + 3
        while calls["n"] == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert calls["n"] >= 1  # the monitor recycled on high memory
    finally:
        daemon._shutdown_event.set()
        if daemon._idle_monitor_thread is not None:
            daemon._idle_monitor_thread.join(timeout=2)


def test_serving_request_tracks_in_flight_and_resets_activity():
    """_serving_request marks a request in flight and bumps last_activity at exit."""
    daemon = _make_daemon()
    assert daemon._has_in_flight_request() is False
    before = daemon._last_activity
    with daemon._serving_request():
        assert daemon._has_in_flight_request() is True
    assert daemon._has_in_flight_request() is False
    assert daemon._last_activity >= before  # idle timer reset on completion


def test_monitor_never_recycles_while_a_request_is_in_flight(monkeypatch):
    """A long in-flight request (e.g. a rebuild) must NOT be cut off, even when the
    daemon looks idle and is over the memory ceiling."""
    from parazettel_mcp.config import config

    monkeypatch.setattr(config, "daemon_max_rss_bytes", 100 * 1024 * 1024)
    monkeypatch.setattr(config, "daemon_memory_recycle_idle_grace_seconds", 0.0)
    monkeypatch.setattr(daemon_server, "_process_memory_mb", lambda: (9999.0, 9999.0))
    monkeypatch.setattr(daemon_server, "_IDLE_POLL_INTERVAL_SECONDS", 0.02)

    daemon = _make_daemon()
    daemon._idle_timeout_seconds = 0
    daemon._httpd = MagicMock()
    daemon._last_activity = time.monotonic() - 100  # looks idle for 100s
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "shutdown", lambda: calls.__setitem__("n", calls["n"] + 1))

    daemon._start_idle_monitor()
    try:
        with daemon._serving_request():  # request in flight
            time.sleep(0.2)  # several poll intervals
            assert calls["n"] == 0  # monitor held off — request not cut
        # Once it completes, the daemon may recycle (reclaiming the leaked memory).
        deadline = time.time() + 2
        while calls["n"] == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert calls["n"] >= 1
    finally:
        daemon._shutdown_event.set()
        if daemon._idle_monitor_thread is not None:
            daemon._idle_monitor_thread.join(timeout=2)
