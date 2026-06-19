"""Tests for the facade parent-death watchdog's host-resolution logic.

Only the pure parent-chain walk is unit-tested; the OS-specific wait (which calls
os._exit on the host's death) is a thin best-effort wrapper exercised at runtime.
"""

from parazettel_mcp.main import _resolve_session_host


def test_resolves_first_non_python_ancestor_through_launcher_pair():
    # real python <- venv launcher python <- claude.exe (the session host)
    ppid = {100: 90, 90: 50, 50: 1}
    name = {100: "python.exe", 90: "python.exe", 50: "claude.exe", 1: "wininit.exe"}
    assert _resolve_session_host(100, ppid, name) == 50


def test_resolves_direct_non_python_parent():
    assert _resolve_session_host(7, {7: 3}, {7: "pythonw.exe", 3: "claude.exe"}) == 3


def test_pythonw_launcher_is_traversed():
    ppid = {100: 90, 90: 50}
    name = {100: "python.exe", 90: "pythonw.exe", 50: "claude.exe"}
    assert _resolve_session_host(100, ppid, name) == 50


def test_orphan_when_host_ancestor_is_gone():
    # The launcher (90) is alive but ITS parent (50, the host) is gone — orphan.
    assert (
        _resolve_session_host(
            100, {100: 90, 90: 50}, {100: "python.exe", 90: "python.exe"}
        )
        is None
    )


def test_orphan_when_immediate_parent_missing():
    assert _resolve_session_host(100, {100: 90}, {100: "python.exe"}) is None


def test_depth_guard_stops_on_all_python_chain():
    # A pathological all-python chain must terminate (no infinite loop) -> None.
    ppid = {i: i + 1 for i in range(100, 130)}
    name = {i: "python.exe" for i in range(100, 131)}
    assert _resolve_session_host(100, ppid, name, max_depth=5) is None
