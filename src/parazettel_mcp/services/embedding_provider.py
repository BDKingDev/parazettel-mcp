"""Embedding providers for semantic search (optional, disabled by default).

This module abstracts *how* note text becomes a dense vector so the rest of the
system (indexing, search, similarity, dedup) depends only on a small
:class:`EmbeddingProvider` protocol — never on a specific model or runtime.

Providers (install tiers)
-------------------------
- ``fastembed`` (lite, default): local on-device embeddings via ``fastembed``
  (ONNX Runtime, no PyTorch). Light footprint; install with
  ``pip install 'parazettel-mcp[embeddings-lite]'``. Default model bge-small.
- ``sentence-transformers`` (full): local embeddings via ``sentence-transformers``
  (PyTorch). Heavier, widest model support incl. EmbeddingGemma; install with
  ``pip install 'parazettel-mcp[embeddings]'``.
- ``hash``: a deterministic, dependency-free pseudo-embedding for tests and
  smoke checks. NOT semantically meaningful — never use it for real retrieval.

All backends are lazy-imported, so a tier's dependency is only required when
embeddings are enabled with that provider.

Embeddings are off by default: :func:`build_embedding_provider` returns ``None``
when ``config.embedding_enabled`` is false, and callers fall back to the existing
BM25 / lexical behaviour.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from typing import Any, Dict, List, Optional, Sequence

try:  # Protocol is stdlib on 3.8+, but guard for clarity.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal interface every embedding backend implements."""

    #: Output dimensionality of the vectors this provider produces.
    dim: int
    #: Stable identifier (``provider:model:dim``) used to detect when stored
    #: embeddings were produced by a different model and need recomputing.
    model_id: str

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed note bodies for storage/indexing."""
        ...

    def embed_query(self, text: str) -> List[float]:
        """Embed a search query (may use a different task prompt than documents)."""
        ...


def _l2_normalize(vec: List[float]) -> List[float]:
    """Return *vec* scaled to unit length (cosine-ready); zero vectors unchanged."""
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class HashEmbeddingProvider:
    """Deterministic, dependency-free pseudo-embeddings for tests and smoke runs.

    Maps text to a fixed-dimension unit vector by hashing. Stable for the same
    input but carries no semantic meaning — do not use for real retrieval.
    """

    def __init__(self, dim: int = 768) -> None:
        """Set the output dimensionality (must be positive) and the model id."""
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.dim = dim
        self.model_id = f"hash:deterministic:{dim}"

    def _embed(self, text: str) -> List[float]:
        """Hash *text* into a deterministic unit vector of length ``dim``."""
        out: List[float] = []
        counter = 0
        # Expand a stream of SHA-256 digests into `dim` floats in [-1, 1).
        while len(out) < self.dim:
            digest = hashlib.sha256(f"{counter}:{text}".encode("utf-8")).digest()
            for i in range(0, len(digest), 4):
                if len(out) >= self.dim:
                    break
                (u,) = struct.unpack("<I", digest[i : i + 4])
                out.append((u / 2**31) - 1.0)
            counter += 1
        return _l2_normalize(out)

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed each document deterministically (order-preserving)."""
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        """Embed a query with the same deterministic hash as documents."""
        return self._embed(text)


class SentenceTransformerProvider:
    """Local on-device embeddings via the ``sentence-transformers`` package.

    The package (and PyTorch) are imported lazily so they are only required when
    embeddings are actually enabled with this provider. Document and query text
    are embedded with task-appropriate prompts where the model defines them
    (e.g. EmbeddingGemma's ``query`` / ``document`` prompts), which materially
    affects retrieval quality. Vectors are truncated to ``dim`` (supporting
    Matryoshka models) and then L2-normalized so cosine works correctly.
    """

    def __init__(
        self,
        model_name: str,
        dim: int,
        *,
        normalize: bool = True,
        batch_size: int = 16,
        device: str = "cpu",
    ) -> None:
        """Configure the model name, output dim, normalization, and batch size."""
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.model_name = model_name
        self.dim = dim
        self.model_id = f"sentence-transformers:{model_name}:{dim}"
        self._normalize = normalize
        self._batch_size = max(1, int(batch_size))
        self._device = (device or "cpu").strip().lower()
        self._model = None  # lazy-loaded on first use

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        """Lazily load the SentenceTransformer model, or explain the missing extra."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "Embeddings are enabled with provider 'sentence-transformers' "
                    "but the package is not installed. Install the optional "
                    "dependency group: pip install 'parazettel-mcp[embeddings]'"
                ) from exc
            st_kwargs: Dict[str, Any] = {}
            # "auto" lets sentence-transformers pick (CUDA if a CUDA torch is
            # present); "cuda"/"gpu" force the GPU; "cpu" pins to CPU.
            if self._device in ("cuda", "gpu"):
                st_kwargs["device"] = "cuda"
            elif self._device == "cpu":
                st_kwargs["device"] = "cpu"
            self._model = SentenceTransformer(self.model_name, **st_kwargs)
        return self._model

    def _has_prompt(self, name: str) -> bool:
        """Return whether the loaded model defines a task prompt named *name*."""
        prompts = getattr(self._model, "prompts", None) or {}
        return name in prompts

    def _encode(self, texts: Sequence[str], *, prompt: str) -> List[List[float]]:
        """Encode texts with the given task prompt, truncate to ``dim``, normalize."""
        model = self._ensure_model()
        kwargs: Dict[str, Any] = {
            "convert_to_numpy": True,
            "batch_size": self._batch_size,
        }
        if self._has_prompt(prompt):
            kwargs["prompt_name"] = prompt
        vectors = model.encode(list(texts), **kwargs)
        out: List[List[float]] = []
        for row in vectors:
            # Truncate first (Matryoshka), then normalize for cosine.
            v = [float(x) for x in row[: self.dim]]
            if len(v) != self.dim:
                raise ValueError(
                    f"Model {self.model_name!r} produced {len(v)} dims but "
                    f"embedding_dim={self.dim}; lower embedding_dim or pick a "
                    "model whose native dimension is >= embedding_dim."
                )
            out.append(_l2_normalize(v) if self._normalize else v)
        return out

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed note bodies using the model's ``document`` task prompt."""
        if not texts:
            return []
        return self._encode(texts, prompt="document")

    def embed_query(self, text: str) -> List[float]:
        """Embed a search query using the model's ``query`` task prompt."""
        return self._encode([text], prompt="query")[0]


class FastEmbedProvider:
    """Lightweight local embeddings via ``fastembed`` (ONNX Runtime, no PyTorch).

    The ``fastembed`` package is imported lazily so it is only required when
    embeddings are enabled with this provider. Vectors are truncated to ``dim``
    and L2-normalized so cosine works correctly. Where the model defines query
    vs. passage prefixes, fastembed's ``query_embed`` / ``passage_embed`` are
    used so query and document text are embedded asymmetrically.
    """

    # ONNX Runtime execution providers per device. CUDA needs the GPU extra
    # (onnxruntime-gpu via [embeddings-lite-gpu]); CPU is kept as a fallback so a
    # missing GPU runtime degrades to CPU instead of erroring.
    _DEVICE_PROVIDERS = {
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "gpu": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    }

    def __init__(
        self,
        model_name: str,
        dim: int,
        *,
        normalize: bool = True,
        batch_size: int = 16,
        device: str = "cpu",
    ) -> None:
        """Configure the model name, output dim, normalization, and batch size."""
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.model_name = model_name
        self.dim = dim
        self.model_id = f"fastembed:{model_name}:{dim}"
        self._normalize = normalize
        self._batch_size = max(1, int(batch_size))
        self._device = (device or "cpu").strip().lower()
        self._model = None  # lazy-loaded on first use

    @staticmethod
    def _preload_cuda_dlls() -> None:
        """Best-effort load of the CUDA/cuDNN DLLs shipped in the nvidia-* wheels.

        onnxruntime-gpu locates its CUDA runtime from the installed ``nvidia-*``
        pip packages only after an explicit preload; this is a no-op on a
        CPU-only onnxruntime build, so it is always safe to call.
        """
        try:
            import onnxruntime

            preload = getattr(onnxruntime, "preload_dlls", None)
            if preload is not None:
                preload()
        except Exception:  # pragma: no cover - preload is best-effort
            pass

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        """Lazily load the fastembed model, or explain the missing extra."""
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError(
                    "Embeddings are enabled with provider 'fastembed' but the "
                    "package is not installed. Install the optional dependency "
                    "group: pip install 'parazettel-mcp[embeddings-lite]' "
                    "(or [embeddings-lite-gpu] for CUDA)."
                ) from exc
            kwargs: Dict[str, Any] = {"model_name": self.model_name}
            providers = self._DEVICE_PROVIDERS.get(self._device)
            if providers:
                self._preload_cuda_dlls()
                kwargs["providers"] = providers
            try:
                self._model = TextEmbedding(**kwargs)
            except Exception as exc:
                # A CUDA providers list fails on a CPU-only onnxruntime build or
                # when the GPU runtime libs are missing. Honour the documented
                # "falls back to CPU" contract by retrying without `providers`
                # rather than disabling embeddings entirely. Re-raise if we were
                # already on the CPU path (no providers) — that's a real error.
                if "providers" not in kwargs:
                    raise
                logger.warning(
                    "fastembed CUDA init failed (%s); falling back to the CPU "
                    "execution provider.",
                    exc,
                )
                kwargs.pop("providers")
                self._model = TextEmbedding(**kwargs)
        return self._model

    def _finalize(self, raw) -> List[List[float]]:  # type: ignore[no-untyped-def]
        """Truncate each raw vector to ``dim`` and L2-normalize for cosine."""
        out: List[List[float]] = []
        for row in raw:
            v = [float(x) for x in list(row)[: self.dim]]
            if len(v) != self.dim:
                raise ValueError(
                    f"Model {self.model_name!r} produced {len(v)} dims but "
                    f"embedding_dim={self.dim}; lower embedding_dim or pick a "
                    "model whose native dimension is >= embedding_dim."
                )
            out.append(_l2_normalize(v) if self._normalize else v)
        return out

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed note bodies as passages, batched to bound the attention tensor."""
        if not texts:
            return []
        model = self._ensure_model()
        texts = list(texts)
        passage_embed = getattr(model, "passage_embed", None)
        # Bound the batch so the attention tensor (batch x heads x seq x seq)
        # stays small — fastembed's default of 256 OOMs large models (e.g. mxbai
        # at seq 512 needs ~4 GB per batch).
        raw = (
            passage_embed(texts, batch_size=self._batch_size)
            if passage_embed
            else model.embed(texts, batch_size=self._batch_size)
        )
        return self._finalize(raw)

    def embed_query(self, text: str) -> List[float]:
        """Embed a search query, using the model's query prefix when available."""
        model = self._ensure_model()
        query_embed = getattr(model, "query_embed", None)
        raw = (
            query_embed([text], batch_size=self._batch_size)
            if query_embed
            else model.embed([text], batch_size=self._batch_size)
        )
        return self._finalize(raw)[0]


def build_embedding_provider(config) -> Optional[EmbeddingProvider]:  # type: ignore[no-untyped-def]
    """Construct the configured provider, or ``None`` when embeddings are off."""
    if not getattr(config, "embedding_enabled", False):
        return None
    name = (config.embedding_provider or "").strip().lower()
    dim = config.embedding_dim
    batch_size = getattr(config, "embedding_batch_size", 16)
    device = getattr(config, "embedding_device", "cpu")
    if name in ("fastembed", "fast-embed", "lite"):
        return FastEmbedProvider(
            config.embedding_model, dim, batch_size=batch_size, device=device
        )
    if name in ("sentence-transformers", "sentence_transformers", "st"):
        return SentenceTransformerProvider(
            config.embedding_model, dim, batch_size=batch_size, device=device
        )
    if name == "hash":
        return HashEmbeddingProvider(dim)
    raise ValueError(f"Unknown embedding provider: {config.embedding_provider!r}")
