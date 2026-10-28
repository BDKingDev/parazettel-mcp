"""Repository for note storage and retrieval.

Dual-storage architecture
-------------------------
* Markdown files on disk – **source of truth** for note content and frontmatter.
* SQLite index database – **index** for fast querying, full-text and vector
  search, and link traversal (see :mod:`parazettel_mcp.storage.index_db`).

The file system is always authoritative; the index can be rebuilt from the
markdown files at any time via :meth:`NoteRepository.rebuild_index`.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

import frontmatter

from parazettel_mcp.config import config
from parazettel_mcp.storage.index_db import (
    EMBEDDING_GENERATION_KEY,
    FTS_WEIGHTS,
    backup_to,
    bump_counter,
    connect,
    ensure_schema,
    fts_index_text,
    fts_match_expression,
    fts_terms,
    get_counter,
    probe_capabilities,
    prune_backups,
    transaction,
    ts_in,
    ts_out,
    vector_to_blob,
    blob_to_vector,
)
from parazettel_mcp.storage.file_lock import file_lock
from parazettel_mcp.storage.vector_index import VectorIndex
from parazettel_mcp.models.schema import (
    Link,
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
    Tag,
)
from parazettel_mcp.services.embedding_provider import build_embedding_provider
from parazettel_mcp.storage.base import Repository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level LRU cache  {(path_str, mtime_ns): Note}
# ---------------------------------------------------------------------------
_NOTE_CACHE: OrderedDict = OrderedDict()
_NOTE_CACHE_LOCK = threading.Lock()
_NOTE_CACHE_MAX = 256
_ATOMIC_WRITE_ATTEMPTS = 5
_ATOMIC_WRITE_BACKOFF_SECONDS = 0.05
_RETRYABLE_ATOMIC_WRITE_WINERRORS = {5, 32}
# Notes per progress-logged chunk during the bulk embed (within each chunk the
# provider further batches at embedding_batch_size). Purely for log granularity.
_EMBED_LOG_CHUNK = 200
# Cap how many skipped filenames are inlined into the rebuild warning log so a
# mass parse failure can't emit one enormous log line.
_REBUILD_SKIPPED_LOG_LIMIT = 10
# How long opening the repository waits for another process's write before the
# startup reconcile (a full rebuild can hold the lock for minutes on CPU).
_STARTUP_LOCK_TIMEOUT_SECONDS = 600.0

# Columns of the ``notes`` table written on every upsert (``pk`` is implicit).
_NOTE_COLUMNS = (
    "id",
    "title",
    "content",
    "note_type",
    "status",
    "source",
    "due_date",
    "priority",
    "recurrence_rule",
    "estimated_minutes",
    "remind_at",
    "project_id",
    "area_id",
    "origin",
    "last_verified",
    "metadata_json",
    "created_at",
    "updated_at",
)
_NOTE_SELECT = ", ".join(f"n.{column}" for column in _NOTE_COLUMNS)
_NOTE_UPSERT_SQL = (
    f"INSERT INTO notes ({', '.join(_NOTE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _NOTE_COLUMNS)}) "
    "ON CONFLICT(id) DO UPDATE SET "
    # created_at is immutable once indexed.
    + ", ".join(
        f"{column} = excluded.{column}"
        for column in _NOTE_COLUMNS
        if column not in ("id", "created_at")
    )
)

# Matches one Obsidian wiki link and captures its target (id, optionally with a
# |alias or #fragment suffix handled by _normalize_wiki_target).
_WIKI_LINK_RE = re.compile(r"\[\[([^\]\[]+)\]\]")
# Note IDs are timestamp-shaped (YYYYMMDDTHHMMSS + microseconds + counter).
_NOTE_ID_RE = re.compile(r"^\d{8}T\d{6,}$")
# Optional per-link provenance comment appended to a ## Links line, e.g.
# "- extends [[id|Title]] desc <!-- created: 2026-06-12T10:00:00 -->".
# Hidden in Obsidian preview; lets link created_at survive rebuilds.
_LINK_CREATED_RE = re.compile(r"\s*<!--\s*created:\s*([^>]+?)\s*-->\s*")

# Matches an actual "## Links" heading line (not the literal text in prose), so
# splitting a note body on it can't be tripped by an in-prose mention.
_LINKS_HEADING_RE = re.compile(r"(?m)^\s*##\s+Links\s*$")

def _cache_get(path_str: str, mtime_ns: int) -> Optional[Note]:
    key = (path_str, mtime_ns)
    with _NOTE_CACHE_LOCK:
        note = _NOTE_CACHE.get(key)
        if note is not None:
            _NOTE_CACHE.move_to_end(key)
            return note.model_copy(deep=True)
    return None


def _cache_put(path_str: str, mtime_ns: int, note: Note) -> None:
    key = (path_str, mtime_ns)
    with _NOTE_CACHE_LOCK:
        _NOTE_CACHE[key] = note
        _NOTE_CACHE.move_to_end(key)
        while len(_NOTE_CACHE) > _NOTE_CACHE_MAX:
            _NOTE_CACHE.popitem(last=False)


def _dedupe_preserve_order(values: List[str]) -> List[str]:
    """Return values without duplicates while preserving first-seen order."""
    seen: Set[str] = set()
    deduped: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _cache_evict(path_str: str) -> None:
    """Remove all cache entries for a given file path (any mtime)."""
    with _NOTE_CACHE_LOCK:
        stale = [k for k in _NOTE_CACHE if k[0] == path_str]
        for k in stale:
            del _NOTE_CACHE[k]


def _json_default(obj: Any) -> Any:
    """Convert non-JSON-serializable types for metadata storage."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _normalize_wiki_target(target: str) -> str:
    """Return the note id portion of an Obsidian wiki-link target."""
    target = target.strip()
    target = target.split("|", 1)[0].strip()
    target = target.split("#", 1)[0].strip()
    if target.endswith(".md"):
        target = target[: -len(".md")]
    return target


_LEADING_H1_RE = re.compile(r"^\s*#\s+.*$")

# Optional suffix an inline wiki-link target may carry after the note id and
# still normalize back to that id (see _normalize_wiki_target): a ".md" suffix
# and/or an Obsidian "#heading" fragment. Used to build the delete-scrub and
# rename-alias-refresh patterns so every form that _parse_inline_refs indexes is
# also cleaned up. Safe against a longer id with the same prefix: neither branch
# matches a trailing digit, so the surrounding "|"/"]]"/whitespace anchor fails.
# Public (no underscore) because the service layer builds the rename pattern
# from the same fragment — keeping one source of truth for the wiki-link grammar.
WIKI_TARGET_SUFFIX = r"(?:\.md)?(?:#[^\]\|]*)?"


def _coerce_datetime(value: Any, fallback: datetime.datetime) -> datetime.datetime:
    """Accept YAML-parsed datetimes/dates as well as ISO timestamp strings."""
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time.min)
    return datetime.datetime.fromisoformat(str(value))


def _is_retryable_atomic_write_error(exc: OSError) -> bool:
    """Return True when a transient Windows file lock may clear on retry."""
    if isinstance(exc, PermissionError):
        return True
    if exc.errno == 13:
        return True
    return getattr(exc, "winerror", None) in _RETRYABLE_ATOMIC_WRITE_WINERRORS


def _first_column(rows: Iterable[sqlite3.Row]) -> List[Any]:
    """Return the first column of every row as a list."""
    return [row[0] for row in rows]


def _json_ids(ids: Iterable[str]) -> str:
    """Encode ids as a JSON array for ``IN (SELECT value FROM json_each(?))``.

    One bound parameter regardless of list length, so large id sets never hit
    SQLite's host-parameter limit.
    """
    return json.dumps(list(ids))


def _db_dict_to_note(
    nd: Dict[str, Any], tags: List[Tag], links: List[Link]
) -> Note:
    """Reconstruct a Note from an index row and pre-fetched relations."""
    metadata = json.loads(nd["metadata_json"]) if nd.get("metadata_json") else {}
    return Note(
        id=nd["id"],
        title=nd["title"],
        content=nd["content"],
        note_type=NoteType(nd["note_type"]),
        tags=tags,
        links=links,
        created_at=ts_out(nd["created_at"]),
        updated_at=ts_out(nd["updated_at"]),
        metadata=metadata,
        status=NoteStatus(nd["status"]) if nd.get("status") else None,
        source=NoteSource(nd["source"]) if nd.get("source") else NoteSource.MANUAL,
        due_date=(
            datetime.date.fromisoformat(nd["due_date"]) if nd.get("due_date") else None
        ),
        priority=nd.get("priority"),
        recurrence_rule=nd.get("recurrence_rule"),
        estimated_minutes=nd.get("estimated_minutes"),
        remind_at=(
            datetime.date.fromisoformat(nd["remind_at"])
            if nd.get("remind_at")
            else None
        ),
        project_id=nd.get("project_id"),
        area_id=nd.get("area_id"),
        origin=nd.get("origin"),
        last_verified=(
            datetime.date.fromisoformat(nd["last_verified"])
            if nd.get("last_verified")
            else None
        ),
    )


class NoteRepository(Repository[Note]):
    """Repository for note storage and retrieval.

    Implements dual storage:
    1. Markdown files on disk – human-readable, editable, and the source of truth.
    2. SQLite index database – fast index for queries, search and link traversal.
    """

    def __init__(self, notes_dir: Optional[Path] = None, *, reconcile: bool = True):
        """Initialise the repository.

        Args:
            notes_dir: Notes directory (default: config).
            reconcile: Rebuild the index at startup if its notes differ from the
                files. Read-only callers on a deadline (the session hooks) pass
                False so they never wait on another process's write lock.
        """
        self.notes_dir = (
            config.get_absolute_path(notes_dir)
            if notes_dir
            else config.get_absolute_path(config.notes_dir)
        )
        self.notes_dir.mkdir(parents=True, exist_ok=True)

        self.index_db_path = config.get_index_db_path()
        # Deprecated alias kept for callers that still read the old name.
        self.graph_db_path = self.index_db_path
        # Cross-process lock file serializing writers (see ZettelService's write
        # lock, which takes it around every mutation).
        self.write_lock_path = self.index_db_path.with_name(
            f"{self.index_db_path.name}.write.lock"
        )
        self.file_lock = threading.RLock()
        # Every process can write under WAL, so the index is never read-only.
        # Kept for callers (e.g. /health) that still read the flag.
        self.read_only = False
        self._closed = True
        # One connection per thread: a sqlite3 connection must not be used by two
        # threads at once (the daemon serves requests on a thread pool).
        self._local = threading.local()
        self._connections: List[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        # Names of markdown files that failed to parse on the most recent rebuild.
        # Surfaced to callers so a shrinking corpus is visible instead of silent.
        self.last_rebuild_skipped: List[str] = []
        # Optional semantic-embedding backend; None when embeddings are disabled
        # (the default), in which case all embedding code paths are skipped and
        # behaviour is unchanged. Built from config once; the model loads lazily.
        self._embedding_provider = build_embedding_provider(config)
        self._vectors = VectorIndex(config.embedding_metric)
        # Cache of FTS term -> document frequency, used to focus long text queries
        # on their most discriminative terms (see _focus_query_terms). Invalidated
        # on every create/update/delete and on rebuild (see
        # _invalidate_fts_term_df_cache) so a term's DF is never stale across a
        # corpus change.
        self._fts_term_df_cache: Dict[str, int] = {}
        self._open_index(allow_rebuild_if_needed=reconcile)

    def close(self) -> None:
        """Release resources held by this repository."""
        if self._closed:
            return
        self._closed = True
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                pass
        self._local = threading.local()

    def _open_index(self, *, allow_rebuild_if_needed: bool) -> None:
        """Open the index, create its schema, and optionally reconcile with files."""
        self._closed = False
        conn = self._get_conn()
        probe_capabilities(conn)
        ensure_schema(conn)
        if allow_rebuild_if_needed:
            # Under the writers' lock, so a startup reconcile can't commit an
            # index snapshot that predates another process's in-flight write.
            with file_lock(self.write_lock_path, timeout=_STARTUP_LOCK_TIMEOUT_SECONDS):
                self.rebuild_index_if_needed()

    def _assert_writable(self) -> None:
        """No-op: every process may write under WAL. Kept for older callers."""

    def index_status(self) -> Dict[str, Any]:
        """Report index and embedding health (for /health and diagnostics).

        ``embeddings.vectors_stored`` counts vectors for the *active* model, so
        a provider that silently failed (e.g. a missing model file) shows up as
        enabled with far fewer vectors than notes.
        """
        provider = self._embedding_provider
        with self._connection() as conn:
            notes = conn.execute("SELECT count(*) FROM notes").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            vectors = (
                conn.execute(
                    "SELECT count(*) FROM embeddings WHERE model = ?",
                    (provider.model_id,),
                ).fetchone()[0]
                if provider is not None
                else 0
            )
            generation = get_counter(conn, EMBEDDING_GENERATION_KEY)
        return {
            "index": {
                "backend": "sqlite",
                "path": str(self.index_db_path),
                "notes": notes,
                "journal_mode": journal_mode,
            },
            "embeddings": {
                "enabled": provider is not None,
                "model_id": provider.model_id if provider is not None else None,
                "vectors_stored": vectors,
                "generation": generation,
            },
        }

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's connection to the index, opening it on first use."""
        if self._closed:
            raise RuntimeError("NoteRepository is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.index_db_path, config.sqlite_busy_timeout_ms)
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def release_thread_connection(self) -> None:
        """Close the calling thread's connection, if it has one.

        Call at the end of work done on a short-lived thread: the daemon's
        ThreadingHTTPServer runs every request on a NEW thread, and without this
        each request would leave an open connection (file handles + page cache)
        in ``_connections`` until shutdown. The next call on the thread reopens.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._connections_lock:
            try:
                self._connections.remove(conn)
            except ValueError:  # already released by close()
                return
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - best effort
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield this thread's index connection (kept open for reuse)."""
        yield self._get_conn()

    def _build_tmp_path(self, file_path: Path) -> Path:
        suffix = f".{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        return file_path.with_name(f"{file_path.name}{suffix}")

    def _write_markdown_atomically(self, file_path: Path, markdown: str) -> None:
        """Write markdown via temp file + replace, retrying transient Windows locks."""
        last_error: Optional[OSError] = None
        for attempt in range(_ATOMIC_WRITE_ATTEMPTS):
            tmp_path = self._build_tmp_path(file_path)
            try:
                with self.file_lock:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(markdown)
                        # Flush to disk before the atomic rename so a crash or
                        # power loss can't leave an empty/truncated file in place
                        # (rename gives atomic *visibility*, not durable content).
                        f.flush()
                        os.fsync(f.fileno())
                    tmp_path.replace(file_path)
                    # Persist the rename itself: on POSIX the directory entry can
                    # otherwise be lost on power loss even though the file's data
                    # was synced. Best-effort — not all platforms/filesystems
                    # allow opening a directory for fsync (e.g. Windows), so a
                    # failure here must not fail the write.
                    self._fsync_dir(file_path.parent)
                return
            except OSError as e:
                last_error = e
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                if (
                    attempt == _ATOMIC_WRITE_ATTEMPTS - 1
                    or not _is_retryable_atomic_write_error(e)
                ):
                    break
                time.sleep(_ATOMIC_WRITE_BACKOFF_SECONDS * (2**attempt))

        assert last_error is not None
        raise IOError(
            f"Failed to write note to {file_path}: {last_error}"
        ) from last_error

    @staticmethod
    def _fsync_dir(dir_path: Path) -> None:
        """Best-effort fsync of a directory so a rename survives power loss.

        Required on POSIX to durably persist the directory entry created by the
        atomic replace. Windows (and some filesystems) don't support opening a
        directory for fsync, so any failure is swallowed — durability of the
        rename is a best-effort guarantee, not a hard one.
        """
        # O_DIRECTORY (where available) ensures we only open an actual directory
        # and fail fast otherwise; it doesn't exist on Windows, where this whole
        # path no-ops via the except below.
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            dir_fd = os.open(str(dir_path), flags)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            # Swallow close errors too: directory fsync is best-effort and must
            # never fail the write path (consistent with the docstring).
            try:
                os.close(dir_fd)
            except OSError:
                pass

    def rebuild_index_if_needed(self) -> None:
        """Rebuild the index from files when the ID sets diverge."""
        with self._connection() as conn:
            db_ids = set(_first_column(conn.execute("SELECT id FROM notes")))
        file_stems = {p.stem for p in self.notes_dir.glob("*.md")}
        if db_ids != file_stems:
            self.rebuild_index()

    def check_consistency(self) -> Dict[str, Any]:
        """Compare the markdown files (source of truth) against the index.

        Read-only: this never modifies either store. It surfaces the three ways
        the file system and the index can silently disagree:

        * ``missing_from_index`` — a ``{id}.md`` file exists on disk but no Note
          node is indexed for it (e.g. a note added/restored outside the server).
        * ``missing_from_files`` — a Note node is indexed but its file is gone
          (e.g. a note deleted on disk directly).
        * ``content_drift`` — both exist, but the file's current content differs
          from what the index stored (e.g. an external editor changed the body
          and the index was never refreshed; ``rebuild_index_if_needed`` only
          checks the ID set, so same-id content edits are invisible to it).

        ``pzk_rebuild_index`` reconciles all three. Returns a structured report
        so callers can decide whether a rebuild is warranted.
        """
        file_stems = {p.stem for p in self.notes_dir.glob("*.md")}
        with self._connection() as conn:
            stored_content: Dict[str, str] = {
                row["id"]: row["content"]
                for row in conn.execute("SELECT id, content FROM notes")
            }
        db_ids = set(stored_content)
        common = file_stems & db_ids

        missing_from_index = sorted(file_stems - db_ids)
        missing_from_files = sorted(db_ids - file_stems)

        content_drift: List[str] = []
        unreadable_files: List[str] = []
        dangling_refs: List[str] = []
        seen_dangling: Set[str] = set()
        for note_id in sorted(common):
            file_path = self.notes_dir / f"{note_id}.md"
            try:
                # Read the file directly, bypassing the mtime-keyed note cache: a
                # consistency check must see the true on-disk bytes, not a cached
                # copy that may predate an external edit landing in the same
                # coarse mtime tick.
                with open(file_path, "r", encoding="utf-8") as f:
                    file_body = frontmatter.loads(f.read()).content
            except Exception:
                unreadable_files.append(note_id)
                continue
            # Compare the on-disk note body to exactly what the index stored.
            # create()/update() index the parsed-markdown body verbatim
            # (frontmatter.loads(markdown).content), so the file body and the
            # stored content are byte-identical when in sync. NOTE: do not compare
            # against a re-render via _note_to_markdown — that regenerates the
            # ## Links section and re-normalizes the heading, so every note with
            # links would falsely register as drifted.
            if file_body != stored_content.get(note_id):
                content_drift.append(note_id)
            # Wiki references (## Links entries AND inline prose refs) whose
            # id-shaped target no longer exists on disk. Informational — these
            # don't affect file/index sync, but each is a broken thread in the
            # knowledge graph.
            for match in _WIKI_LINK_RE.finditer(file_body):
                target_id = _normalize_wiki_target(match.group(1))
                if (
                    _NOTE_ID_RE.match(target_id)
                    and target_id != note_id
                    and target_id not in file_stems
                ):
                    entry = f"{note_id} -> {target_id}"
                    if entry not in seen_dangling:
                        seen_dangling.add(entry)
                        dangling_refs.append(entry)

        in_sync = len(common) - len(content_drift) - len(unreadable_files)
        return {
            "total_files": len(file_stems),
            "total_indexed": len(db_ids),
            "in_sync": in_sync,
            "missing_from_index": missing_from_index,
            "missing_from_files": missing_from_files,
            "content_drift": content_drift,
            "unreadable_files": unreadable_files,
            # Informational: broken wiki references; not counted in `consistent`
            # because they reflect vault content, not file/index drift.
            "dangling_refs": dangling_refs,
            "consistent": not (
                missing_from_index
                or missing_from_files
                or content_drift
                or unreadable_files
            ),
        }

    def _build_index_backup_path(self) -> Path:
        """Return a timestamped backup path for the index snapshot."""
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = self.index_db_path.with_name(
            f"{self.index_db_path.name}.{timestamp}.bak"
        )
        counter = 1
        while backup_path.exists():
            backup_path = self.index_db_path.with_name(
                f"{self.index_db_path.name}.{timestamp}.{counter}.bak"
            )
            counter += 1
        return backup_path

    def _create_index_backup(self) -> Optional[Path]:
        """Snapshot the current index before a rebuild.

        Uses SQLite's online backup API, so other connections (in this or any
        other process) keep working while it runs. Older backups beyond
        ``config.index_backup_keep`` are pruned.
        """
        with self._connection() as conn:
            if conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0:
                return None  # nothing worth backing up
            backup_path = backup_to(conn, self._build_index_backup_path())
        prune_backups(self.index_db_path, config.index_backup_keep)
        logger.info("Created index backup before reindex: %s", backup_path)
        return backup_path

    def _parse_rebuild_note(self, file_path: Path) -> Optional[Note]:
        """Parse one markdown file for rebuild, logging and skipping failures."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            return self._parse_note_from_markdown(content)
        except Exception as e:
            logger.error("Error processing file %s: %s", file_path, e)
            return None

    def rebuild_index(self) -> Optional[Path]:
        """Rebuild the index from all markdown files.

        The rebuild is the only path that fully reconciles the index with the
        files, so it must reflect *every* on-disk edit — including tags or links
        removed by hand. Every derived table is cleared and refilled from the
        parsed files inside ONE write transaction: readers (in any process) keep
        seeing the previous index until the commit, and a failure part-way rolls
        back to it untouched. ``retrieval_signals`` is not cleared — it exists
        only in the index, never in the files.

        Notes are embedded *before* the transaction opens (it is the slow step),
        reusing each stored vector whose model and source text are unchanged.
        Rows are written notes-first, then links, so a link is indexed even when
        its target appears later in file order.
        """
        # Reset up front so a failure before parsing completes (e.g. during backup
        # creation), or a concurrent reader, never sees a previous rebuild's list.
        self.last_rebuild_skipped = []
        # The corpus is changing, so cached term document frequencies are stale.
        self._fts_term_df_cache.clear()
        backup_path = self._create_index_backup()

        note_files = list(self.notes_dir.glob("*.md"))
        notes: List[Note] = []
        skipped: List[str] = []

        for file_path in note_files:
            note = self._parse_rebuild_note(file_path)
            if note is not None:
                notes.append(note)
            else:
                skipped.append(file_path.name)

        # Surface unparseable files instead of silently dropping them from the
        # index. The rebuilt graph contains only the files that parsed, so a parse
        # regression would otherwise quietly shrink the searchable corpus while
        # reporting success.
        self.last_rebuild_skipped = skipped
        if skipped:
            preview = ", ".join(skipped[:_REBUILD_SKIPPED_LOG_LIMIT])
            if len(skipped) > _REBUILD_SKIPPED_LOG_LIMIT:
                preview += f", … (+{len(skipped) - _REBUILD_SKIPPED_LOG_LIMIT} more)"
            logger.warning(
                "rebuild_index skipped %d unparseable note file(s): %s",
                len(skipped),
                preview,
            )

        embeddings = self._compute_embeddings(notes)
        with self._connection() as conn:
            with transaction(conn):
                # Children first; FTS rows are keyed by notes.pk, not cascaded.
                for table in ("note_fts", "embeddings", "links", "note_tags", "notes"):
                    conn.execute(f"DELETE FROM {table}")
                # Embeddings were just cleared: invalidate every process's cached
                # vector matrix even if nothing is re-written below (embeddings
                # disabled in this process, or an empty vault).
                bump_counter(conn, EMBEDDING_GENERATION_KEY)
                for note in notes:
                    self._write_note_rows(conn, note, note.content)
                self._write_links(conn, notes)
                self._write_embeddings(conn, embeddings)
                # Drop signals for notes that no longer exist on disk.
                conn.execute(
                    "DELETE FROM retrieval_signals "
                    "WHERE note_id NOT IN (SELECT id FROM notes)"
                )
            conn.execute("INSERT INTO note_fts(note_fts) VALUES ('optimize')")
            conn.execute("PRAGMA optimize")

        return backup_path

    # --- index row writers (call inside a transaction) -----------------------

    def _write_note_rows(
        self, conn: sqlite3.Connection, note: Note, content_for_db: str
    ) -> None:
        """Upsert a note's row, full-text entry and tags (not its links).

        ``ON CONFLICT DO UPDATE`` keeps the row (and its ``pk``) in place —
        ``INSERT OR REPLACE`` would delete it, and the cascade would wipe every
        other note's links *to* this one.
        """
        params = self._note_params(note, content_for_db)
        conn.execute(_NOTE_UPSERT_SQL, tuple(params[c] for c in _NOTE_COLUMNS))
        pk = conn.execute("SELECT pk FROM notes WHERE id = ?", (note.id,)).fetchone()[0]
        conn.execute("DELETE FROM note_fts WHERE rowid = ?", (pk,))
        conn.execute(
            "INSERT INTO note_fts(rowid, title, content, body) VALUES (?, ?, ?, ?)",
            (
                pk,
                fts_index_text(note.title),
                fts_index_text(content_for_db),
                fts_index_text(f"{note.title or ''}\n{content_for_db or ''}"),
            ),
        )
        conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note.id,))
        conn.executemany(
            "INSERT OR IGNORE INTO note_tags(note_id, tag) VALUES (?, ?)",
            [(note.id, tag.name) for tag in note.tags],
        )

    def _write_links(self, conn: sqlite3.Connection, notes: List[Note]) -> None:
        """Replace the outgoing links of *notes*.

        A link whose target note isn't indexed is skipped (not an error), as the
        graph index always did — the ``## Links`` line stays in the markdown and
        is picked up by a later rebuild once the target exists.
        """
        conn.executemany(
            "DELETE FROM links WHERE source_id = ?", [(note.id,) for note in notes]
        )
        conn.executemany(
            "INSERT INTO links(source_id, target_id, link_type, description, created_at) "
            "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM notes WHERE id = ?)",
            [
                (
                    row["source_id"],
                    row["target_id"],
                    row["link_type"],
                    row["description"],
                    ts_in(row["created_at"]),
                    row["target_id"],
                )
                for note in notes
                for row in self._link_rows_for_note(note)
            ],
        )

    @staticmethod
    def _link_rows_for_note(note: Note) -> List[Dict[str, Any]]:
        """Build LINKS_TO edge rows: explicit ## Links plus derived inline refs.

        Explicit links exclude the INLINE type (a graph-sourced Note can carry
        reconstructed inline links; re-deriving them from ``inline_refs`` keeps
        one source of truth). Inline refs to a target the note already links to
        explicitly are skipped by the parser, so no duplicate edges arise.
        """
        rows: List[Dict[str, Any]] = [
            {
                "source_id": note.id,
                "target_id": link.target_id,
                "link_type": link.link_type.value,
                "description": link.description,
                "created_at": link.created_at,
            }
            for link in note.links
            if link.link_type != LinkType.INLINE
        ]
        explicit_targets = {row["target_id"] for row in rows}
        for target_id in note.inline_refs:
            if target_id in explicit_targets or target_id == note.id:
                continue
            rows.append(
                {
                    "source_id": note.id,
                    "target_id": target_id,
                    "link_type": LinkType.INLINE.value,
                    "description": None,
                    # Inline refs carry no stored timestamp; the note's own
                    # updated_at is the closest durable approximation.
                    "created_at": note.updated_at,
                }
            )
        return rows

    def _parse_note_from_markdown(self, content: str) -> Note:
        """Parse a note from markdown content."""
        post = frontmatter.loads(content)
        metadata = post.metadata

        note_id = metadata.get("id")
        if not note_id:
            raise ValueError("Note ID missing from frontmatter")

        title = metadata.get("title")
        if not title:
            for line in post.content.strip().split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        if not title:
            raise ValueError("Note title missing from frontmatter or content")

        note_type_str = metadata.get("type", NoteType.PERMANENT.value)
        try:
            note_type = NoteType(note_type_str)
        except ValueError:
            note_type = NoteType.PERMANENT

        tags_str = metadata.get("tags", "")
        if isinstance(tags_str, str):
            tag_names = [t.strip() for t in tags_str.split(",") if t.strip()]
        elif isinstance(tags_str, list):
            tag_names = [str(t).strip() for t in tags_str if str(t).strip()]
        else:
            tag_names = []
        tag_names = _dedupe_preserve_order(tag_names)
        tags = [Tag(name=name) for name in tag_names]

        links = self.parse_links_in_content(note_id, post.content)
        inline_refs = self._parse_inline_refs(note_id, post.content, links)

        created_at = _coerce_datetime(metadata.get("created"), datetime.datetime.now())
        updated_at = _coerce_datetime(metadata.get("updated"), created_at)

        _action_keys = {
            "id", "title", "type", "tags", "created", "updated",
            "status", "source", "due_date", "priority", "recurrence_rule",
            "estimated_minutes", "remind_at", "project_id", "area_id",
            "origin", "last_verified",
        }

        status_str = metadata.get("status")
        status = None
        if status_str:
            try:
                status = NoteStatus(str(status_str))
            except ValueError:
                status = None

        source_str = metadata.get("source", NoteSource.MANUAL.value)
        try:
            source = NoteSource(source_str)
        except ValueError:
            source = NoteSource.MANUAL

        due_date_str = metadata.get("due_date")
        due_date = (
            datetime.date.fromisoformat(str(due_date_str)) if due_date_str else None
        )

        remind_at_str = metadata.get("remind_at")
        remind_at = (
            datetime.date.fromisoformat(str(remind_at_str)) if remind_at_str else None
        )

        priority = metadata.get("priority")
        recurrence_rule = metadata.get("recurrence_rule") or None
        estimated_minutes = metadata.get("estimated_minutes")
        project_id = metadata.get("project_id") or None
        area_id = metadata.get("area_id") or None
        origin = metadata.get("origin") or None
        last_verified_str = metadata.get("last_verified")
        last_verified = None
        if last_verified_str:
            try:
                if isinstance(last_verified_str, datetime.date):
                    last_verified = last_verified_str
                else:
                    last_verified = datetime.date.fromisoformat(
                        str(last_verified_str)
                    )
            except ValueError:
                last_verified = None

        return Note(
            id=note_id,
            title=title,
            content=post.content,
            note_type=note_type,
            tags=tags,
            links=links,
            created_at=created_at,
            updated_at=updated_at,
            metadata={k: v for k, v in metadata.items() if k not in _action_keys},
            status=status,
            source=source,
            due_date=due_date,
            priority=priority,
            recurrence_rule=recurrence_rule,
            estimated_minutes=estimated_minutes,
            remind_at=remind_at,
            project_id=project_id,
            area_id=area_id,
            origin=origin,
            last_verified=last_verified,
            inline_refs=inline_refs,
        )

    def parse_links_in_content(self, note_id: str, content: str) -> List[Link]:
        """Parse the ``## Links`` section of *content* into Link objects.

        Public so the service layer can reconcile a hand-edited ``## Links``
        section on update against the graph-backed links. A per-link
        ``<!-- created: ISO -->`` comment, written by the serializer, restores
        the link's original creation time; without one the parse falls back to
        now() (the historical behaviour).
        """
        links: List[Link] = []
        seen_link_keys: Set[Tuple[str, LinkType]] = set()
        links_section = False
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("## Links"):
                links_section = True
                continue
            if links_section and line.startswith("## "):
                links_section = False
                continue
            if links_section and line.startswith("- "):
                try:
                    line_content = line.strip()
                    if "[[" in line_content and "]]" in line_content:
                        parts = line_content.split("[[", 1)
                        link_type_str = parts[0].strip()
                        if link_type_str.startswith("- "):
                            link_type_str = link_type_str[2:].strip()
                        id_and_description = parts[1].split("]]", 1)
                        raw_target = id_and_description[0].strip()
                        target_id = _normalize_wiki_target(raw_target)
                        description = None
                        created_at = None
                        if len(id_and_description) > 1:
                            description = id_and_description[1]
                            created_match = _LINK_CREATED_RE.search(description)
                            if created_match:
                                try:
                                    created_at = datetime.datetime.fromisoformat(
                                        created_match.group(1)
                                    )
                                except ValueError:
                                    created_at = None
                                description = _LINK_CREATED_RE.sub(
                                    " ", description
                                )
                            description = description.strip() or None
                        try:
                            link_type = LinkType(link_type_str)
                        except ValueError:
                            link_type = LinkType.REFERENCE
                        if link_type == LinkType.INLINE:
                            # 'inline' is a derived type; a hand-written inline
                            # line in ## Links reads as a plain reference.
                            link_type = LinkType.REFERENCE
                        link_key = (target_id, link_type)
                        if link_key in seen_link_keys:
                            continue
                        seen_link_keys.add(link_key)
                        links.append(
                            Link(
                                source_id=note_id,
                                target_id=target_id,
                                link_type=link_type,
                                description=description,
                                created_at=created_at or datetime.datetime.now(),
                            )
                        )
                except Exception as e:
                    logger.error("Error parsing link: %s - %s", line, e)
        return links

    @staticmethod
    def inline_targets(note: Note) -> List[str]:
        """Every note ID ``[[linked]]`` in *note*'s prose, including targets it
        also links explicitly — which ``inline_refs`` leaves out to avoid a
        duplicate graph edge (e.g. an area's lane map naming its own members).
        """
        return NoteRepository._parse_inline_refs(note.id, note.content, [])

    @staticmethod
    def _parse_inline_refs(
        note_id: str, content: str, links: List[Link]
    ) -> List[str]:
        """Extract note IDs wiki-linked from prose (outside ``## Links``).

        Only id-shaped targets count — a ``[[Some Title]]`` style link is not a
        managed reference. Targets already covered by an explicit ## Links entry
        are skipped so the graph doesn't grow a duplicate edge. Fenced code
        blocks are ignored.
        """
        # Exclude INLINE links: a graph-sourced Note carries its inline edges in
        # ``links`` too, and treating those as "explicit" would suppress
        # re-deriving the very inline refs they represent (dropping the edge on
        # the next reindex). Only true ## Links entries should pre-empt a ref.
        explicit_targets = {
            link.target_id for link in links if link.link_type != LinkType.INLINE
        }
        refs: List[str] = []
        seen: Set[str] = set()
        links_section = False
        in_code_fence = False
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                continue
            if in_code_fence:
                continue
            if stripped.startswith("## Links"):
                links_section = True
                continue
            if links_section and stripped.startswith("## "):
                links_section = False
            if links_section:
                continue
            for match in _WIKI_LINK_RE.finditer(line):
                target_id = _normalize_wiki_target(match.group(1))
                if not _NOTE_ID_RE.match(target_id):
                    continue
                if (
                    target_id == note_id
                    or target_id in explicit_targets
                    or target_id in seen
                ):
                    continue
                seen.add(target_id)
                refs.append(target_id)
        return refs

    def _note_to_markdown(self, note: Note) -> str:
        """Convert a note to markdown with frontmatter."""
        metadata: Dict[str, Any] = {
            "id": note.id,
            "title": note.title,
            "type": note.note_type.value,
            "tags": [tag.name for tag in note.tags],
            "created": note.created_at.isoformat(),
            "updated": note.updated_at.isoformat(),
        }
        if note.status is not None:
            metadata["status"] = note.status.value
        if note.source != NoteSource.MANUAL:
            metadata["source"] = note.source.value
        if note.due_date is not None:
            metadata["due_date"] = note.due_date.isoformat()
        if note.priority is not None:
            metadata["priority"] = note.priority
        if note.recurrence_rule is not None:
            metadata["recurrence_rule"] = note.recurrence_rule
        if note.estimated_minutes is not None:
            metadata["estimated_minutes"] = note.estimated_minutes
        if note.remind_at is not None:
            metadata["remind_at"] = note.remind_at.isoformat()
        if note.project_id is not None:
            metadata["project_id"] = note.project_id
        if note.area_id is not None:
            metadata["area_id"] = note.area_id
        if note.origin is not None:
            metadata["origin"] = note.origin
        if note.last_verified is not None:
            metadata["last_verified"] = note.last_verified.isoformat()
        metadata.update(note.metadata)

        content_parts = []
        skip_section = False
        for line in note.content.split("\n"):
            if line.strip() == "## Links":
                skip_section = True
                continue
            elif skip_section and line.startswith("## "):
                skip_section = False
            if not skip_section:
                content_parts.append(line)

        content = "\n".join(content_parts).rstrip()
        content = self._ensure_title_heading(content, note.title)

        # INLINE links are derived from the prose body (they already live in
        # the content) and must never be rendered into ## Links.
        serializable_links = [
            link for link in note.links if link.link_type != LinkType.INLINE
        ]
        if serializable_links:
            unique_links: Dict[str, Link] = {}
            for link in serializable_links:
                key = f"{link.target_id}:{link.link_type.value}"
                unique_links[key] = link
            title_map = self._get_link_title_map(note, list(unique_links.values()))
            content += "\n\n## Links\n"
            for link in unique_links.values():
                desc = f" {link.description}" if link.description else ""
                target_ref = self._format_wiki_link_target(link.target_id, title_map)
                # Persist creation time as a markdown comment (invisible in
                # Obsidian preview) so link provenance survives index rebuilds.
                created = f" <!-- created: {link.created_at.isoformat(timespec='seconds')} -->"
                content += (
                    f"- {link.link_type.value} [[{target_ref}]]{desc}{created}\n"
                )

        post = frontmatter.Post(content, **metadata)
        return frontmatter.dumps(post)

    def _ensure_title_heading(self, content: str, title: str) -> str:
        """Keep the first meaningful line aligned with the note title."""
        heading = f"# {title}"
        if not content.strip():
            return heading

        lines = content.split("\n")
        first_meaningful = next(
            (i for i, line in enumerate(lines) if line.strip()), None
        )
        if first_meaningful is None:
            return heading

        if _LEADING_H1_RE.match(lines[first_meaningful]):
            lines[first_meaningful] = heading
            return "\n".join(lines).strip()

        body = "\n".join(lines).strip()
        return f"{heading}\n\n{body}" if body else heading

    def _get_link_title_map(
        self, note: Note, links: List[Link]
    ) -> Dict[str, str]:
        """Resolve link target titles in one index query for markdown serialisation."""
        target_ids = {link.target_id for link in links}
        if not target_ids:
            return {}

        with self._connection() as conn:
            title_map: Dict[str, str] = {
                row["id"]: row["title"]
                for row in conn.execute(
                    "SELECT id, title FROM notes "
                    "WHERE id IN (SELECT value FROM json_each(?))",
                    (_json_ids(target_ids),),
                )
            }
        if note.id in target_ids:
            title_map[note.id] = note.title
        return title_map

    def _format_wiki_link_target(
        self, target_id: str, title_map: Dict[str, str]
    ) -> str:
        """Render a wiki-link target with a safe alias when possible."""
        title = title_map.get(target_id)
        if not title:
            return target_id
        alias = " ".join(title.splitlines()).strip()
        if not alias or alias == target_id or "|" in alias or "]]" in alias:
            return target_id
        return f"{target_id}|{alias}"

    def _note_params(self, note: Note, content_for_db: str) -> Dict[str, Any]:
        """Build the column values for a ``notes`` row upsert."""
        return {
            "id": note.id,
            "title": note.title,
            "content": content_for_db,
            "note_type": note.note_type.value,
            "status": note.status.value if note.status else None,
            "source": (
                note.source.value if note.source != NoteSource.MANUAL else None
            ),
            "due_date": note.due_date.isoformat() if note.due_date else None,
            "priority": note.priority,
            "recurrence_rule": note.recurrence_rule,
            "estimated_minutes": note.estimated_minutes,
            "remind_at": note.remind_at.isoformat() if note.remind_at else None,
            "project_id": note.project_id,
            "area_id": note.area_id,
            "origin": note.origin,
            "last_verified": (
                note.last_verified.isoformat() if note.last_verified else None
            ),
            "metadata_json": (
                json.dumps(note.metadata, default=_json_default)
                if note.metadata
                else None
            ),
            "created_at": ts_in(note.created_at),
            "updated_at": ts_in(note.updated_at),
        }

    # --- Semantic embeddings (only active when a provider is configured) ------

    def _embedding_text(self, note: Note) -> str:
        """Return the text embedded for a note: its title plus prose body.

        The rendered ``## Links`` section is stripped before embedding: link
        lines are IDs and titles of OTHER notes, so they dilute the vector —
        and a container note (an area lists every direct member as a has_part
        line) would otherwise embed as a soup of member titles instead of its
        own meaning. Only an actual ``## Links`` heading line is matched, so the
        literal text appearing in prose never truncates the body.
        """
        body = _LINKS_HEADING_RE.split(note.content or "", maxsplit=1)[0]
        return f"{note.title or ''}\n\n{body}".strip()

    @staticmethod
    def _title_text(note: Note) -> str:
        """Return the note's title — embedded as a second 'atomic claim' vector."""
        return (note.title or "").strip()

    def _embedding_text_hash(self, note: Note) -> str:
        """Fingerprint of exactly the text embedded for a note (doc + title).

        A stored vector whose hash and model still match is reused on rebuild
        instead of re-embedding — the embed is by far the slowest rebuild step.
        """
        digest = hashlib.sha256()
        digest.update(self._embedding_text(note).encode("utf-8"))
        digest.update(b"\0")
        digest.update(self._title_text(note).encode("utf-8"))
        return digest.hexdigest()

    def _compute_embeddings(
        self, notes: List[Note]
    ) -> Dict[str, Tuple[List[float], List[float], str]]:
        """Return ``{note_id: (doc_vector, title_vector, text_hash)}`` for *notes*.

        Reuses each stored vector whose model and source text are unchanged and
        embeds only the rest. Runs outside any write transaction. Best-effort: if
        embedding fails (e.g. the model dependency is missing) the notes it
        couldn't embed are left out — the rebuild still completes, and search
        falls back to BM25 for them.
        """
        provider = self._embedding_provider
        if provider is None or not notes:
            return {}
        model_id = provider.model_id
        hashes = {note.id: self._embedding_text_hash(note) for note in notes}
        result: Dict[str, Tuple[List[float], List[float], str]] = {}
        with self._connection() as conn:
            for row in conn.execute(
                "SELECT note_id, doc_vec, title_vec, text_hash FROM embeddings "
                "WHERE model = ?",
                (model_id,),
            ):
                note_id = row["note_id"]
                expected = hashes.get(note_id)
                # A NULL stored hash (e.g. a vector imported from Kuzu) never
                # matches, so such vectors are re-embedded on the next rebuild.
                if expected is not None and expected == row["text_hash"] and row["title_vec"]:
                    result[note_id] = (
                        blob_to_vector(row["doc_vec"]).tolist(),
                        blob_to_vector(row["title_vec"]).tolist(),
                        row["text_hash"],
                    )
        todo = [note for note in notes if note.id not in result]
        if not todo:
            return result
        logger.info(
            "Embedding %d notes (%d reused unchanged; provider=%s, batch=%d)...",
            len(todo),
            len(result),
            model_id,
            # Log the configured batch size (clamped like the providers do)
            # rather than reaching into a provider-private attribute.
            max(1, int(getattr(config, "embedding_batch_size", 16))),
        )
        try:
            # Chunk the bulk embed so progress is visible during the (slow) embed
            # phase. Each chunk embeds its doc texts then its title texts in one
            # provider round-trip, then splits the result.
            for start in range(0, len(todo), _EMBED_LOG_CHUNK):
                chunk = todo[start : start + _EMBED_LOG_CHUNK]
                doc_texts = [self._embedding_text(note) for note in chunk]
                title_texts = [self._title_text(note) for note in chunk]
                all_vectors = provider.embed_documents(doc_texts + title_texts)
                if len(all_vectors) != 2 * len(chunk):
                    # A wrong count would pair vectors with the wrong notes.
                    raise RuntimeError(
                        f"Embedding provider returned {len(all_vectors)} vectors "
                        f"for {2 * len(chunk)} expected (doc+title)."
                    )
                for i, note in enumerate(chunk):
                    result[note.id] = (
                        all_vectors[i],
                        all_vectors[len(chunk) + i],
                        hashes[note.id],
                    )
                logger.info(
                    "  embedded %d/%d notes", min(start + len(chunk), len(todo)), len(todo)
                )
        except Exception as exc:
            logger.warning(
                "Embedding failed for some notes; they are indexed without "
                "vectors (search falls back to BM25 for them): %s",
                exc,
            )
        return result

    def _embed_note(self, note: Note) -> Optional[Tuple[List[float], List[float], str]]:
        """Embed one note for create/update, or None when unavailable.

        Called *before* the write transaction opens, so a slow model never
        holds the database write lock. Best-effort: a failure (e.g. a missing
        model) is logged and never blocks the write.
        """
        provider = self._embedding_provider
        if provider is None:
            return None
        try:
            vector, title_vector = provider.embed_documents(
                [self._embedding_text(note), self._title_text(note)]
            )
        except Exception as exc:
            logger.warning("Could not embed note %s: %s", note.id, exc)
            return None
        return vector, title_vector, self._embedding_text_hash(note)

    def _write_embeddings(
        self,
        conn: sqlite3.Connection,
        embeddings: Dict[str, Tuple[List[float], List[float], str]],
    ) -> None:
        """Upsert vectors and bump the generation so every process reloads them."""
        if not embeddings or self._embedding_provider is None:
            return
        model_id = self._embedding_provider.model_id
        now = ts_in(datetime.datetime.now())
        conn.executemany(
            "INSERT INTO embeddings"
            "(note_id, model, dim, doc_vec, title_vec, text_hash, embedded_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM notes WHERE id = ?) "
            "ON CONFLICT(note_id) DO UPDATE SET model = excluded.model, "
            "dim = excluded.dim, doc_vec = excluded.doc_vec, "
            "title_vec = excluded.title_vec, text_hash = excluded.text_hash, "
            "embedded_at = excluded.embedded_at",
            [
                (
                    note_id,
                    model_id,
                    len(doc),
                    vector_to_blob(doc),
                    vector_to_blob(title),
                    text_hash,
                    now,
                    note_id,
                )
                for note_id, (doc, title, text_hash) in embeddings.items()
            ],
        )
        bump_counter(conn, EMBEDDING_GENERATION_KEY)

    def _search_by_vector(
        self, query_vector: List[float], limit: int
    ) -> Dict[str, float]:
        """Return {note_id: distance} (lower = closer) for a precomputed vector.

        An exact scan over every current-model vector (doc and title; the closer
        of the two counts), so there is no approximate index to go stale.
        """
        if self._embedding_provider is None:
            return {}
        try:
            with self._connection() as conn:
                return self._vectors.search(
                    conn, query_vector, self._embedding_provider.model_id, max(1, limit)
                )
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return {}

    def vector_search(self, text: str, limit: int = 50) -> List[Tuple[str, float]]:
        """Nearest notes to *text* as (note_id, distance) pairs, closest first.

        Embeds *text* as a search query, then runs :meth:`_search_by_vector`.
        Returns ``[]`` when embeddings are disabled or the query cannot be
        embedded, so callers cleanly fall back to BM25 / lexical behaviour.
        """
        provider = self._embedding_provider
        if provider is None or not text or not text.strip():
            return []
        try:
            query_vector = provider.embed_query(text)
        except Exception as exc:
            logger.warning("Query embedding failed; skipping vector search: %s", exc)
            return []
        distances = self._search_by_vector(query_vector, limit)
        return sorted(distances.items(), key=lambda kv: kv[1])[:limit]

    def vector_search_ids(self, text: str, limit: int = 50) -> List[str]:
        """Nearest note ids to *text*, closest first (see :meth:`vector_search`)."""
        return [note_id for note_id, _distance in self.vector_search(text, limit)]

    def vector_search_by_vector(
        self, query_vector: List[float], limit: int = 50
    ) -> List[Tuple[str, float]]:
        """Nearest notes to a precomputed embedding, as (note_id, distance) pairs.

        Used by note-to-note similarity, which queries with a note's own stored
        document vector instead of re-embedding its text as a search query.
        """
        if not query_vector:
            return []
        distances = self._search_by_vector(query_vector, limit)
        return sorted(distances.items(), key=lambda kv: kv[1])[:limit]

    def embed_query(self, text: str) -> Optional[List[float]]:
        """Embed *text* as a search query (L2-normalized), or None if unavailable.

        Thin public accessor over the embedding provider so services can rank
        arbitrary text (tags, areas) against the same vector space the index uses,
        without reaching into the private provider. None when embeddings are off
        or the text can't be embedded.
        """
        provider = self._embedding_provider
        if provider is None or not text or not text.strip():
            return None
        try:
            return provider.embed_query(text)
        except Exception as exc:
            logger.warning("Query embedding failed: %s", exc)
            return None

    def embed_documents(self, texts: Iterable[str]) -> List[List[float]]:
        """Embed *texts* as documents (L2-normalized); [] when embeddings are off."""
        provider = self._embedding_provider
        items = [t for t in texts]
        if provider is None or not items:
            return []
        try:
            return provider.embed_documents(items)
        except Exception as exc:
            logger.warning("Document embedding failed: %s", exc)
            return []

    def embed_tags(self, names: Iterable[str]) -> Dict[str, List[float]]:
        """Return ``{tag_name: vector}`` for *names*, cached per embedding model.

        Tags change rarely, so caching their vectors means a repeated semantic
        tag search only embeds the (usually empty) set of new tags plus the one
        query — not the whole tag list each call. The cache resets if the active
        embedding model changes (stale vectors would be incomparable).
        """
        provider = self._embedding_provider
        wanted = [n for n in names]
        if provider is None or not wanted:
            return {}
        model_id = provider.model_id
        if getattr(self, "_tag_embed_cache_model", None) != model_id:
            self._tag_embed_cache: Dict[str, List[float]] = {}
            self._tag_embed_cache_model = model_id
        cache = self._tag_embed_cache
        missing = [n for n in dict.fromkeys(wanted) if n not in cache]
        if missing:
            try:
                vectors = provider.embed_documents(missing)
            except Exception as exc:
                logger.warning("Tag embedding failed: %s", exc)
                vectors = []
            if len(vectors) != len(missing):
                logger.warning(
                    "Tag embedding count mismatch: expected %d, got %d (caching "
                    "only the returned prefix; the rest re-embed next call)",
                    len(missing),
                    len(vectors),
                )
            for name, vec in zip(missing, vectors):
                cache[name] = vec
        return {n: cache[n] for n in wanted if n in cache}

    def get_note_embedding(self, note_id: str) -> Optional[List[float]]:
        """Return a note's stored *current-model* document embedding.

        Only returns a vector produced by the active model — a stale vector from
        a previous model would be incompatible with ``_search_by_vector`` (which
        searches current-model vectors). Returns ``None`` when embeddings are
        disabled or no current-model vector is stored yet.
        """
        if self._embedding_provider is None:
            return None
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT doc_vec FROM embeddings WHERE note_id = ? AND model = ?",
                    (note_id, self._embedding_provider.model_id),
                ).fetchone()
        except Exception as exc:
            logger.warning("Could not read embedding for %s: %s", note_id, exc)
            return None
        return blob_to_vector(row["doc_vec"]).tolist() if row else None

    def incoming_knowledge_link_ids(
        self,
        note_id: str,
        routing_link_values: Iterable[str],
        *,
        exclude_reference: bool = False,
    ) -> Set[str]:
        """Source ids of incoming *knowledge* links to *note_id*.

        Excludes PARA/GTD routing link types (``routing_link_values``) and — for
        an area note (``exclude_reference``) — incoming ``reference`` links, since
        every member note references its area. Without this, a parent area/project
        (which every child links to) would count all its children as structurally
        similar, the scaffolding-dominates-the-graph problem.
        """
        sql = (
            "SELECT DISTINCT source_id FROM links WHERE target_id = ? "
            "AND link_type NOT IN (SELECT value FROM json_each(?))"
        )
        params: List[Any] = [note_id, _json_ids(routing_link_values)]
        if exclude_reference:
            sql += " AND link_type <> ?"
            params.append(LinkType.REFERENCE.value)
        with self._connection() as conn:
            return set(_first_column(conn.execute(sql, params)))

    def _index_note(
        self, note: Note, rendered_content: Optional[str] = None
    ) -> None:
        """Upsert a note, its tags, links and embedding into the index.

        One write transaction, so other processes see the whole change or none
        of it. The embedding is computed first, outside the transaction.
        """
        content_for_db = (
            rendered_content if rendered_content is not None else note.content
        )
        # Embed the body as written to the file — the same text a rebuild parses
        # back — so a note's vector doesn't change (and can be reused) on rebuild.
        embedding = self._embed_note(
            note
            if rendered_content is None
            else note.model_copy(update={"content": rendered_content})
        )
        with self._connection() as conn:
            with transaction(conn):
                self._write_note_rows(conn, note, content_for_db)
                self._write_links(conn, [note])
                if embedding is not None:
                    self._write_embeddings(conn, {note.id: embedding})

    def _fetch_notes_by_ids(
        self, conn: sqlite3.Connection, ids: List[str]
    ) -> List[Note]:
        """Reconstruct full Note objects for the given IDs from the index."""
        if not ids:
            return []
        ids_json = _json_ids(ids)

        notes_data = [
            dict(row)
            for row in conn.execute(
                f"SELECT {_NOTE_SELECT} FROM notes n "
                "WHERE n.id IN (SELECT value FROM json_each(?))",
                (ids_json,),
            )
        ]
        if not notes_data:
            return []

        tags_by_note: Dict[str, List[str]] = defaultdict(list)
        for row in conn.execute(
            "SELECT note_id, tag FROM note_tags "
            "WHERE note_id IN (SELECT value FROM json_each(?))",
            (ids_json,),
        ):
            tags_by_note[row["note_id"]].append(row["tag"])

        links_by_note: Dict[str, List[sqlite3.Row]] = defaultdict(list)
        for row in conn.execute(
            "SELECT source_id, target_id, link_type, description, created_at "
            "FROM links WHERE source_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY pk",
            (ids_json,),
        ):
            links_by_note[row["source_id"]].append(row)

        note_map: Dict[str, Note] = {}
        for nd in notes_data:
            note_id = nd["id"]
            tags = [Tag(name=name) for name in tags_by_note.get(note_id, [])]
            links = [
                Link(
                    source_id=note_id,
                    target_id=row["target_id"],
                    link_type=LinkType(row["link_type"]),
                    description=row["description"],
                    created_at=ts_out(row["created_at"]) or datetime.datetime.now(),
                )
                for row in links_by_note.get(note_id, [])
            ]
            note = _db_dict_to_note(nd, tags, links)
            # Mirror inline edges into inline_refs so a graph-sourced note that
            # gets re-indexed re-derives the same inline rows it carried.
            note.inline_refs = [
                link.target_id
                for link in links
                if link.link_type == LinkType.INLINE
            ]
            note_map[note_id] = note
        return [note_map[note_id] for note_id in ids if note_id in note_map]

    def _query_fts_index_scored(
        self,
        conn: sqlite3.Connection,
        column: str,
        query: str,
        *,
        conjunctive: bool = False,
    ) -> List[Tuple[str, float]]:
        """Return (note_id, BM25 score) pairs from a full-text query, best first.

        *column* is ``"body"`` (title + content), ``"title"`` or ``"content"``.
        Scores are FTS5's ``bm25()`` negated, so higher is better and every match
        scores > 0. The query is reduced to plain word tokens (stopwords removed)
        and each is quoted, so no user input can inject FTS5 query syntax.
        """
        expression = fts_match_expression(
            fts_terms(query), column, conjunctive=conjunctive
        )
        if expression is None:
            return []
        weights = ", ".join(str(w) for w in FTS_WEIGHTS[column])
        rows = conn.execute(
            f"SELECT n.id, -bm25(note_fts, {weights}) AS score "
            "FROM note_fts JOIN notes n ON n.pk = note_fts.rowid "
            "WHERE note_fts MATCH ? ORDER BY score DESC, n.id",
            (expression,),
        )
        return [(row[0], float(row[1])) for row in rows]

    def _query_fts_index(
        self,
        conn: sqlite3.Connection,
        column: str,
        query: str,
        *,
        conjunctive: bool = False,
    ) -> List[str]:
        """Return note IDs from a full-text search query ordered by score."""
        return [
            note_id
            for note_id, _score in self._query_fts_index_scored(
                conn, column, query, conjunctive=conjunctive
            )
        ]

    def _term_document_frequency(self, conn: sqlite3.Connection, term: str) -> int:
        """Number of notes whose indexed text contains *term* (cached per term).

        Counted through the FTS index itself, so the term is stemmed exactly as
        at index time. Cached and invalidated on writes, so the cost is paid at
        most once per distinct term between corpus changes.
        """
        cached = self._fts_term_df_cache.get(term)
        if cached is not None:
            return cached
        df = 0
        expression = fts_match_expression(fts_terms(term), "body", conjunctive=True)
        if expression is not None:
            df = int(
                conn.execute(
                    "SELECT count(*) FROM note_fts WHERE note_fts MATCH ?",
                    (expression,),
                ).fetchone()[0]
            )
        self._fts_term_df_cache[term] = df
        return df

    def _invalidate_fts_term_df_cache(self) -> None:
        """Drop cached term document frequencies after a corpus change.

        A note create/update/delete shifts DFs; without this a term cached as 0
        (non-occurring) stays dropped from a long query's term budget after it
        first appears, and a now-common term cached low escapes pruning — both
        until the next rebuild. Cheap (the cache is small), so just clear it.
        """
        self._fts_term_df_cache.clear()

    def _focus_query_terms(self, conn: sqlite3.Connection, query: str) -> str:
        """Reduce a long text FTS query to its most discriminative (lowest-DF) terms.

        A long query with many moderately-common terms matches a large fraction
        of the corpus, burying the best match among weak ones (Kuzu's FTS went
        further and dropped it outright, because common terms scored negative).
        Keeping only the ``fts_max_query_terms`` lowest-DF (highest-IDF) terms
        bounds the match set so the best lexical hit stays findable. Short/narrow
        queries are untouched and pay no DF cost; the vector half of hybrid search
        still embeds the FULL query, so semantic recall is unchanged.
        """
        max_terms = config.fts_max_query_terms
        if max_terms <= 0:
            return query
        tokens = re.findall(r"\w+", query or "")
        unique = fts_terms(query)
        if len(unique) <= max_terms:
            return query  # already narrow enough — no DF lookups needed
        df = {t: self._term_document_frequency(conn, t) for t in unique}
        # Keep the most discriminative terms that actually occur (df >= 1): a df-0
        # term matches nothing, so it can never help and must not consume budget.
        occurring = sorted((t for t in unique if df[t] >= 1), key=lambda t: df[t])
        keep = set(occurring[:max_terms])
        if not keep:
            return query  # nothing occurs in the corpus — leave the query as-is
        logger.debug(
            "FTS query focused to %d of %d distinct terms (lowest DF)",
            len(keep),
            len(unique),
        )
        return " ".join(t for t in tokens if t.lower() in keep)

    def text_fts_scores(self, text: str) -> Dict[str, float]:
        """Return {note_id: BM25 score} for *text* against the combined note index.

        Exposes the relevance scores the full-text index already computes so
        callers can rank by BM25 instead of re-deriving order with lexical
        substring heuristics.
        """
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized:
            return {}
        with self._connection() as conn:
            return {
                note_id: score
                for note_id, score in self._query_fts_index_scored(
                    conn, "body", normalized
                )
            }

    def _intersect_ordered_id_lists(self, ordered_id_lists: List[List[str]]) -> List[str]:
        """Intersect ordered ID lists while preserving the first list's order."""
        if not ordered_id_lists:
            return []

        common_ids = set(ordered_id_lists[0])
        for ids in ordered_id_lists[1:]:
            common_ids &= set(ids)

        seen: Set[str] = set()
        ordered_common_ids: List[str] = []
        for note_id in ordered_id_lists[0]:
            if note_id in common_ids and note_id not in seen:
                ordered_common_ids.append(note_id)
                seen.add(note_id)
        return ordered_common_ids

    def _candidate_ids_and_scores(
        self, conn: sqlite3.Connection, kwargs: Dict[str, Any]
    ) -> Tuple[Optional[List[str]], Dict[str, float]]:
        """Return (FTS candidate IDs, BM25 score map) for text filters in *kwargs*.

        The score map is taken from the *first* text-oriented index queried — the
        same index whose ordering drives the candidate list (see
        ``_intersect_ordered_id_lists``, which preserves the first list's order).
        Returning scores from that index, rather than always the combined
        title+content index, means a title-only or content-only search is never
        re-ranked with scores from a different index than produced its candidates.

        Returns ``(None, {})`` when no text filter is present (so callers fall
        back to structural ordering with no BM25 scores).
        """
        ordered_scored: List[List[Tuple[str, float]]] = []

        text_query = kwargs.get("text")
        if isinstance(text_query, str):
            # Reduce a long query to its most discriminative terms so it doesn't
            # match most of the corpus and bury its best lexical hit.
            ordered_scored.append(
                self._query_fts_index_scored(
                    conn, "body", self._focus_query_terms(conn, text_query)
                )
            )

        title_query = kwargs.get("title")
        if isinstance(title_query, str):
            ordered_scored.append(
                self._query_fts_index_scored(
                    conn, "title", title_query, conjunctive=True
                )
            )

        content_query = kwargs.get("content")
        if isinstance(content_query, str):
            ordered_scored.append(
                self._query_fts_index_scored(
                    conn, "content", content_query, conjunctive=True
                )
            )

        if not ordered_scored:
            return None, {}

        ordered_id_lists = [[note_id for note_id, _ in pairs] for pairs in ordered_scored]
        candidate_ids = self._intersect_ordered_id_lists(ordered_id_lists)
        score_by_id = {note_id: score for note_id, score in ordered_scored[0]}
        return candidate_ids, score_by_id

    def create(self, note: Note) -> Note:
        """Create a new note."""
        self._assert_writable()
        self._invalidate_fts_term_df_cache()
        if not note.id:
            from parazettel_mcp.models.schema import generate_id

            note.id = generate_id()

        markdown = self._note_to_markdown(note)
        file_path = self.notes_dir / f"{note.id}.md"
        self._write_markdown_atomically(file_path, markdown)

        rendered_body = frontmatter.loads(markdown).content
        # Derive inline prose refs from the rendered body so [[id]] mentions in
        # freshly created content are indexed immediately, not on the next parse.
        note.inline_refs = self._parse_inline_refs(note.id, rendered_body, note.links)
        self._index_note(note, rendered_content=rendered_body)
        return note

    def get(self, id: str) -> Optional[Note]:
        """Get a note by ID (reads the markdown file; uses an LRU cache)."""
        file_path = self.notes_dir / f"{id}.md"
        if not file_path.exists():
            return None
        try:
            path_str = str(file_path)
            mtime_ns = file_path.stat().st_mtime_ns
            cached = _cache_get(path_str, mtime_ns)
            if cached is not None:
                return cached
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            note = self._parse_note_from_markdown(content)
            _cache_put(path_str, mtime_ns, note)
            return note.model_copy(deep=True)
        except Exception as e:
            raise IOError(f"Failed to read note {id}: {e}") from e

    def get_by_title(self, title: str) -> Optional[Note]:
        """Get a note by exact title (indexed lookup)."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id FROM notes WHERE title = ? ORDER BY pk LIMIT 1", (title,)
            ).fetchone()
        if row is None:
            return None
        return self.get(row[0])

    def get_all(self) -> List[Note]:
        """Return all notes, reconstructed from the index."""
        with self._connection() as conn:
            ids = _first_column(conn.execute("SELECT id FROM notes ORDER BY pk"))
            return self._fetch_notes_by_ids(conn, ids)

    def update(self, note: Note) -> Note:
        """Update a note."""
        self._assert_writable()
        return self._update_note(note)

    def update_preserving_updated_at(
        self,
        note: Note,
        *,
        existing_note: Note,
        existing_links_source: Optional[Note] = None,
    ) -> Note:
        """Rewrite derived markdown while keeping stable timestamps."""
        return self._update_note(
            note,
            preserve_updated_at=True,
            existing_note=existing_note,
            existing_links_source=existing_links_source,
        )

    def _preserve_link_created_at(self, note: Note, existing_note: Note) -> None:
        """Carry graph-backed link creation timestamps onto rewritten file-backed notes."""
        exact_timestamps = {
            (link.target_id, link.link_type, link.description): link.created_at
            for link in existing_note.links
        }
        fallback_timestamps = {
            (link.target_id, link.link_type): link.created_at
            for link in existing_note.links
        }

        preserved_links = []
        for link in note.links:
            created_at = exact_timestamps.get(
                (link.target_id, link.link_type, link.description)
            )
            if created_at is None:
                created_at = fallback_timestamps.get((link.target_id, link.link_type))

            if created_at is None or created_at == link.created_at:
                preserved_links.append(link)
                continue

            preserved_links.append(link.model_copy(update={"created_at": created_at}))

        note.links = preserved_links

    def _update_note(
        self,
        note: Note,
        *,
        preserve_updated_at: bool = False,
        existing_note: Optional[Note] = None,
        existing_links_source: Optional[Note] = None,
    ) -> Note:
        """Internal update implementation."""
        self._invalidate_fts_term_df_cache()
        if existing_note is None:
            existing_note = self.get(note.id)
        if not existing_note:
            raise ValueError(f"Note with ID {note.id} does not exist")

        if preserve_updated_at:
            note.updated_at = existing_note.updated_at
            self._preserve_link_created_at(
                note, existing_links_source or existing_note
            )
        else:
            note.updated_at = datetime.datetime.now()

        markdown = self._note_to_markdown(note)
        file_path = self.notes_dir / f"{note.id}.md"
        self._write_markdown_atomically(file_path, markdown)
        _cache_evict(str(file_path))

        rendered_body = frontmatter.loads(markdown).content
        # Re-derive inline prose refs from the body actually written, so edits
        # that add/remove [[id]] mentions update the graph on this same write.
        note.inline_refs = self._parse_inline_refs(note.id, rendered_body, note.links)
        try:
            self._index_note(note, rendered_content=rendered_body)
        except Exception as e:
            logger.error("Failed to update note in the index: %s", e)
            raise

        return note

    def delete(self, id: str) -> None:
        """Delete a note by ID.

        Incoming references are scrubbed from every source note: the matching
        ``## Links`` entries are removed, and inline ``[[id]]`` / ``[[id|alias]]``
        wiki-links in prose are unlinked in place — replaced by their alias text
        (or the deleted note's title) so the sentence stays readable but no
        dangling reference remains.
        """
        self._assert_writable()
        self._invalidate_fts_term_df_cache()
        file_path = self.notes_dir / f"{id}.md"
        if not file_path.exists():
            raise ValueError(f"Note with ID {id} does not exist")

        # Read the note before removal so inline refs can be replaced with its
        # title when they carry no alias. A corrupted/unparseable note must
        # still be deletable, so fall back to its id as the replacement text.
        try:
            deleted_note = self.get(id)
        except Exception:
            deleted_note = None
        deleted_title = deleted_note.title if deleted_note else id

        # Incoming sources come from the graph, which includes inline edges, so
        # prose-only referencers are scrubbed too.
        source_notes = self.find_linked_notes(id, "incoming")

        with self.file_lock:
            os.remove(file_path)
        _cache_evict(str(file_path))

        # Match every inline form that normalizes to this id — bare, aliased,
        # and with a ".md"/"#fragment" suffix — so none dangle after delete.
        inline_ref_pattern = re.compile(
            r"\[\[\s*" + re.escape(id) + WIKI_TARGET_SUFFIX
            + r"(?:\|([^\]]*))?\s*\]\]"
        )

        def _unlink_ref(match: "re.Match[str]") -> str:
            """Replace a deleted note's inline ref with its alias text, else the deleted note's title."""
            alias = (match.group(1) or "").strip()
            return alias or deleted_title

        for source_note in source_notes:
            # Best-effort scrub: an unreadable/unparseable source note must not
            # abort the delete and leave the vault half-scrubbed.
            try:
                file_backed_source = self.get(source_note.id)
            except Exception as exc:
                logger.warning(
                    "Could not scrub references to %s in source %s during "
                    "delete: %s",
                    id,
                    source_note.id,
                    exc,
                )
                continue
            if not file_backed_source:
                continue
            existing_source = file_backed_source.model_copy(deep=True)
            file_backed_source.remove_link(id)
            file_backed_source.content = inline_ref_pattern.sub(
                _unlink_ref, file_backed_source.content
            )
            file_backed_source.inline_refs = [
                ref for ref in file_backed_source.inline_refs if ref != id
            ]
            self.update_preserving_updated_at(
                file_backed_source,
                existing_note=existing_source,
                existing_links_source=source_note,
            )

        with self._connection() as conn:
            with transaction(conn):
                conn.execute(
                    "DELETE FROM note_fts WHERE rowid = "
                    "(SELECT pk FROM notes WHERE id = ?)",
                    (id,),
                )
                # Cascades to its tags, embedding, and links in both directions.
                conn.execute("DELETE FROM notes WHERE id = ?", (id,))
                conn.execute("DELETE FROM retrieval_signals WHERE note_id = ?", (id,))
                bump_counter(conn, EMBEDDING_GENERATION_KEY)

    def search(self, **kwargs: Any) -> List[Note]:
        """Search for notes based on criteria.

        Supported keyword arguments
        ---------------------------
        text, content, title, note_type, tag, tags, linked_to, linked_from,
        created_after, created_before, updated_after, updated_before,
        status, source, due_date_before, due_date_after, priority,
        remind_at_before, remind_at_after, project_id, area_id
        """
        notes, _scores = self._search_with_scores(**kwargs)
        return notes

    def search_scored(self, **kwargs: Any) -> Tuple[List[Note], Dict[str, float]]:
        """Like :meth:`search`, but also return the BM25 score map in one FTS pass.

        Avoids the previous two-query pattern (one FTS call for candidates, a
        second for scores). The scores come from the same index that produced the
        candidate ordering. For a non-text search the score map is empty.
        """
        return self._search_with_scores(**kwargs)

    def _search_with_scores(
        self, **kwargs: Any
    ) -> Tuple[List[Note], Dict[str, float]]:
        """Shared search implementation returning notes plus their BM25 scores."""
        with self._connection() as conn:
            candidate_ids, bm25_scores = self._candidate_ids_and_scores(conn, kwargs)
            if candidate_ids == []:
                return [], {}

            # Every clause below is a fixed string; all values are bound params.
            where_parts: List[str] = []
            params: List[Any] = []

            if "tag" in kwargs:
                where_parts.append(
                    "EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id AND t.tag = ?)"
                )
                params.append(kwargs["tag"])
            elif "tags" in kwargs:
                tag_names = kwargs["tags"]
                if isinstance(tag_names, list):
                    where_parts.append(
                        "EXISTS (SELECT 1 FROM note_tags t WHERE t.note_id = n.id "
                        "AND t.tag IN (SELECT value FROM json_each(?)))"
                    )
                    params.append(_json_ids(tag_names))

            if "linked_to" in kwargs:
                where_parts.append(
                    "EXISTS (SELECT 1 FROM links l WHERE l.source_id = n.id "
                    "AND l.target_id = ?)"
                )
                params.append(kwargs["linked_to"])

            if "linked_from" in kwargs:
                where_parts.append(
                    "EXISTS (SELECT 1 FROM links l WHERE l.target_id = n.id "
                    "AND l.source_id = ?)"
                )
                params.append(kwargs["linked_from"])

            if candidate_ids is not None:
                where_parts.append("n.id IN (SELECT value FROM json_each(?))")
                params.append(_json_ids(candidate_ids))

            _scalar: Dict[str, str] = {
                "note_type": "n.note_type = ?",
                "status": "n.status = ?",
                "source": "n.source = ?",
                "priority": "n.priority = ?",
                "project_id": "n.project_id = ?",
                "area_id": "n.area_id = ?",
            }
            for kwarg, clause in _scalar.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    v = kwargs[kwarg]
                    params.append(v.value if hasattr(v, "value") else v)

            _ts: Dict[str, str] = {
                "created_after": "n.created_at >= ?",
                "created_before": "n.created_at <= ?",
                "updated_after": "n.updated_at >= ?",
                "updated_before": "n.updated_at <= ?",
            }
            for kwarg, clause in _ts.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    v = kwargs[kwarg]
                    params.append(
                        ts_in(v) if isinstance(v, (datetime.date, datetime.datetime)) else v
                    )

            _date: Dict[str, str] = {
                "due_date_before": "n.due_date <= ?",
                "due_date_after": "n.due_date >= ?",
                "remind_at_before": "n.remind_at <= ?",
                "remind_at_after": "n.remind_at >= ?",
            }
            for kwarg, clause in _date.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    v = kwargs[kwarg]
                    params.append(v.isoformat() if hasattr(v, "isoformat") else v)

            sql = "SELECT n.id FROM notes n"
            if where_parts:
                sql += " WHERE " + " AND ".join(where_parts)
            sql += " ORDER BY n.pk"

            ids = _first_column(conn.execute(sql, params))
            if candidate_ids is not None:
                id_set = set(ids)
                ids = [note_id for note_id in candidate_ids if note_id in id_set]

            return self._fetch_notes_by_ids(conn, ids), bm25_scores

    def find_by_tag(self, tag: Union[str, Tag]) -> List[Note]:
        """Find notes by tag."""
        tag_name = tag.name if isinstance(tag, Tag) else tag
        return self.search(tag=tag_name)

    def find_linked_notes(
        self, note_id: str, direction: str = "outgoing"
    ) -> List[Note]:
        """Find notes linked to/from the given note."""
        outgoing_sql = (
            "SELECT target_id FROM links WHERE source_id = ? "
            "GROUP BY target_id ORDER BY min(pk)"
        )
        incoming_sql = (
            "SELECT source_id FROM links WHERE target_id = ? "
            "GROUP BY source_id ORDER BY min(pk)"
        )
        with self._connection() as conn:
            if direction == "outgoing":
                ids = _first_column(conn.execute(outgoing_sql, (note_id,)))
            elif direction == "incoming":
                ids = _first_column(conn.execute(incoming_sql, (note_id,)))
            elif direction == "both":
                ids_set: Set[str] = set(
                    _first_column(conn.execute(outgoing_sql, (note_id,)))
                )
                ids_set.update(_first_column(conn.execute(incoming_sql, (note_id,))))
                ids_set.discard(note_id)
                ids = list(ids_set)
            else:
                raise ValueError(
                    f"Invalid direction: {direction}. "
                    "Use 'outgoing', 'incoming', or 'both'"
                )

            return self._fetch_notes_by_ids(conn, ids)

    def get_all_tags(self) -> List[Tag]:
        """Return every tag currently applied to at least one note.

        Derived from note membership, so a tag removed from its last note simply
        disappears.
        """
        with self._connection() as conn:
            names = _first_column(
                conn.execute("SELECT DISTINCT tag FROM note_tags ORDER BY tag")
            )
        return [Tag(name=name) for name in names]

    def get_link(
        self, source_id: str, target_id: str, link_type: str
    ) -> Optional[Dict[str, Any]]:
        """Return the stored properties of a specific directed link, or None."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT created_at FROM links "
                "WHERE source_id = ? AND target_id = ? AND link_type = ? "
                "ORDER BY pk LIMIT 1",
                (source_id, target_id, link_type),
            ).fetchone()
        if row is None:
            return None
        return {"created_at": ts_out(row["created_at"])}

    def record_retrieval(self, note_ids: List[str]) -> None:
        """Bump retrieval signals (last_retrieved_at, hit_count) for *note_ids*.

        Index-only operational data: deliberately NOT written to markdown, so
        reading a note never rewrites its file; the table is never cleared by a
        rebuild. Best-effort — a read must never fail because a hit counter
        couldn't be written (e.g. another process held the write lock too long).
        """
        if not note_ids:
            return
        now = ts_in(datetime.datetime.now())
        try:
            with self._connection() as conn:
                with transaction(conn):
                    conn.executemany(
                        "INSERT INTO retrieval_signals(note_id, hit_count, last_retrieved_at) "
                        "SELECT ?, 1, ? WHERE EXISTS (SELECT 1 FROM notes WHERE id = ?) "
                        "ON CONFLICT(note_id) DO UPDATE SET "
                        "hit_count = hit_count + 1, "
                        "last_retrieved_at = excluded.last_retrieved_at",
                        [(note_id, now, note_id) for note_id in dict.fromkeys(note_ids)],
                    )
        except sqlite3.OperationalError as exc:
            logger.warning("Could not record retrieval signals: %s", exc)

    def get_retrieval_signals(
        self, note_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Return {note_id: {last_retrieved_at, hit_count}} for ids with signals."""
        if not note_ids:
            return {}
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT note_id, last_retrieved_at, hit_count FROM retrieval_signals "
                "WHERE note_id IN (SELECT value FROM json_each(?))",
                (_json_ids(note_ids),),
            ).fetchall()
        return {
            row["note_id"]: {
                "last_retrieved_at": ts_out(row["last_retrieved_at"]),
                "hit_count": row["hit_count"],
            }
            for row in rows
        }

    def find_orphaned_note_ids(self) -> List[str]:
        """Return IDs of notes that have no links in either direction."""
        with self._connection() as conn:
            return _first_column(
                conn.execute(
                    "SELECT n.id FROM notes n "
                    "WHERE NOT EXISTS (SELECT 1 FROM links WHERE source_id = n.id) "
                    "AND NOT EXISTS (SELECT 1 FROM links WHERE target_id = n.id) "
                    "ORDER BY n.pk"
                )
            )

    def get_connection_counts(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Return (note_id, connection_count) for the most-connected notes.

        A note's count is its distinct outgoing targets plus distinct incoming
        sources (a note linked both ways with another counts it twice, once per
        direction), as the Kuzu index counted it.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, cnt FROM ("
                "  SELECT n.id AS id,"
                "    (SELECT count(DISTINCT target_id) FROM links WHERE source_id = n.id)"
                "    + (SELECT count(DISTINCT source_id) FROM links WHERE target_id = n.id)"
                "    AS cnt"
                "  FROM notes n"
                ") WHERE cnt > 0 ORDER BY cnt DESC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row["id"], row["cnt"]) for row in rows]
