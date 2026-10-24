"""Exact (brute-force) vector search over the index's stored embeddings.

At vault scale (thousands of notes) an exact scan is a single matrix-vector
product — well under a millisecond — so no approximate index is needed. The
vectors are loaded from the ``embeddings`` table into float32 matrices once and
cached per process.

The cache is keyed on the ``embedding_gen`` counter in the ``meta`` table, which
every embedding write bumps inside its own transaction. Each search reads that
one integer and reloads only when it changed, so a write in *any* process is
picked up by every other process on its next search.
"""

import logging
import sqlite3
import threading
from typing import Dict, List, Optional, Sequence

import numpy as np

from parazettel_mcp.storage.index_db import (
    EMBEDDING_GENERATION_KEY,
    blob_to_vector,
    get_counter,
)

logger = logging.getLogger(__name__)

VALID_METRICS = frozenset({"cosine", "l2", "dotproduct"})


class VectorIndex:
    """In-memory matrices of every note's doc and title vector for one model."""

    def __init__(self, metric: str = "cosine") -> None:
        metric = (metric or "cosine").strip().lower()
        if metric not in VALID_METRICS:
            raise ValueError(
                f"Unsupported embedding metric {metric!r}; expected one of "
                f"{sorted(VALID_METRICS)}"
            )
        self.metric = metric
        self._lock = threading.Lock()
        self._loaded_key: Optional[tuple] = None
        self._ids: List[str] = []
        self._doc = np.zeros((0, 0), dtype=np.float32)
        self._title = np.zeros((0, 0), dtype=np.float32)
        self._has_title = np.zeros((0,), dtype=bool)

    # -- loading ----------------------------------------------------------- #

    def _ensure_loaded(self, conn: sqlite3.Connection, model_id: str) -> None:
        key = (get_counter(conn, EMBEDDING_GENERATION_KEY), model_id)
        if key == self._loaded_key:
            return
        rows = conn.execute(
            "SELECT note_id, dim, doc_vec, title_vec FROM embeddings WHERE model = ?",
            (model_id,),
        ).fetchall()
        ids: List[str] = []
        docs: List[np.ndarray] = []
        titles: List[np.ndarray] = []
        has_title: List[bool] = []
        dim: Optional[int] = None
        for row in rows:
            doc = blob_to_vector(row["doc_vec"])
            if dim is None:
                dim = doc.shape[0]
            if doc.shape[0] != dim:
                logger.warning(
                    "Skipping embedding for %s: dim %d != %d", row["note_id"], doc.shape[0], dim
                )
                continue
            ids.append(row["note_id"])
            docs.append(doc)
            title_blob = row["title_vec"]
            title = blob_to_vector(title_blob) if title_blob else None
            if title is not None and title.shape[0] == dim:
                titles.append(title)
                has_title.append(True)
            else:
                titles.append(np.zeros(dim, dtype=np.float32))
                has_title.append(False)
        dim = dim or 0
        self._ids = ids
        self._doc = self._prepare(np.vstack(docs) if docs else np.zeros((0, dim), np.float32))
        self._title = self._prepare(
            np.vstack(titles) if titles else np.zeros((0, dim), np.float32)
        )
        self._has_title = np.asarray(has_title, dtype=bool)
        self._loaded_key = key

    def _prepare(self, matrix: np.ndarray) -> np.ndarray:
        matrix = np.ascontiguousarray(matrix, dtype=np.float32)
        if self.metric == "cosine" and matrix.size:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        return matrix

    def loaded_count(self) -> int:
        """Number of vectors in the cache (for health reporting)."""
        return len(self._ids)

    # -- search ------------------------------------------------------------ #

    def _distances(self, matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
        """Distance from *query* to every row (lower = closer), per the metric.

        Mirrors the conventions the Kuzu index used: cosine → 1 - cos,
        l2 → Euclidean distance, dotproduct → negative dot product.
        """
        if self.metric == "cosine":
            return 1.0 - matrix @ query
        if self.metric == "l2":
            return np.linalg.norm(matrix - query, axis=1)
        return -(matrix @ query)

    def search(
        self,
        conn: sqlite3.Connection,
        query_vector: Sequence[float],
        model_id: str,
        k: int,
    ) -> Dict[str, float]:
        """Return ``{note_id: distance}`` for the *k* closest notes.

        A note's distance is the closer of its doc vector and its title vector,
        so a note surfaces when either its body or its title (the atomic claim)
        matches.
        """
        if k <= 0:
            return {}
        with self._lock:
            self._ensure_loaded(conn, model_id)
            if not self._ids:
                return {}
            query = np.asarray(query_vector, dtype=np.float32)
            if query.shape[0] != self._doc.shape[1]:
                logger.warning(
                    "Query vector dim %d != index dim %d; skipping vector search",
                    query.shape[0],
                    self._doc.shape[1],
                )
                return {}
            if self.metric == "cosine":
                norm = float(np.linalg.norm(query))
                if norm == 0.0:
                    return {}
                query = query / norm
            distances = self._distances(self._doc, query)
            if self._has_title.any():
                title_distances = self._distances(self._title, query)
                title_distances[~self._has_title] = np.inf
                distances = np.minimum(distances, title_distances)
            k = min(k, distances.shape[0])
            top = np.argpartition(distances, k - 1)[:k]
            top = top[np.argsort(distances[top], kind="stable")]
            return {self._ids[i]: float(distances[i]) for i in top}
