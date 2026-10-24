"""Read the index directly in tests, without depending on its schema elsewhere.

Tests that need to see what is *stored* (rather than what the service returns)
go through these helpers, so a schema change touches one file.
"""

import datetime
from typing import List, Optional, Set

from parazettel_mcp.storage.index_db import blob_to_vector, ts_out


def _repo(owner):
    """Accept a NoteRepository or anything with a ``.repository`` attribute."""
    return getattr(owner, "repository", owner)


def edge_types(owner, source_id: str, target_id: str) -> Set[str]:
    """Link types stored from *source_id* to *target_id*."""
    with _repo(owner)._connection() as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT link_type FROM links WHERE source_id = ? AND target_id = ?",
                (source_id, target_id),
            )
        }


def edge_count(owner, source_id: str, target_id: str) -> int:
    """Number of stored link rows from *source_id* to *target_id*."""
    with _repo(owner)._connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM links WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        ).fetchone()[0]


def edge_created_at(owner, source_id: str, target_id: str) -> List[datetime.datetime]:
    """Stored ``created_at`` of each link from *source_id* to *target_id*."""
    with _repo(owner)._connection() as conn:
        return [
            ts_out(row[0])
            for row in conn.execute(
                "SELECT created_at FROM links WHERE source_id = ? AND target_id = ? "
                "ORDER BY pk",
                (source_id, target_id),
            )
        ]


def note_indexed(owner, note_id: str) -> bool:
    """Whether a row for *note_id* exists in the index."""
    with _repo(owner)._connection() as conn:
        return (
            conn.execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone()
            is not None
        )


def stored_content(owner, note_id: str) -> Optional[str]:
    """The note body as stored in the index (None if not indexed)."""
    with _repo(owner)._connection() as conn:
        row = conn.execute("SELECT content FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else None


def set_stored_content(owner, note_id: str, content: str) -> None:
    """Overwrite the stored body (simulates index/file drift)."""
    with _repo(owner)._connection() as conn:
        conn.execute("UPDATE notes SET content = ? WHERE id = ?", (content, note_id))


def stored_vector(owner, note_id: str, *, title: bool = False) -> Optional[List[float]]:
    """The stored doc (or title) vector for *note_id*, any model."""
    column = "title_vec" if title else "doc_vec"
    with _repo(owner)._connection() as conn:
        row = conn.execute(
            f"SELECT {column} FROM embeddings WHERE note_id = ?", (note_id,)
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return blob_to_vector(row[0]).tolist()


def stored_embedding_model(owner, note_id: str) -> Optional[str]:
    with _repo(owner)._connection() as conn:
        row = conn.execute(
            "SELECT model FROM embeddings WHERE note_id = ?", (note_id,)
        ).fetchone()
    return row[0] if row else None


def embedding_count(owner) -> int:
    with _repo(owner)._connection() as conn:
        return conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
