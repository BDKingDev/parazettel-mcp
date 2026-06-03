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
import struct
from typing import Any, Dict, List, Optional, Sequence

try:  # Protocol is stdlib on 3.8+, but guard for clarity.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore


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
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.dim = dim
        self.model_id = f"hash:deterministic:{dim}"

    def _embed(self, text: str) -> List[float]:
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
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
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

    def __init__(self, model_name: str, dim: int, *, normalize: bool = True) -> None:
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.model_name = model_name
        self.dim = dim
        self.model_id = f"sentence-transformers:{model_name}:{dim}"
        self._normalize = normalize
        self._model = None  # lazy-loaded on first use

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "Embeddings are enabled with provider 'sentence-transformers' "
                    "but the package is not installed. Install the optional "
                    "dependency group: pip install 'parazettel-mcp[embeddings]'"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _has_prompt(self, name: str) -> bool:
        prompts = getattr(self._model, "prompts", None) or {}
        return name in prompts

    def _encode(self, texts: Sequence[str], *, prompt: str) -> List[List[float]]:
        model = self._ensure_model()
        kwargs: Dict[str, Any] = {"convert_to_numpy": True}
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
        if not texts:
            return []
        return self._encode(texts, prompt="document")

    def embed_query(self, text: str) -> List[float]:
        return self._encode([text], prompt="query")[0]


class FastEmbedProvider:
    """Lightweight local embeddings via ``fastembed`` (ONNX Runtime, no PyTorch).

    The ``fastembed`` package is imported lazily so it is only required when
    embeddings are enabled with this provider. Vectors are truncated to ``dim``
    and L2-normalized so cosine works correctly. Where the model defines query
    vs. passage prefixes, fastembed's ``query_embed`` / ``passage_embed`` are
    used so query and document text are embedded asymmetrically.
    """

    def __init__(self, model_name: str, dim: int, *, normalize: bool = True) -> None:
        if dim <= 0:
            raise ValueError("embedding_dim must be positive")
        self.model_name = model_name
        self.dim = dim
        self.model_id = f"fastembed:{model_name}:{dim}"
        self._normalize = normalize
        self._model = None  # lazy-loaded on first use

    def _ensure_model(self):  # type: ignore[no-untyped-def]
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError(
                    "Embeddings are enabled with provider 'fastembed' but the "
                    "package is not installed. Install the optional dependency "
                    "group: pip install 'parazettel-mcp[embeddings-lite]'"
                ) from exc
            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    def _finalize(self, raw) -> List[List[float]]:  # type: ignore[no-untyped-def]
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
        if not texts:
            return []
        model = self._ensure_model()
        passage_embed = getattr(model, "passage_embed", None)
        raw = passage_embed(list(texts)) if passage_embed else model.embed(list(texts))
        return self._finalize(raw)

    def embed_query(self, text: str) -> List[float]:
        model = self._ensure_model()
        query_embed = getattr(model, "query_embed", None)
        raw = query_embed([text]) if query_embed else model.embed([text])
        return self._finalize(raw)[0]


def build_embedding_provider(config) -> Optional[EmbeddingProvider]:  # type: ignore[no-untyped-def]
    """Construct the configured provider, or ``None`` when embeddings are off."""
    if not getattr(config, "embedding_enabled", False):
        return None
    name = (config.embedding_provider or "").strip().lower()
    dim = config.embedding_dim
    if name in ("fastembed", "fast-embed", "lite"):
        return FastEmbedProvider(config.embedding_model, dim)
    if name in ("sentence-transformers", "sentence_transformers", "st"):
        return SentenceTransformerProvider(config.embedding_model, dim)
    if name == "hash":
        return HashEmbeddingProvider(dim)
    raise ValueError(f"Unknown embedding provider: {config.embedding_provider!r}")
