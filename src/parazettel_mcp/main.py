#!/usr/bin/env python
"""Main entry point for the Zettelkasten MCP server."""
import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

from parazettel_mcp.config import DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS, config
from parazettel_mcp.daemon.client import DaemonRpcClient, DaemonUnavailableError
from parazettel_mcp.daemon.server import ParazettelDaemonServer
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer
from parazettel_mcp.utils import setup_logging

logger = logging.getLogger(__name__)

_DAEMON_START_TIMEOUT_SECONDS = 10.0
_DAEMON_HEALTH_POLL_INTERVAL_SECONDS = 0.25
_DAEMON_STOP_TIMEOUT_SECONDS = 10.0


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Zettelkasten MCP Server")
    parser.add_argument(
        "--notes-dir",
        help="Directory for storing note files",
        type=str,
        default=os.environ.get("PARAZETTEL_NOTES_DIR"),
    )
    parser.add_argument(
        "--graph-db-path",
        help="Kuzu graph database file path",
        type=str,
        default=os.environ.get("PARAZETTEL_GRAPH_DB_PATH"),
    )
    parser.add_argument(
        "--database-path",
        help="Deprecated alias for the graph DB path. Legacy *.db values map to a sibling graph.kuzu path.",
        type=str,
        default=os.environ.get("PARAZETTEL_DATABASE_PATH"),
    )
    parser.add_argument(
        "--log-level",
        help="Logging level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=os.environ.get("PARAZETTEL_LOG_LEVEL", "INFO"),
    )
    parser.add_argument(
        "--transport",
        help="MCP transport to use",
        choices=["stdio", "sse"],
        default=os.environ.get("PARAZETTEL_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        help="Host to bind when using SSE transport",
        type=str,
        default=os.environ.get("PARAZETTEL_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        help="Port to bind when using SSE transport",
        type=int,
        default=int(os.environ.get("PARAZETTEL_MCP_PORT", "8765")),
    )
    parser.add_argument(
        "--backend-mode",
        help="Backend mode for MCP tool execution",
        choices=["direct", "daemon"],
        default=os.environ.get("PARAZETTEL_BACKEND_MODE", "direct"),
    )
    parser.add_argument(
        "--run-daemon",
        help="Run the local Parazettel daemon instead of the MCP facade",
        action="store_true",
    )
    parser.add_argument(
        "--daemon-status",
        help="Report local daemon status and exit",
        action="store_true",
    )
    parser.add_argument(
        "--stop-daemon",
        help="Stop the local Parazettel daemon and exit",
        action="store_true",
    )
    parser.add_argument(
        "--daemon-host",
        help="Host to bind the local Parazettel daemon",
        type=str,
        default=os.environ.get("PARAZETTEL_DAEMON_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--daemon-port",
        help="Port to bind the local Parazettel daemon",
        type=int,
        default=int(os.environ.get("PARAZETTEL_DAEMON_PORT", "8766")),
    )
    parser.add_argument(
        "--daemon-idle-timeout",
        help="Idle seconds before the local daemon shuts itself down (0 disables)",
        type=float,
        default=DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS,
    )
    return parser.parse_args()


def update_config(args):
    """Update the global config with command line arguments."""
    if args.notes_dir:
        config.notes_dir = Path(args.notes_dir)
    if args.graph_db_path:
        config.graph_db_path = Path(args.graph_db_path)
    elif getattr(args, "database_path", None):
        legacy_path = Path(args.database_path)
        if legacy_path.suffix == ".db":
            config.graph_db_path = legacy_path.with_name("graph.kuzu")
        else:
            config.graph_db_path = legacy_path
    config.server_transport = args.transport
    config.server_host = args.host
    config.server_port = args.port
    config.backend_mode = args.backend_mode
    config.daemon_host = args.daemon_host
    config.daemon_port = args.daemon_port
    config.daemon_idle_timeout_seconds = getattr(
        args, "daemon_idle_timeout", config.daemon_idle_timeout_seconds
    )


def _write_daemon_pid_file(pid: int) -> Path:
    """Write the daemon PID file and return its path."""
    pid_file = config.get_daemon_pid_file()
    pid_file.write_text(str(pid), encoding="utf-8")
    return pid_file


def _remove_daemon_pid_file() -> None:
    """Remove the daemon PID file if it exists."""
    pid_file = config.get_daemon_pid_file()
    if pid_file.exists():
        pid_file.unlink()


def _read_daemon_pid_file() -> int | None:
    """Read the daemon PID file if it exists and contains a valid PID."""
    pid_file = config.get_daemon_pid_file()
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_running(pid: int) -> bool:
    """Return True if a process with *pid* appears to still be running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    """Wait for a PID to exit and return True if it stopped in time."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(_DAEMON_HEALTH_POLL_INTERVAL_SECONDS)
    return not _pid_is_running(pid)


def get_daemon_status() -> dict[str, object]:
    """Collect the current daemon health and PID-file status."""
    pid = _read_daemon_pid_file()
    pid_running = bool(pid and _pid_is_running(pid))
    client = DaemonRpcClient(config.get_daemon_base_url())
    try:
        health = client.health()
        healthy = True
        error_message = None
    except DaemonUnavailableError as exc:
        health = None
        healthy = False
        error_message = str(exc)

    if pid and not pid_running:
        _remove_daemon_pid_file()

    return {
        "healthy": healthy,
        "health": health,
        "pid": pid,
        "pid_running": pid_running,
        "pid_file": str(config.get_daemon_pid_file()),
        "base_url": config.get_daemon_base_url(),
        "error": error_message,
    }


def format_daemon_status(status: dict[str, object]) -> str:
    """Render daemon status as a human-readable CLI message."""
    lines = [f"Daemon URL: {status['base_url']}"]
    lines.append(f"PID file: {status['pid_file']}")
    if status["healthy"]:
        health = status["health"] or {}
        lines.insert(0, "Parazettel daemon is running.")
        if health.get("pid") is not None:
            lines.append(f"PID: {health['pid']}")
        lines.append(f"Graph writable: {health.get('graph_writable')}")
        lines.append(f"Idle timeout: {health.get('idle_timeout_seconds')}")
        lines.append(f"Version: {health.get('version')}")
        return "\n".join(lines)

    if status["pid_running"]:
        lines.insert(0, "Parazettel daemon process exists but is unhealthy.")
    else:
        lines.insert(0, "Parazettel daemon is not running.")
    if status["pid"] is not None:
        lines.append(f"Last known PID: {status['pid']}")
    if status["error"]:
        lines.append(f"Error: {status['error']}")
    lines.append(f"To start it: {config.format_daemon_start_command()}")
    return "\n".join(lines)


def stop_daemon() -> str:
    """Stop the managed local daemon and return a status message."""
    status = get_daemon_status()
    pid = status["pid"]

    if status["healthy"]:
        client = DaemonRpcClient(config.get_daemon_base_url())
        client.shutdown()
        if pid is not None:
            _wait_for_pid_exit(pid, _DAEMON_STOP_TIMEOUT_SECONDS)
        _remove_daemon_pid_file()
        return "Parazettel daemon stopped."

    if pid and status["pid_running"]:
        os.kill(pid, signal.SIGTERM)
        _wait_for_pid_exit(pid, _DAEMON_STOP_TIMEOUT_SECONDS)
        _remove_daemon_pid_file()
        return f"Parazettel daemon process {pid} terminated."

    _remove_daemon_pid_file()
    return "Parazettel daemon was not running."


def _get_windows_background_python() -> str:
    """Prefer pythonw.exe for detached background launches on Windows."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return sys.executable


def _build_daemon_command(args: argparse.Namespace) -> list[str]:
    """Build the detached daemon launch command matching the current config."""
    absolute_notes_dir = config.get_absolute_path(config.notes_dir)
    absolute_graph_db_path = config.get_graph_db_path()
    command = [
        _get_windows_background_python() if os.name == "nt" else sys.executable,
        "-m",
        "parazettel_mcp.main",
        "--run-daemon",
        "--notes-dir",
        str(absolute_notes_dir),
        "--graph-db-path",
        str(absolute_graph_db_path),
        "--log-level",
        args.log_level,
        "--daemon-host",
        config.daemon_host,
        "--daemon-port",
        str(config.daemon_port),
        "--daemon-idle-timeout",
        str(config.daemon_idle_timeout_seconds),
    ]
    return command


def _build_windows_daemon_bootstrap_command(
    args: argparse.Namespace,
) -> list[str]:
    """Build a helper command that detaches the daemon from the MCP process tree.

    The daemon is spawned with ``CREATE_BREAKAWAY_FROM_JOB`` (0x01000000) so it
    leaves the launcher's Windows *job object*. Without this, the daemon stays in
    the job of whatever spawned it — a transient ``!``/restart shell, or a facade
    that later gets recycled — and the OS silently terminates it (no crash, no
    shutdown log) the moment that job closes, killing the shared daemon out from
    under every other client. Breakaway requires the job to permit it
    (``JOB_OBJECT_LIMIT_BREAKAWAY_OK``); if it does not, ``CreateProcess`` raises
    and we fall back to spawning without breakaway (the prior behaviour).
    """
    helper_code = (
        "import subprocess, sys\n"
        "base=("
        "getattr(subprocess,'DETACHED_PROCESS',0)|"
        "getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)|"
        "getattr(subprocess,'CREATE_NO_WINDOW',0)"
        ")\n"
        "kw=dict(stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "stdin=subprocess.DEVNULL,close_fds=True)\n"
        "try:\n"
        "    subprocess.Popen(sys.argv[1:],creationflags=base|0x01000000,**kw)\n"
        "except OSError:\n"
        "    subprocess.Popen(sys.argv[1:],creationflags=base,**kw)\n"
    )
    return [_get_windows_background_python(), "-c", helper_code, *_build_daemon_command(args)]


def _spawn_daemon_process(args: argparse.Namespace) -> subprocess.Popen:
    """Start the daemon in the background without blocking the MCP facade."""
    command = _build_daemon_command(args)
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        command = _build_windows_daemon_bootstrap_command(args)
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["creationflags"] = creationflags
        kwargs["startupinfo"] = startupinfo
    return subprocess.Popen(command, **kwargs)


def _wait_for_daemon_ready(client: DaemonRpcClient, timeout_seconds: float) -> None:
    """Poll the daemon health endpoint until it becomes ready or times out."""
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            client.health()
            return
        except DaemonUnavailableError as exc:
            last_error = exc
            time.sleep(_DAEMON_HEALTH_POLL_INTERVAL_SECONDS)
    if last_error is not None:
        raise last_error


_DAEMON_START_LOCK_TIMEOUT_SECONDS = 30.0
_DAEMON_START_LOCK_POLL_SECONDS = 0.2


@contextmanager
def _daemon_start_lock() -> Iterator[None]:
    """Hold an OS-level file lock across the daemon health-check-then-spawn.

    Without it, two MCP clients starting simultaneously can both see an
    unhealthy daemon and both spawn one; the loser of the port race dies to
    DEVNULL and the second client may fall back to read-only mode. Best-effort:
    if the lock cannot be acquired within the timeout (or the platform refuses
    file locking), the start proceeds unlocked rather than deadlocking.
    """
    lock_path = config.get_daemon_runtime_dir() / "daemon-start.lock"
    try:
        handle = open(lock_path, "a+b")
    except OSError:
        yield
        return

    locked = False
    deadline = time.time() + _DAEMON_START_LOCK_TIMEOUT_SECONDS
    try:
        if os.name == "nt":
            import msvcrt

            while time.time() < deadline:
                try:
                    # Non-blocking probe; retry until the holder releases it.
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(_DAEMON_START_LOCK_POLL_SECONDS)
        else:
            import fcntl

            while time.time() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    time.sleep(_DAEMON_START_LOCK_POLL_SECONDS)
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def ensure_daemon_running(args: argparse.Namespace) -> None:
    """Ensure the configured daemon is available, auto-starting it if needed."""
    client = DaemonRpcClient(config.get_daemon_base_url())
    try:
        client.health()
        return
    except DaemonUnavailableError:
        pass

    # Serialize the spawn across processes: whoever wins the lock starts the
    # daemon; everyone who waited re-checks health and finds it already up.
    with _daemon_start_lock():
        try:
            client.health()
            return
        except DaemonUnavailableError:
            _remove_daemon_pid_file()

        _spawn_daemon_process(args)
        try:
            _wait_for_daemon_ready(client, _DAEMON_START_TIMEOUT_SECONDS)
        except DaemonUnavailableError as exc:
            raise DaemonUnavailableError(
                "Failed to auto-start the Parazettel daemon. Start it manually "
                f"with: {config.format_daemon_start_command()}"
            ) from exc


def _resolve_session_host(
    start_pid: int,
    ppid_of: Dict[int, int],
    name_of: Dict[int, str],
    *,
    max_depth: int = 12,
) -> Optional[int]:
    """Walk parent links up from *start_pid* to the first non-python ancestor.

    An MCP facade is launched python(real) <- python(venv launcher) <- the
    Claude session host (claude.exe / a shell), so the host is the first ancestor
    that is not a python process. Returns its PID, or ``None`` when the chain
    breaks before reaching one — i.e. the process is already orphaned or the host
    can't be resolved (the caller then just skips the watchdog).
    """
    cur = start_pid
    for _ in range(max_depth):
        parent = ppid_of.get(cur)
        if not parent or parent not in name_of:
            return None
        # Keep this "name starts with python" launcher rule in sync with
        # scripts/restart_daemon.ps1 Test-FacadeOrphaned (the reaper walks the
        # same chain to decide orphan vs active).
        if not name_of[parent].startswith("python"):
            return parent
        cur = parent
    return None


def _snapshot_processes_windows():  # pragma: no cover - Windows-only, ctypes
    """Return ({pid: ppid}, {pid: exe_name_lower}) from the Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    # INVALID_HANDLE_VALUE is ((HANDLE)-1); compute it once here rather than
    # reconstruct it inline. It is local (not module-level) on purpose: ctypes is
    # imported lazily inside the Windows-only branches so non-Windows import paths
    # never pay for it.
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    ppid_of: Dict[int, int] = {}
    name_of: Dict[int, str] = {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    # CreateToolhelp32Snapshot returns INVALID_HANDLE_VALUE (-1) on failure, which
    # is truthy — guard it explicitly so we never walk an invalid handle, and
    # don't CloseHandle it.
    if not snap or snap == INVALID_HANDLE_VALUE:
        return ppid_of, name_of
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return ppid_of, name_of
        while True:
            pid = int(entry.th32ProcessID)
            ppid_of[pid] = int(entry.th32ParentProcessID)
            name_of[pid] = entry.szExeFile.decode("ascii", "ignore").lower()
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return ppid_of, name_of


def _start_parent_death_watchdog() -> None:
    """Self-exit this MCP facade when its Claude session host process dies.

    Claude spawns the facade and should close our stdin when the session ends —
    but if that EOF is ever missed, the facade lingers as an orphan holding memory
    and OS handles. This is a hard backstop: a daemon thread waits on the session
    host and force-exits the instant it disappears, so orphans never accumulate
    between restart-script reaps. Best-effort and silent on failure (the reap
    remains the fallback). Only the per-session facade calls this; the daemon is
    intentionally detached from its launcher and must NOT self-exit.
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            ppid_of, name_of = _snapshot_processes_windows()
            host_pid = _resolve_session_host(os.getpid(), ppid_of, name_of)
            if not host_pid:
                return
            SYNCHRONIZE = 0x00100000
            INFINITE = 0xFFFFFFFF
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            # A handle to the host's process OBJECT stays valid after it exits and
            # is immune to PID recycling (it refers to the original process).
            handle = k32.OpenProcess(SYNCHRONIZE, False, host_pid)
            if not handle:
                return

            def _wait() -> None:
                k32.WaitForSingleObject(handle, INFINITE)
                logger.warning(
                    "Claude session host (PID %s) exited; shutting down this MCP "
                    "facade to avoid orphaning it.",
                    host_pid,
                )
                os._exit(0)

            threading.Thread(
                target=_wait, name="pzk-parent-watchdog", daemon=True
            ).start()
        else:
            start_ppid = os.getppid()
            if start_ppid <= 1:
                return

            def _poll() -> None:
                # On POSIX a dead parent reparents us (getppid -> 1 / a subreaper).
                while True:
                    time.sleep(5.0)
                    if os.getppid() != start_ppid:
                        logger.warning(
                            "Parent process exited; shutting down this MCP facade "
                            "to avoid orphaning it."
                        )
                        os._exit(0)

            threading.Thread(
                target=_poll, name="pzk-parent-watchdog", daemon=True
            ).start()
    except Exception as exc:  # pragma: no cover - best-effort backstop
        logger.debug("Parent-death watchdog not started: %s", exc)


def main():
    """Run the Zettelkasten MCP server."""
    args = parse_args()
    update_config(args)

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    if getattr(args, "daemon_status", False):
        print(format_daemon_status(get_daemon_status()))
        return

    if getattr(args, "stop_daemon", False):
        print(stop_daemon())
        return

    # Ensure directories exist
    notes_dir = config.get_absolute_path(config.notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path = config.get_graph_db_path()
    logger.info(f"Using Kuzu graph database: {graph_db_path}")

    # Create and run the daemon or MCP facade
    daemon = None
    server = None
    try:
        if args.run_daemon:
            daemon = ParazettelDaemonServer(
                config.daemon_host,
                config.daemon_port,
                idle_timeout_seconds=config.daemon_idle_timeout_seconds,
            )
            try:
                # Claim the port before writing the shared PID file. On Windows
                # the bind is exclusive, so a second daemon fails here instead of
                # silently co-binding and stealing traffic.
                daemon.bind()
            except OSError as exc:
                # Lost the start race: another daemon already owns the port. Exit
                # cleanly and DO NOT touch the PID file — it belongs to the
                # winner (writing/removing it here is what corrupted status).
                logger.warning(
                    "Parazettel daemon port %s is already in use (%s); another "
                    "daemon is running. Exiting without serving.",
                    config.get_daemon_base_url(),
                    exc,
                )
                daemon.close()
                daemon = None
            else:
                logger.info(
                    "Starting Parazettel daemon at %s",
                    config.get_daemon_base_url(),
                )
                _write_daemon_pid_file(os.getpid())
                daemon.serve_forever()
        else:
            if config.backend_mode == "daemon":
                ensure_daemon_running(args)
            logger.info("Starting Zettelkasten MCP server")
            # Self-terminate if the Claude session that spawned this facade dies,
            # so it never lingers as an orphan (the restart-script reap is the
            # backstop). Facade-only — never the detached daemon.
            _start_parent_death_watchdog()
            server = ZettelkastenMcpServer()
            server.run(config.server_transport)
    except Exception as e:
        logger.error(f"Error running server: {e}")
        sys.exit(1)
    finally:
        if daemon is not None:
            daemon.shutdown()
            _remove_daemon_pid_file()
        if server is not None:
            server.close()


if __name__ == "__main__":
    main()
