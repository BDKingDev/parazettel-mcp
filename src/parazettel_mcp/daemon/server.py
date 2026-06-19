"""Local HTTP daemon that owns the Parazettel data services."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Any, Dict, Optional, Set

from parazettel_mcp.config import config
from parazettel_mcp.daemon.codec import decode_value, encode_value
from parazettel_mcp.services.search_service import SearchService
from parazettel_mcp.services.zettel_service import ZettelService

logger = logging.getLogger(__name__)
_IDLE_POLL_INTERVAL_SECONDS = 1.0
# Log this process's memory every N RPC requests so a slow climb over a long
# session is visible in the daemon log — which distinguishes a true leak
# (monotonic climb) from the Kuzu buffer pool's bounded high-water plateau.
_MEMORY_LOG_EVERY_N_REQUESTS = 200


def _process_memory_mb() -> "Optional[tuple[float, Optional[float]]]":
    """Return (working_set_MB, commit_MB) for this process, or None.

    Windows: Win32 GetProcessMemoryInfo (working set + private commit /
    PagefileUsage). POSIX: resource.getrusage RSS (no commit figure). Best-effort
    — any failure returns None so memory logging never affects request handling.
    """
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            # Declare signatures so 64-bit HANDLEs aren't truncated through
            # ctypes' default c_int return/args.
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetCurrentProcess.argtypes = []
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_PMC),
                wintypes.DWORD,
            ]
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            handle = kernel32.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return None
            return (
                counters.WorkingSetSize / 1048576.0,
                counters.PagefileUsage / 1048576.0,
            )
        import resource
        import sys

        # ru_maxrss is KB on Linux but BYTES on macOS — convert to MB per platform.
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / 1048576.0 if sys.platform == "darwin" else rss / 1024.0
        return (rss_mb, None)
    except Exception:  # pragma: no cover - diagnostics must never raise
        return None


class _ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to co-bind an already-in-use port.

    The stdlib ``HTTPServer`` sets ``allow_reuse_address = True`` (``SO_REUSEADDR``).
    On POSIX that only permits reusing a port stuck in ``TIME_WAIT`` — harmless,
    and kept so the daemon can restart promptly. On **Windows**, ``SO_REUSEADDR``
    is far more permissive: a second socket may bind a port that is *already
    actively bound*, silently stealing its incoming connections. Two daemons
    could then co-bind the daemon port — the original becomes a zombie (often
    fallen back to a read-only graph) and clients flap between the two.

    So on Windows we disable ``SO_REUSEADDR`` and set ``SO_EXCLUSIVEADDRUSE``,
    making a second bind fail loudly (``WinError 10048``). The losing daemon then
    exits cleanly — exactly the "loser of the start race dies" behaviour the
    daemon-start lock already assumes (which ``SO_REUSEADDR`` had quietly broken
    on Windows). POSIX behaviour is unchanged.
    """

    # Keep POSIX TIME_WAIT reuse; drop the address-stealing reuse on Windows.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(
                    socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                )
            except OSError:  # pragma: no cover - best-effort hardening
                pass
        super().server_bind()


def _is_loopback_host(host: str) -> bool:
    """Return True when *host* resolves to a local loopback address."""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class DaemonBusyError(RuntimeError):
    """Raised when the daemon is in maintenance mode and cannot serve a request."""


ALLOWED_SERVICE_METHODS: Dict[str, Set[str]] = {
    "zettel_service": {
        "create_note",
        "get_note",
        "get_note_by_title",
        "update_note",
        "delete_note",
        "get_all_notes",
        "search_notes",
        "get_notes_by_tag",
        "add_tag_to_note",
        "remove_tag_from_note",
        "get_all_tags",
        "create_link",
        "remove_link",
        "get_linked_notes",
        "rebuild_index",
        "check_consistency",
        "export_note",
        "find_similar_notes",
        "find_similar_to_text",
        "suggest_tags",
        "suggest_areas",
        "record_retrieval",
        "get_retrieval_signals",
        "create_task",
        "update_task",
        "update_task_status",
        "get_tasks",
        "get_todays_tasks",
        "create_project_note",
        "get_parent_project",
        "get_subprojects",
        "get_project_tasks",
        "get_project_notes",
        "get_linked_projects",
        "create_area_note",
        "get_reminders",
    },
    "search_service": {
        "search_by_text",
        "search_by_tag",
        "search_by_link",
        "find_orphaned_notes",
        "find_central_notes",
        "find_notes_by_date_range",
        "find_similar_notes",
        "search_combined",
    },
}


class ParazettelDaemonServer:
    """Long-lived local daemon that owns Parazettel data services."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        zettel_service: Optional[ZettelService] = None,
        search_service: Optional[SearchService] = None,
        idle_timeout_seconds: Optional[float] = None,
    ):
        if not _is_loopback_host(host):
            raise ValueError(
                "Parazettel daemon only supports loopback hosts. "
                f"Received: {host}"
            )
        self.host = host
        self.port = port
        self.zettel_service = zettel_service or ZettelService()
        self.search_service = search_service or SearchService(self.zettel_service)
        self._initialized = False
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._closed = False
        self._last_activity = time.monotonic()
        self._idle_timeout_seconds = (
            idle_timeout_seconds
            if idle_timeout_seconds is not None
            else config.daemon_idle_timeout_seconds
        )
        self._shutdown_event = threading.Event()
        self._idle_monitor_thread: Optional[threading.Thread] = None
        self._maintenance_state_lock = threading.Lock()
        self._maintenance_reason: Optional[str] = None
        self._request_count = 0
        self._request_count_lock = threading.Lock()

    @property
    def server_address(self) -> tuple[str, int]:
        if self._httpd is not None:
            return self._httpd.server_address
        return self.host, self.port

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"

    def initialize(self) -> None:
        """Initialize underlying services once."""
        if self._initialized:
            return
        self.zettel_service.initialize()
        self.search_service.initialize()
        self._initialized = True

    def _mark_activity(self) -> None:
        """Record the latest daemon activity time."""
        self._last_activity = time.monotonic()

    def _record_request_memory(self) -> None:
        """Count an RPC request; every N, log this process's memory.

        Gives an after-the-fact view of whether the long-lived daemon's memory is
        plateauing (bounded buffer-pool high-water) or genuinely climbing (a leak)
        without needing an external profiler. Best-effort and never raises.
        """
        with self._request_count_lock:
            self._request_count += 1
            count = self._request_count
        if count % _MEMORY_LOG_EVERY_N_REQUESTS != 0:
            return
        mem = _process_memory_mb()
        if mem is None:
            return
        working_set_mb, commit_mb = mem
        if commit_mb is not None:
            logger.info(
                "daemon memory after %d requests: working_set=%.0f MB, "
                "commit=%.0f MB",
                count,
                working_set_mb,
                commit_mb,
            )
        else:
            logger.info(
                "daemon memory after %d requests: rss=%.0f MB",
                count,
                working_set_mb,
            )

    def _start_idle_monitor(self) -> None:
        """Start background idle shutdown monitoring when configured."""
        if self._idle_timeout_seconds <= 0 or self._idle_monitor_thread is not None:
            return

        def monitor() -> None:
            while not self._shutdown_event.wait(_IDLE_POLL_INTERVAL_SECONDS):
                if self._httpd is None:
                    return
                idle_for = time.monotonic() - self._last_activity
                if idle_for >= self._idle_timeout_seconds:
                    logger.info(
                        "Shutting down Parazettel daemon after %.1fs of inactivity",
                        idle_for,
                    )
                    self.shutdown()
                    return

        self._idle_monitor_thread = threading.Thread(
            target=monitor,
            name="parazettel-daemon-idle-monitor",
            daemon=True,
        )
        self._idle_monitor_thread.start()

    def bind(self) -> None:
        """Bind the daemon's listening socket (idempotent, fail-fast).

        Separated from :meth:`serve_forever` so a caller can claim the port
        *before* writing the shared PID file and before the (heavier) service
        warmup. Raises ``OSError`` when the port is already in use — on Windows
        that is the decisive "another daemon owns this port" signal, because the
        socket binds exclusively (see :class:`_ExclusiveThreadingHTTPServer`).
        Callers treat that as losing the start race and exit without touching
        the winner's PID file.
        """
        if self._httpd is not None:
            return
        self._httpd = _ExclusiveThreadingHTTPServer(
            (self.host, self.port),
            self._build_handler(),
        )

    def serve_forever(self) -> None:
        """Start the daemon HTTP server and block until shutdown."""
        # Bind first (cheap; fails fast if another daemon owns the port) so a
        # loser does not pay the service-init cost before discovering it lost.
        self.bind()
        try:
            self.initialize()
            self._closed = False
            self._shutdown_event.clear()
            self._mark_activity()
            self._start_idle_monitor()
            logger.info("Starting Parazettel daemon at %s", self.base_url)
            self._httpd.serve_forever()
        except BaseException:
            # Warmup (initialize) or serving failed: don't leave the port bound
            # but dead — otherwise callers/tests would see a live-looking
            # server_address for a server that never ran. Close + clear it.
            self._close_socket()
            raise
        finally:
            self.close()

    def _close_socket(self) -> None:
        """Close the listening socket and clear the handle (best-effort)."""
        if self._httpd is not None:
            try:
                self._httpd.server_close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            self._httpd = None

    def shutdown(self) -> None:
        """Stop the running HTTP server."""
        self._shutdown_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._close_socket()
        self.close()

    def close(self) -> None:
        """Release daemon-owned services."""
        if self._closed:
            return
        self._closed = True
        self.zettel_service.close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def _ensure_loopback_client(self) -> bool:
                client_host = self.client_address[0]
                if _is_loopback_host(client_host):
                    return True
                self._send_json(
                    403,
                    {
                        "ok": False,
                        "error": {
                            "type": "Forbidden",
                            "message": "Parazettel daemon only accepts loopback clients.",
                        },
                    },
                )
                return False

            def do_GET(self) -> None:
                if not self._ensure_loopback_client():
                    return
                if self.path == "/health":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "version": config.server_version,
                            "mode": "daemon",
                            "graph_writable": not daemon.zettel_service.repository.read_only,
                            "pid": os.getpid(),
                            "port": daemon.server_address[1],
                            "idle_timeout_seconds": daemon._idle_timeout_seconds,
                            "pid_file": str(config.get_daemon_pid_file()),
                            "maintenance_reason": daemon._maintenance_reason,
                        },
                    )
                    return
                self._send_json(404, {"ok": False, "error": {"type": "NotFound", "message": "Unknown path"}})

            def do_POST(self) -> None:
                if not self._ensure_loopback_client():
                    return
                daemon._mark_activity()
                if self.path == "/shutdown":
                    self._send_json(
                        200,
                        {"ok": True, "message": "Shutting down Parazettel daemon."},
                    )
                    threading.Thread(
                        target=daemon.shutdown,
                        name="parazettel-daemon-shutdown",
                        daemon=True,
                    ).start()
                    return

                segments = self.path.strip("/").split("/")
                if len(segments) != 3 or segments[0] != "rpc":
                    self._send_json(
                        404,
                        {"ok": False, "error": {"type": "NotFound", "message": "Unknown path"}},
                    )
                    return

                _, service_name, method_name = segments
                if method_name not in ALLOWED_SERVICE_METHODS.get(service_name, set()):
                    self._send_json(
                        404,
                        {
                            "ok": False,
                            "error": {
                                "type": "NotFound",
                                "message": f"Unsupported RPC method: {service_name}.{method_name}",
                            },
                        },
                    )
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    args = decode_value(payload.get("args", []))
                    kwargs = decode_value(payload.get("kwargs", {}))
                    result = daemon._invoke(service_name, method_name, args, kwargs)
                except ValueError as exc:
                    self._send_error_json(400, exc)
                    return
                except DaemonBusyError as exc:
                    self._send_error_json(503, exc)
                    return
                except Exception as exc:  # noqa: BLE001
                    status = 400 if exc.__class__.__name__.endswith("Error") else 500
                    self._send_error_json(status, exc)
                    return

                self._send_json(200, {"ok": True, "result": encode_value(result)})
                daemon._record_request_memory()

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("Parazettel daemon HTTP: " + format, *args)

            def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_error_json(self, status: int, exc: Exception) -> None:
                logger.error("Parazettel daemon error: %s", exc, exc_info=True)
                self._send_json(
                    status,
                    {
                        "ok": False,
                        "error": {
                            "type": exc.__class__.__name__,
                            "message": str(exc),
                        },
                    },
                )

        return Handler

    def _invoke(
        self,
        service_name: str,
        method_name: str,
        args: list[Any],
        kwargs: Dict[str, Any],
    ) -> Any:
        if method_name == "rebuild_index":
            return self._invoke_with_maintenance_mode(
                "rebuild_index",
                lambda: self.zettel_service.rebuild_index(*args, **kwargs),
            )
        if self._maintenance_reason is not None:
            raise DaemonBusyError(
                f"Parazettel daemon is busy with {self._maintenance_reason}. "
                "Try again after maintenance completes."
            )
        service = {
            "zettel_service": self.zettel_service,
            "search_service": self.search_service,
        }[service_name]
        method = getattr(service, method_name)
        return method(*args, **kwargs)

    def _invoke_with_maintenance_mode(
        self, reason: str, callback: Any
    ) -> Any:
        with self._maintenance_state_lock:
            if self._maintenance_reason is not None:
                raise DaemonBusyError(
                    f"Parazettel daemon is busy with {self._maintenance_reason}. "
                    "Try again after maintenance completes."
                )
            self._maintenance_reason = reason
        try:
            return callback()
        finally:
            with self._maintenance_state_lock:
                self._maintenance_reason = None
