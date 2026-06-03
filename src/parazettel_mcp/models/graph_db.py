"""Kuzu graph database schema and initialisation for parazettel-mcp.

Graph schema
------------
Nodes
  Note   – one node per markdown note; all structured fields stored as properties
  Tag    – one node per unique tag name

Relationships
  LINKS_TO  (Note → Note) – typed semantic link with optional description
  HAS_TAG   (Note → Tag)  – membership edge

The Note node stores dates (due_date, remind_at) as ISO-8601 strings rather than
Kuzu DATE values so that NULL is representable without a sentinel and round-trips
through Python cleanly.
"""

import os
import threading
from pathlib import Path
from typing import Dict, Tuple

import kuzu

_DB_CACHE_LOCK = threading.Lock()
_DB_CACHE: Dict[str, Tuple[kuzu.Database, int]] = {}
_NOTE_FTS_INDEXES = {
    "note_text_fts": ["title", "content"],
    "note_title_fts": ["title"],
    "note_content_fts": ["content"],
}


class GraphDatabaseReadOnlyError(RuntimeError):
    """Raised when a mutating operation is attempted in read-only graph mode."""


def _db_cache_key(db_path: Path, read_only: bool = False) -> str:
    """Return a normalized cache key for a graph database path and access mode."""
    mode = "ro" if read_only else "rw"
    return f"{Path(db_path).expanduser().resolve()}::{mode}"


def init_graph_db(db_path: Path, read_only: bool = False) -> kuzu.Database:
    """Create (or open) the Kuzu database at *db_path* and ensure the schema exists.

    Args:
        db_path: File path for the Kuzu database.  The parent directory must
                 exist.  Kuzu manages its own internal file structure at this
                 path; do **not** create the path as a directory before calling
                 this function.

    Returns:
        An open :class:`kuzu.Database` instance.
    """
    db_path = Path(db_path).expanduser().resolve()
    cache_key = _db_cache_key(db_path, read_only=read_only)

    with _DB_CACHE_LOCK:
        cached = _DB_CACHE.get(cache_key)
        if cached is not None:
            db, refcount = cached
            _DB_CACHE[cache_key] = (db, refcount + 1)
            return db

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = kuzu.Database(str(db_path), read_only=read_only)
        if not read_only:
            conn = kuzu.Connection(db)
            try:
                _create_schema(conn)
            finally:
                conn.close()
        _DB_CACHE[cache_key] = (db, 1)
        return db


def close_graph_db(db_path: Path, read_only: bool = False) -> None:
    """Release a shared graph database handle for *db_path*."""
    cache_key = _db_cache_key(db_path, read_only=read_only)

    with _DB_CACHE_LOCK:
        cached = _DB_CACHE.get(cache_key)
        if cached is None:
            return
        db, refcount = cached
        if refcount > 1:
            _DB_CACHE[cache_key] = (db, refcount - 1)
            return
        del _DB_CACHE[cache_key]

    db.close()


def force_close_graph_db(db_path: Path) -> None:
    """Fully release the cached handle for *db_path*, ignoring the refcount.

    Used by the rebuild swap, which must guarantee the on-disk file is closed
    (so it can be atomically replaced) regardless of how many logical references
    are outstanding. Callers are responsible for reopening afterwards.
    """
    resolved = Path(db_path).expanduser().resolve()
    with _DB_CACHE_LOCK:
        for mode in ("rw", "ro"):
            cache_key = f"{resolved}::{mode}"
            cached = _DB_CACHE.pop(cache_key, None)
            if cached is not None:
                cached[0].close()


# Kuzu sidecar files that live next to the main DB file (e.g. graph.kuzu.wal).
# Restricted to a known suffix allowlist so unrelated files that merely share the
# DB name prefix (backups, in-progress rebuild temp DBs) are never matched.
_KUZU_SIDECAR_SUFFIXES = (".wal", ".shadow", ".tmp.wal")


def graph_db_companions(db_path: Path) -> list[Path]:
    """Return Kuzu sidecar files (e.g. the .wal) that travel with the DB file."""
    return [
        path
        for suffix in _KUZU_SIDECAR_SUFFIXES
        for path in [db_path.with_name(f"{db_path.name}{suffix}")]
        if path.is_file()
    ]


# Backwards-compatible internal alias.
_companion_paths = graph_db_companions


def swap_graph_db_file(new_db_path: Path, live_db_path: Path) -> None:
    """Atomically replace the live graph DB file with a freshly built one.

    The caller must have already closed every handle to *live_db_path*
    (see :func:`force_close_graph_db`). Both the main DB file and its Kuzu
    sidecar files (``.wal`` etc.) are swapped; stale sidecars left over from the
    old database are removed so a half-old/half-new on-disk state is impossible.
    """
    new_db_path = Path(new_db_path)
    live_db_path = Path(live_db_path)

    # Remove the old live sidecars first so none survive the swap.
    for stale in _companion_paths(live_db_path):
        stale.unlink()

    os.replace(new_db_path, live_db_path)

    # Move any sidecars produced alongside the freshly built DB into place.
    for companion in _companion_paths(new_db_path):
        suffix = companion.name[len(new_db_path.name):]
        os.replace(companion, live_db_path.parent / f"{live_db_path.name}{suffix}")


def _ensure_fts_indexes(conn: kuzu.Connection) -> None:
    """Ensure the note full-text indexes exist."""
    conn.execute("INSTALL FTS")
    conn.execute("LOAD FTS")

    index_result = conn.execute("CALL SHOW_INDEXES() RETURN *")
    existing_indexes = set()
    while index_result.has_next():
        table_name, index_name, index_type, *_ = index_result.get_next()
        if table_name == "Note" and index_type == "FTS":
            existing_indexes.add(index_name)

    for index_name, properties in _NOTE_FTS_INDEXES.items():
        if index_name in existing_indexes:
            continue
        property_literals = ", ".join(f"'{property_name}'" for property_name in properties)
        conn.execute(
            "CALL CREATE_FTS_INDEX("
            f"'Note', '{index_name}', [{property_literals}]"
            ")"
        )


def _create_schema(conn: kuzu.Connection) -> None:
    """Ensure all node and relationship tables exist (idempotent)."""
    conn.execute(
        """
        CREATE NODE TABLE IF NOT EXISTS Note(
            id              STRING  PRIMARY KEY,
            title           STRING,
            content         STRING,
            note_type       STRING,
            status          STRING,
            source          STRING,
            due_date        STRING,
            priority        INT64,
            recurrence_rule STRING,
            estimated_minutes INT64,
            remind_at       STRING,
            project_id      STRING,
            area_id         STRING,
            metadata_json   STRING,
            created_at      TIMESTAMP,
            updated_at      TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE NODE TABLE IF NOT EXISTS Tag(
            name STRING PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE REL TABLE IF NOT EXISTS LINKS_TO(
            FROM Note TO Note,
            link_type   STRING,
            description STRING,
            created_at  TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE REL TABLE IF NOT EXISTS HAS_TAG(
            FROM Note TO Tag
        )
        """
    )
    _ensure_fts_indexes(conn)


# --- Semantic embedding schema / HNSW vector index (optional, off by default) ---
#
# Kuzu 0.11.x ships the `vector` extension statically linked, so no INSTALL/LOAD
# is required. These helpers are only invoked when embeddings are enabled; the
# base Note schema above is untouched otherwise.

NOTE_VECTOR_INDEX = "note_vec"
_VALID_METRICS = frozenset({"cosine", "l2", "dotproduct"})


def ensure_embedding_schema(conn: kuzu.Connection, dim: int) -> None:
    """Idempotently add the embedding columns to the Note node.

    ``ALTER ... ADD IF NOT EXISTS`` is a no-op when a column already exists, so
    this is safe to call on every open. The embedding column is ``FLOAT[dim]``;
    rows created before it existed simply hold NULL until (re)embedded.

    Args:
        conn: An open writable Kuzu connection.
        dim: Vector dimensionality; must match the configured embedding model.
    """
    dim = int(dim)
    if dim <= 0:
        raise ValueError("embedding dim must be positive")
    conn.execute(f"ALTER TABLE Note ADD IF NOT EXISTS embedding FLOAT[{dim}]")
    conn.execute("ALTER TABLE Note ADD IF NOT EXISTS embedded_at TIMESTAMP")
    conn.execute("ALTER TABLE Note ADD IF NOT EXISTS embedding_model STRING")


def note_vector_index_exists(
    conn: kuzu.Connection, index_name: str = NOTE_VECTOR_INDEX
) -> bool:
    """Return True if an HNSW index named *index_name* exists on the Note table."""
    result = conn.execute("CALL SHOW_INDEXES() RETURN *")
    while result.has_next():
        table_name, existing_name, *_ = result.get_next()
        if table_name == "Note" and existing_name == index_name:
            return True
    return False


def drop_note_vector_index(
    conn: kuzu.Connection, index_name: str = NOTE_VECTOR_INDEX
) -> None:
    """Drop the Note HNSW index if present (no error when it does not exist).

    Note: Kuzu 0.11.x does not reliably allow recreating an index of the *same
    name* in the same database after a drop, so this is not used for in-place
    recreation — dimension/metric changes go through a full rebuild into a fresh
    database instead.
    """
    if note_vector_index_exists(conn, index_name):
        conn.execute(f"CALL DROP_VECTOR_INDEX('Note', '{index_name}')")


def create_note_vector_index(
    conn: kuzu.Connection,
    metric: str = "cosine",
    index_name: str = NOTE_VECTOR_INDEX,
) -> None:
    """Create the HNSW index over ``Note.embedding`` if it does not already exist.

    A no-op when the index is already present, so it is safe to call at the end
    of every rebuild. The embedding column must exist first (see
    :func:`ensure_embedding_schema`). Because the rebuild pipeline builds into a
    fresh database, the index is always created exactly once per database; the
    embedding dimension or metric is changed by rebuilding (not recreating in
    place), which Kuzu 0.11.x does not reliably support for same-named indexes.
    """
    if metric not in _VALID_METRICS:
        raise ValueError(
            f"Unsupported embedding metric {metric!r}; expected one of "
            f"{sorted(_VALID_METRICS)}"
        )
    if note_vector_index_exists(conn, index_name):
        return
    conn.execute(
        "CALL CREATE_VECTOR_INDEX("
        f"'Note', '{index_name}', 'embedding', metric := '{metric}'"
        ")"
    )
