"""Local HTTP daemon that owns the Parazettel data services."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Set

from parazettel_mcp.config import config
from parazettel_mcp.daemon.codec import decode_value, encode_value
from parazettel_mcp.services.search_service import SearchService
from parazettel_mcp.services.zettel_service import ZettelService

logger = logging.getLogger(__name__)

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
    ):
        self.host = host
        self.port = port
        self.zettel_service = zettel_service or ZettelService()
        self.search_service = search_service or SearchService(self.zettel_service)
        self._initialized = False
        self._httpd: Optional[ThreadingHTTPServer] = None

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

    def serve_forever(self) -> None:
        """Start the daemon HTTP server and block until shutdown."""
        self.initialize()
        self._httpd = ThreadingHTTPServer(
            (self.host, self.port),
            self._build_handler(),
        )
        logger.info("Starting Parazettel daemon at %s", self.base_url)
        try:
            self._httpd.serve_forever()
        finally:
            self.close()

    def shutdown(self) -> None:
        """Stop the running HTTP server."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.close()

    def close(self) -> None:
        """Release daemon-owned services."""
        self.zettel_service.close()

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "version": config.server_version,
                            "mode": "daemon",
                            "graph_writable": not daemon.zettel_service.repository.read_only,
                        },
                    )
                    return
                self._send_json(404, {"ok": False, "error": {"type": "NotFound", "message": "Unknown path"}})

            def do_POST(self) -> None:
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
        service = {
            "zettel_service": self.zettel_service,
            "search_service": self.search_service,
        }[service_name]
        method = getattr(service, method_name)
        return method(*args, **kwargs)
