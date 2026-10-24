"""SQLite index database for parazettel-mcp.

The markdown files are the source of truth; this database is a derived index
that can be rebuilt from them at any time. It holds:

Tables
  notes              – one row per note; all structured fields as columns
  note_tags          – (note_id, tag) membership
  links              – typed note→note links with optional description
  note_fts           – FTS5 full-text index (title / content / title+content)
  embeddings         – optional doc + title vectors (float32 BLOBs)
  retrieval_signals  – hit counters; operational data that is NOT in markdown,
                       so it survives a rebuild (no foreign key to ``notes``)
  meta               – small integer counters (e.g. the embedding generation)

Concurrency: the database runs in WAL mode, so any number of processes can read
while one writes. Writers take ``BEGIN IMMEDIATE`` (see :func:`transaction`) and
wait up to ``busy_timeout`` for the write lock instead of failing.

Dates are stored as ISO-8601 strings. Timestamps use a fixed microsecond
precision (:func:`ts_in`) so they sort correctly as text.
"""

import datetime
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence

import numpy as np

from parazettel_mcp.storage.fts_stopwords import ENGLISH_STOPWORDS

logger = logging.getLogger(__name__)

# Bump when the schema changes. The index is derived data, so a mismatch simply
# drops and recreates every table except retrieval_signals.
SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 10_000
EMBEDDING_GENERATION_KEY = "embedding_gen"

# bm25() column weights for each logical full-text field. Each field searches one
# FTS5 column, so the other weights are zero (the replacement for Kuzu's three
# separate FTS indexes).
FTS_COLUMNS = ("title", "content", "body")
FTS_WEIGHTS = {
    "body": (0.0, 0.0, 1.0),
    "title": (1.0, 0.0, 0.0),
    "content": (0.0, 1.0, 0.0),
}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class GraphDatabaseReadOnlyError(RuntimeError):
    """Raised when a mutating operation is attempted in read-only mode.

    SQLite in WAL mode lets every process write, so this is no longer raised.
    The class is kept so callers (and an older daemon client) that import or
    map it by name keep working.
    """


class IndexCapabilityError(RuntimeError):
    """The Python SQLite build lacks a feature the index requires."""


# --------------------------------------------------------------------------- #
# connections
# --------------------------------------------------------------------------- #


def connect(
    db_path: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """Open a connection to the index at *db_path* with the required pragmas.

    Pragmas other than ``journal_mode`` are connection-scoped and reset when a
    connection closes, so every connection must set them — not just the first.
    The connection is in autocommit mode; use :func:`transaction` for writes.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise IndexCapabilityError(
            f"Could not enable WAL mode on {db_path} (got {mode!r}); the index "
            "needs WAL for safe multi-process access. Is the file on a network "
            "or read-only filesystem?"
        )
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def probe_capabilities(conn: sqlite3.Connection) -> None:
    """Fail loudly if this SQLite build lacks FTS5 or the JSON functions.

    Better to refuse to start with an actionable message than to run with
    search quietly degraded.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE temp._probe_fts USING fts5(x, tokenize='porter unicode61')"
        )
        conn.execute("DROP TABLE temp._probe_fts")
    except sqlite3.OperationalError as exc:
        raise IndexCapabilityError(
            f"This Python's SQLite ({sqlite3.sqlite_version}) lacks FTS5 with the "
            "porter tokenizer, which full-text search requires. Use a Python "
            "build whose bundled SQLite includes FTS5 (e.g. a uv-managed Python)."
        ) from exc
    try:
        conn.execute("SELECT value FROM json_each('[1]')").fetchall()
    except sqlite3.OperationalError as exc:
        raise IndexCapabilityError(
            f"This Python's SQLite ({sqlite3.sqlite_version}) lacks the JSON "
            "functions (json_each) the index requires."
        ) from exc


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction, taking the database write lock up front.

    ``BEGIN IMMEDIATE`` acquires the write lock at the start (waiting up to
    ``busy_timeout``), so two writers never deadlock trying to upgrade a read
    lock mid-transaction.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

_DERIVED_TABLES = ("note_fts", "embeddings", "links", "note_tags", "notes", "meta")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    pk                INTEGER PRIMARY KEY,
    id                TEXT NOT NULL UNIQUE,
    title             TEXT,
    content           TEXT,
    note_type         TEXT NOT NULL,
    status            TEXT,
    source            TEXT,
    due_date          TEXT,
    priority          INTEGER,
    recurrence_rule   TEXT,
    estimated_minutes INTEGER,
    remind_at         TEXT,
    project_id        TEXT,
    area_id           TEXT,
    origin            TEXT,
    last_verified     TEXT,
    metadata_json     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_title ON notes(title);
CREATE INDEX IF NOT EXISTS notes_type_status ON notes(note_type, status);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project_id);
CREATE INDEX IF NOT EXISTS notes_area ON notes(area_id);
CREATE INDEX IF NOT EXISTS notes_due ON notes(due_date);
CREATE INDEX IF NOT EXISTS notes_remind ON notes(remind_at);
CREATE INDEX IF NOT EXISTS notes_created ON notes(created_at);
CREATE INDEX IF NOT EXISTS notes_updated ON notes(updated_at);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS note_tags_tag ON note_tags(tag, note_id);

CREATE TABLE IF NOT EXISTS links (
    pk          INTEGER PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_id   TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    link_type   TEXT NOT NULL,
    description TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS links_source ON links(source_id);
CREATE INDEX IF NOT EXISTS links_target ON links(target_id, link_type);

CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
    title, content, body,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS embeddings (
    note_id     TEXT PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    doc_vec     BLOB NOT NULL,
    title_vec   BLOB,
    text_hash   TEXT,
    embedded_at TEXT
);

CREATE TABLE IF NOT EXISTS retrieval_signals (
    note_id           TEXT PRIMARY KEY,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    last_retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema, or recreate it if it was built by another version.

    Idempotent. A version mismatch drops the derived tables (they are rebuilt
    from the markdown files) but keeps ``retrieval_signals``, which exists only
    here.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        # Already current: don't take BEGIN IMMEDIATE, so a reader opening the
        # index while another process writes isn't stuck behind the busy timeout.
        return
    with transaction(conn):
        if version not in (0, SCHEMA_VERSION):
            logger.warning(
                "Index schema version %s != %s; recreating derived tables "
                "(they rebuild from the markdown files).",
                version,
                SCHEMA_VERSION,
            )
            for table in _DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in _split_sql(_SCHEMA_SQL):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _split_sql(script: str) -> List[str]:
    """Split a schema script into statements (no semicolons inside our DDL)."""
    return [s.strip() for s in script.split(";") if s.strip()]


# --------------------------------------------------------------------------- #
# backups
# --------------------------------------------------------------------------- #


def backup_to(conn: sqlite3.Connection, dest: Path) -> Path:
    """Write a consistent online copy of the database to *dest*.

    Uses SQLite's backup API, so it is safe while other connections (including
    other processes) are reading or writing.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def prune_backups(db_path: Path, keep: int) -> List[Path]:
    """Delete all but the newest *keep* ``<db>.<timestamp>.bak`` files."""
    db_path = Path(db_path)
    backups = sorted(
        db_path.parent.glob(f"{db_path.name}.*.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = []
    for stale in backups[max(0, keep):]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning("Could not remove old index backup %s: %s", stale, exc)
    return removed


# --------------------------------------------------------------------------- #
# full-text helpers
# --------------------------------------------------------------------------- #


def fts_terms(text: Optional[str]) -> List[str]:
    """Lowercased word tokens of *text*, minus stopwords, in first-seen order."""
    seen: List[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in ENGLISH_STOPWORDS or token in seen:
            continue
        seen.append(token)
    return seen


def fts_index_text(text: Optional[str]) -> str:
    """Text as stored in the FTS index: word tokens with stopwords removed.

    Stopwords are dropped at index time (not just query time) so BM25 document
    lengths match what Kuzu's FTS computed.
    """
    return " ".join(
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if token not in ENGLISH_STOPWORDS
    )


def fts_match_expression(
    terms: Sequence[str], column: str, *, conjunctive: bool
) -> Optional[str]:
    """Build an FTS5 MATCH expression searching *terms* in *column*.

    Every term is double-quoted, which makes FTS5 treat it as a literal string:
    operators (AND/OR/NOT/NEAR), prefix ``*``, ``^`` and column ``:`` syntax in
    user input can't change the query. Returns None when there is nothing to
    search for.
    """
    if column not in FTS_COLUMNS:
        raise ValueError(f"unknown FTS column {column!r}")
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms if term]
    if not quoted:
        return None
    joiner = " " if conjunctive else " OR "
    return f"{{{column}}} : ({joiner.join(quoted)})"


# --------------------------------------------------------------------------- #
# timestamps and vectors
# --------------------------------------------------------------------------- #


def ts_in(value: Optional[datetime.datetime]) -> Optional[str]:
    """Store a timestamp as fixed-precision ISO text so it sorts as a string.

    Timezone-aware values are converted to naive local time, matching the naive
    timestamps notes are written with.
    """
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time.min)
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def ts_out(value: Optional[str]) -> Optional[datetime.datetime]:
    """Parse a stored timestamp back to a naive datetime."""
    if value is None or value == "":
        return None
    return datetime.datetime.fromisoformat(value)


def vector_to_blob(vector: Iterable[float]) -> bytes:
    """Pack a vector as little-endian float32 bytes."""
    return np.asarray(list(vector), dtype="<f4").tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    """Unpack a float32 BLOB produced by :func:`vector_to_blob`."""
    return np.frombuffer(blob, dtype="<f4")


# --------------------------------------------------------------------------- #
# meta counters
# --------------------------------------------------------------------------- #


def get_counter(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row[0]) if row else 0


def bump_counter(conn: sqlite3.Connection, key: str) -> None:
    """Increment a meta counter. Call inside the write transaction it describes."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = value + 1",
        (key,),
    )
