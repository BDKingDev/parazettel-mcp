"""Cross-encoder reranker for dedup-on-create confirmation (optional, lazy).

BM25 over-flags near-duplicates because its score tracks shared vocabulary, not
whether two notes make the same claim. A cross-encoder reads *both* texts
together and scores their relationship directly, which separates true duplicates
from merely topically-adjacent notes far more cleanly. It runs only on the small
BM25 candidate set (<=5), so the cost is negligible.

The backend (``fastembed``'s ``TextCrossEncoder``) is imported lazily and the
model is loaded on first use, so nothing is required until embeddings are enabled
with a reranker model configured. :func:`build_reranker` returns ``None`` when the
feature is off.

The first-use model load is the ONE unbounded step in the per-session facade: it
acquires a ``filelock`` on the *shared* fastembed/HuggingFace model cache, and a
prior process that died mid-load can leave that lock wedged — which would hang the
session forever with no catchable exception. So the load runs on a dedicated
worker thread under a hard timeout (:attr:`FastEmbedReranker._load_timeout`); on
timeout :meth:`score` raises :class:`RerankerLoadTimeoutError` and the caller
surfaces a loud, actionable error instead of hanging. The load lifecycle is logged
at INFO (and failures at ERROR) so a hang is debuggable from the logs after the
fact.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

try:  # Protocol is stdlib on 3.8+, but guard for clarity.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

logger = logging.getLogger(__name__)


class RerankerError(RuntimeError):
    """The dedup reranker failed (load error or scoring error)."""


class RerankerLoadTimeoutError(RerankerError):
    """The cross-encoder model did not finish loading within the timeout.

    Almost always a wedged filelock on the shared fastembed/HuggingFace model
    cache (a prior process died mid-load), or a stuck GPU init.
    """


@runtime_checkable
class RerankerProvider(Protocol):
    """Minimal interface for a cross-encoder reranker backend."""

    #: Stable identifier (``backend:model``) for logging/diagnostics.
    model_id: str

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        """Return a relevance score per document for *query* (higher = closer)."""
        ...


class FastEmbedReranker:
    """Cross-encoder via ``fastembed``'s ``TextCrossEncoder`` (ONNX, no PyTorch).

    Lazily loads the model on first :meth:`score`, under a hard timeout. With
    ``device="cuda"`` it runs on the GPU (CUDA execution provider, with the
    CUDA/cuDNN DLLs preloaded from the nvidia-* wheels); otherwise it runs on CPU.
    """

    # ONNX Runtime execution providers per device (CPU is kept as a fallback).
    _DEVICE_PROVIDERS = {
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "gpu": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    }

    def __init__(
        self, model_name: str, *, device: str = "cpu", load_timeout: float = 45.0
    ) -> None:
        """Configure the reranker model name, execution device, and load timeout."""
        self.model_name = model_name
        self.model_id = f"fastembed-rerank:{model_name}"
        self._device = (device or "cpu").strip().lower()
        self._load_timeout = max(1.0, float(load_timeout))
        self._model = None  # lazy-loaded on first use
        # The (possibly wedged) load runs on a dedicated DAEMON thread so the
        # caller can walk away from it on timeout AND so a wedged load can never
        # block the facade's process exit (a non-daemon worker joined at exit
        # would zombie the process). One shared future means the load happens
        # exactly once and every concurrent waiter reuses it.
        self._load_lock = threading.Lock()
        self._load_thread: Optional[threading.Thread] = None
        self._load_future: "Optional[concurrent.futures.Future]" = None

    @staticmethod
    def _preload_cuda_dlls() -> None:
        """Best-effort load of the CUDA/cuDNN DLLs from the nvidia-* wheels."""
        try:
            import onnxruntime

            preload = getattr(onnxruntime, "preload_dlls", None)
            if preload is not None:
                preload()
        except Exception:  # pragma: no cover - preload is best-effort
            pass

    def _build_model(self):  # type: ignore[no-untyped-def]
        """Construct the TextCrossEncoder (runs on the load worker thread).

        This is the call that can wedge on the shared model-cache filelock, so it
        is deliberately isolated on its own thread and bracketed with timing logs.
        """
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise RerankerError(
                "The dedup reranker needs fastembed's cross-encoder support. "
                "Install the optional dependency group: "
                "pip install 'parazettel-mcp[embeddings-lite]'"
            ) from exc
        cache_path = os.getenv("FASTEMBED_CACHE_PATH") or "(fastembed default)"
        logger.info(
            "dedup reranker: loading cross-encoder model=%s device=%s cache=%s",
            self.model_name,
            self._device,
            cache_path,
        )
        started = time.perf_counter()
        kwargs: Dict[str, Any] = {"model_name": self.model_name}
        providers = self._DEVICE_PROVIDERS.get(self._device)
        if providers:
            self._preload_cuda_dlls()
            kwargs["providers"] = providers
        try:
            model = TextCrossEncoder(**kwargs)
        except TypeError:
            # Older fastembed without a `providers` kwarg: fall back to CPU.
            logger.info(
                "dedup reranker: fastembed has no 'providers' kwarg; loading on CPU"
            )
            model = TextCrossEncoder(model_name=self.model_name)
        logger.info(
            "dedup reranker: model loaded in %.2fs (model=%s device=%s)",
            time.perf_counter() - started,
            self.model_name,
            self._device,
        )
        return model

    def _run_load(self, future: "concurrent.futures.Future") -> None:
        """Build the model on the load worker, resolving *its own* future.

        Operates on the passed-in future (not ``self._load_future``) so that if a
        prior failed load is cleared and a new one started, this older worker can
        never resolve the newer future.
        """
        try:
            future.set_result(self._build_model())
        except BaseException as exc:  # noqa: BLE001 - relayed to the waiter
            future.set_exception(exc)

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        """Return the loaded model, loading it under a hard timeout on first use."""
        if self._model is not None:
            return self._model

        # Start (or join) the single shared load. The lock only guards starting
        # it; the (possibly long) wait happens outside the lock so concurrent
        # callers don't serialize.
        with self._load_lock:
            if self._model is not None:
                return self._model
            if self._load_future is None:
                logger.info(
                    "dedup reranker: starting model load (timeout=%.0fs)",
                    self._load_timeout,
                )
                future: "concurrent.futures.Future" = concurrent.futures.Future()
                self._load_future = future
                self._load_thread = threading.Thread(
                    target=self._run_load,
                    args=(future,),
                    name="pzk-reranker-load",
                    daemon=True,
                )
                self._load_thread.start()
            future = self._load_future

        waited_from = time.perf_counter()
        try:
            model = future.result(timeout=self._load_timeout)
        except concurrent.futures.TimeoutError as exc:
            # Leave the future running: the load is still in flight on the worker,
            # so a later call reuses it if it eventually completes — but THIS call
            # refuses to wait any longer and surfaces the stall.
            logger.error(
                "dedup reranker: model load did NOT complete within %.0fs — the "
                "shared fastembed/HuggingFace model-cache filelock (or GPU init) "
                "appears wedged. cache=%s. Background load left running.",
                self._load_timeout,
                os.getenv("FASTEMBED_CACHE_PATH") or "(fastembed default)",
            )
            raise RerankerLoadTimeoutError(
                f"dedup reranker model load exceeded {self._load_timeout:.0f}s "
                "(likely a stuck fastembed/HuggingFace model-cache lock left by a "
                "process that died mid-load, or a stalled GPU init). Restart the "
                "daemon/session to clear it, or disable the reranker by setting "
                "PARAZETTEL_DEDUP_RERANK_MODEL=''."
            ) from exc
        except RerankerError:
            # Genuine load failure (missing dep / bad model): let a later call
            # retry from scratch rather than caching a one-off failure.
            self._clear_failed_load(future)
            raise
        except Exception as exc:
            self._clear_failed_load(future)
            logger.error("dedup reranker: model load failed: %s", exc, exc_info=True)
            raise RerankerError(f"dedup reranker model load failed: {exc}") from exc
        self._model = model
        logger.debug(
            "dedup reranker: model ready (waited %.2fs on this call)",
            time.perf_counter() - waited_from,
        )
        return self._model

    def _clear_failed_load(self, future: "concurrent.futures.Future") -> None:
        """Drop a failed load so the next call retries from scratch (idempotent)."""
        with self._load_lock:
            if self._load_future is future:
                self._load_future = None
                self._load_thread = None

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        """Score each document against *query* with the cross-encoder."""
        docs = list(documents)
        if not docs:
            return []
        model = self._ensure_model()
        started = time.perf_counter()
        scores = [float(s) for s in model.rerank(query, docs)]
        logger.debug(
            "dedup reranker: scored %d doc(s) in %.3fs (scores=%s)",
            len(docs),
            time.perf_counter() - started,
            [round(s, 3) for s in scores],
        )
        return scores


def build_reranker(config) -> Optional[RerankerProvider]:  # type: ignore[no-untyped-def]
    """Construct the dedup reranker, or ``None`` when it is disabled.

    Off unless embeddings are enabled *and* a reranker model is configured. Note:
    this only constructs the provider — the model is loaded lazily on first use.
    """
    if not getattr(config, "embedding_enabled", False):
        return None
    model = (getattr(config, "dedup_rerank_model", "") or "").strip()
    if not model:
        return None
    device = getattr(config, "embedding_device", "cpu")
    load_timeout = float(
        getattr(config, "dedup_rerank_load_timeout_seconds", 45.0) or 45.0
    )
    return FastEmbedReranker(model, device=device, load_timeout=load_timeout)
