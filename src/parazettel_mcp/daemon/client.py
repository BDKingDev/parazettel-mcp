"""HTTP client for the Parazettel daemon."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib import error, request

from parazettel_mcp.daemon.codec import decode_value, encode_value
from parazettel_mcp.models.graph_db import GraphDatabaseReadOnlyError
from parazettel_mcp.services.reranker import RerankerError, RerankerLoadTimeoutError


class DaemonUnavailableError(RuntimeError):
    """Raised when the configured daemon cannot be reached."""


class DaemonBusyError(RuntimeError):
    """Raised when the daemon is in maintenance mode and cannot serve a request.

    Defined here (rather than in the daemon server) so the facade-side client can
    reconstruct it from a remote 503 response and callers can distinguish a
    transient "vault is rebuilding" condition from a real crash. The daemon server
    imports this same class so both sides agree on the type name.
    """


ERROR_REGISTRY = {
    "GraphDatabaseReadOnlyError": GraphDatabaseReadOnlyError,
    # A maintenance-mode rejection (e.g. mid index rebuild). Reconstructed as its
    # own type so the facade surfaces "try again shortly", not a crash-looking
    # generic RuntimeError.
    "DaemonBusyError": DaemonBusyError,
    # The dedup reranker now runs in the daemon; relay its failures back to the
    # facade as the SAME type so _rerank_confirm / ingest_batch can recognize and
    # surface them (a wedged/failed rerank must fail loud, not silently degrade).
    "RerankerError": RerankerError,
    "RerankerLoadTimeoutError": RerankerLoadTimeoutError,
    "RuntimeError": RuntimeError,
    "ValueError": ValueError,
    "FileNotFoundError": FileNotFoundError,
    "IOError": IOError,
    "OSError": OSError,
}


class DaemonRpcClient:
    """Simple JSON-over-HTTP client for the local Parazettel daemon."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 5.0,
        on_unavailable: Optional[Any] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        # Optional zero-arg callback invoked when the daemon is unreachable. If it
        # returns truthy (it (re)started the daemon), the request is retried ONCE.
        # Lets a recycled or idle-shut-down daemon recover transparently mid-
        # session instead of failing the caller's tool call.
        self._on_unavailable = on_unavailable

    def health(self) -> Dict[str, Any]:
        """Fetch daemon health and status information."""
        return self._request_json("GET", "/health")

    def shutdown(self) -> Dict[str, Any]:
        """Ask the daemon to shut down cleanly."""
        return self._request_json("POST", "/shutdown")

    def call(
        self,
        service: str,
        method: str,
        *,
        args: Optional[list[Any]] = None,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Invoke a daemon service method and decode the structured result."""
        payload = {
            "args": encode_value(args or []),
            "kwargs": encode_value(kwargs or {}),
        }
        response = self._request_json(
            "POST",
            f"/rpc/{service}/{method}",
            payload=payload,
        )
        return decode_value(response["result"])

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Dict[str, Any]] = None,
        allow_restart: bool = True,
    ) -> Dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(body or str(exc)) from exc
            self._raise_remote_error(payload.get("error", {}))
        except (error.URLError, OSError) as exc:
            # The daemon may have recycled or idle-shut-down. Try to (re)start it
            # once and replay the request so the caller's tool call still succeeds.
            if allow_restart and self._try_restart_daemon():
                return self._request_json(
                    method, path, payload=payload, allow_restart=False
                )
            raise DaemonUnavailableError(
                "Parazettel daemon is unavailable. Start it with: "
                "python -m parazettel_mcp.main --run-daemon "
                "(or restart your MCP client, which auto-starts the daemon; "
                "check status with python -m parazettel_mcp.main --daemon-status). "
                "To run without the daemon, set PARAZETTEL_BACKEND_MODE=direct."
            ) from exc

        payload = json.loads(body)
        if payload.get("ok") is False:
            self._raise_remote_error(payload.get("error", {}))
        return payload

    def _try_restart_daemon(self) -> bool:
        """Run the unavailable-callback to (re)start the daemon; False on any error."""
        if self._on_unavailable is None:
            return False
        try:
            return bool(self._on_unavailable())
        except Exception:  # pragma: no cover - recovery is best-effort
            return False

    def _raise_remote_error(self, error_payload: Dict[str, Any]) -> None:
        error_type = error_payload.get("type", "RuntimeError")
        message = error_payload.get("message", "Unknown daemon error")
        exc_type = ERROR_REGISTRY.get(error_type, RuntimeError)
        raise exc_type(message)


class RemoteServiceProxy:
    """Dynamic proxy exposing a service-like API over daemon RPC."""

    def __init__(self, rpc_client: DaemonRpcClient, service_name: str):
        self._rpc_client = rpc_client
        self._service_name = service_name

    def __getattr__(self, item: str) -> Any:
        def remote_method(*args: Any, **kwargs: Any) -> Any:
            return self._rpc_client.call(
                self._service_name,
                item,
                args=list(args),
                kwargs=kwargs,
            )

        return remote_method
