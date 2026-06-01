"""Local HTTP daemon that owns the Parazettel data services."""

from __future__ import annotations

import json
import logging
import os
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

    def serve_forever(self) -> None:
        """Start the daemon HTTP server and block until shutdown."""
        self.initialize()
        self._httpd = ThreadingHTTPServer(
            (self.host, self.port),
            self._build_handler(),
        )
        self._closed = False
        self._shutdown_event.clear()
        self._mark_activity()
        self._start_idle_monitor()
        logger.info("Starting Parazettel daemon at %s", self.base_url)
        try:
            self._httpd.serve_forever()
        finally:
            self.close()

    def shutdown(self) -> None:
        """Stop the running HTTP server."""
        self._shutdown_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
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
