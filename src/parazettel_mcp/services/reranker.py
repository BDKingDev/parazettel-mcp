"""Cross-encoder reranker for dedup-on-create confirmation (optional, lazy).

BM25 over-flags near-duplicates because its score tracks shared vocabulary, not
whether two notes make the same claim. A cross-encoder reads *both* texts
together and scores their relationship directly, which separates true duplicates
from merely topically-adjacent notes far more cleanly. It runs only on the small
BM25 candidate set (<=5), so the cost is negligible.

The backend (``fastembed``'s ``TextCrossEncoder``) is imported lazily and the
model is loaded on first use, so nothing is required until embeddings are enabled
with a reranker model configured. :func:`build_reranker` returns ``None`` when the
feature is off, and callers fall back to the BM25-only dedup behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

try:  # Protocol is stdlib on 3.8+, but guard for clarity.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

logger = logging.getLogger(__name__)


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

    Lazily loads the model on first :meth:`score`. With ``device="cuda"`` it runs
    on the GPU (CUDA execution provider, with the CUDA/cuDNN DLLs preloaded from
    the nvidia-* wheels); otherwise it runs on CPU.
    """

    # ONNX Runtime execution providers per device (CPU is kept as a fallback).
    _DEVICE_PROVIDERS = {
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "gpu": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    }

    def __init__(self, model_name: str, *, device: str = "cpu") -> None:
        """Configure the reranker model name and execution device."""
        self.model_name = model_name
        self.model_id = f"fastembed-rerank:{model_name}"
        self._device = (device or "cpu").strip().lower()
        self._model = None  # lazy-loaded on first use

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

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        """Lazily load the TextCrossEncoder, or explain the missing dependency."""
        if self._model is None:
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except ImportError as exc:
                raise RuntimeError(
                    "The dedup reranker needs fastembed's cross-encoder support. "
                    "Install the optional dependency group: "
                    "pip install 'parazettel-mcp[embeddings-lite]'"
                ) from exc
            kwargs: Dict[str, Any] = {"model_name": self.model_name}
            providers = self._DEVICE_PROVIDERS.get(self._device)
            if providers:
                self._preload_cuda_dlls()
                kwargs["providers"] = providers
            try:
                self._model = TextCrossEncoder(**kwargs)
            except TypeError:
                # Older fastembed without a `providers` kwarg: fall back to CPU.
                self._model = TextCrossEncoder(model_name=self.model_name)
        return self._model

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        """Score each document against *query* with the cross-encoder."""
        docs = list(documents)
        if not docs:
            return []
        model = self._ensure_model()
        return [float(s) for s in model.rerank(query, docs)]


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
    return FastEmbedReranker(model, device=device)
