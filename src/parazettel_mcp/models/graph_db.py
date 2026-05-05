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

import threading
from pathlib import Path
from typing import Dict, Tuple

import kuzu

_DB_CACHE_LOCK = threading.Lock()
_DB_CACHE: Dict[str, Tuple[kuzu.Database, int]] = {}


def _db_cache_key(db_path: Path) -> str:
    """Return a normalized cache key for a graph database path."""
    return str(Path(db_path).expanduser().resolve())


def init_graph_db(db_path: Path) -> kuzu.Database:
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
    cache_key = _db_cache_key(db_path)

    with _DB_CACHE_LOCK:
        cached = _DB_CACHE.get(cache_key)
        if cached is not None:
            db, refcount = cached
            _DB_CACHE[cache_key] = (db, refcount + 1)
            return db

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        try:
            _create_schema(conn)
        finally:
            conn.close()
        _DB_CACHE[cache_key] = (db, 1)
        return db


def close_graph_db(db_path: Path) -> None:
    """Release a shared graph database handle for *db_path*."""
    cache_key = _db_cache_key(db_path)

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
