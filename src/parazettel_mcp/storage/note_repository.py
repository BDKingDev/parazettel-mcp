"""Repository for note storage and retrieval.

Dual-storage architecture
-------------------------
* Markdown files on disk – **source of truth** for note content and frontmatter.
* Kuzu embedded graph database – **index** for fast querying and graph traversal.

The file system is always authoritative; the graph database can be rebuilt from
the markdown files at any time via :meth:`NoteRepository.rebuild_index`.
"""

import datetime
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

import frontmatter
import kuzu

from parazettel_mcp.config import config
from parazettel_mcp.models.graph_db import (
    NOTE_TITLE_VECTOR_INDEX,
    NOTE_VECTOR_INDEX,
    GraphDatabaseReadOnlyError,
    close_graph_db,
    create_note_vector_index,
    ensure_embedding_schema,
    force_close_graph_db,
    graph_db_companions,
    init_graph_db,
    note_vector_index_exists,
    swap_graph_db_file,
)
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

_GRAPH_LOCK_ERROR_MARKERS = (
    "could not set lock on file",
    "lock on file",
    "resource temporarily unavailable",
)

# ---------------------------------------------------------------------------
# Module-level LRU cache  {(path_str, mtime_ns): Note}
# ---------------------------------------------------------------------------
_NOTE_CACHE: OrderedDict = OrderedDict()
_NOTE_CACHE_LOCK = threading.Lock()
_NOTE_CACHE_MAX = 256
_ATOMIC_WRITE_ATTEMPTS = 5
_ATOMIC_WRITE_BACKOFF_SECONDS = 0.05
_RETRYABLE_ATOMIC_WRITE_WINERRORS = {5, 32}
# Snapshotting the just-closed graph DB file can briefly race the OS releasing
# Kuzu's memory-map: WinError 32 (file in use) and 33 (locked region) both clear
# within a few milliseconds, so the backup copy retries on them.
_GRAPH_BACKUP_COPY_ATTEMPTS = 8
_GRAPH_BACKUP_BACKOFF_SECONDS = 0.05
_RETRYABLE_GRAPH_COPY_WINERRORS = {5, 32, 33}
_GRAPH_BATCH_SIZE = 100
# Notes per progress-logged chunk during the bulk embed (within each chunk the
# provider further batches at embedding_batch_size). Purely for log granularity.
_EMBED_LOG_CHUNK = 200
# Cap how many skipped filenames are inlined into the rebuild warning log so a
# mass parse failure can't emit one enormous log line.
_REBUILD_SKIPPED_LOG_LIMIT = 10

# Brute-force distance expressions (lower = closer) matching each HNSW metric, so
# the fallback's ordering is consistent with the index instead of always cosine.
_BRUTE_FORCE_DISTANCE = {
    "cosine": "1.0 - array_cosine_similarity(p.embedding, $q)",
    "l2": "array_distance(p.embedding, $q)",
    "dotproduct": "-array_dot_product(p.embedding, $q)",
}
# Same distances over the dirty-set's *title* vector (dual-vector recall).
_BRUTE_FORCE_DISTANCE_TITLE = {
    "cosine": "1.0 - array_cosine_similarity(p.title_embedding, $q)",
    "l2": "array_distance(p.title_embedding, $q)",
    "dotproduct": "-array_dot_product(p.title_embedding, $q)",
}

_NOTE_SELECT = (
    "n.id AS id, n.title AS title, n.content AS content, n.note_type AS note_type, "
    "n.status AS status, n.source AS source, n.due_date AS due_date, "
    "n.priority AS priority, n.recurrence_rule AS recurrence_rule, "
    "n.estimated_minutes AS estimated_minutes, n.remind_at AS remind_at, "
    "n.project_id AS project_id, n.area_id AS area_id, "
    "n.origin AS origin, n.last_verified AS last_verified, "
    "n.metadata_json AS metadata_json, n.created_at AS created_at, n.updated_at AS updated_at"
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

# Marker description on derived area->member has_part edges. Direct area
# membership is bidirectional in the GRAPH (member part_of area, area has_part
# member), but an area can have hundreds of direct members, so the area-side
# counter edge is derived at index time from the member's area_id frontmatter
# instead of being materialized into the area's ## Links. Derived edges are
# recognized (and excluded from markdown serialization) by this description.
_AREA_MEMBERSHIP_DESC = "derived: area membership"


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


def _is_retryable_graph_copy_error(exc: OSError) -> bool:
    """Return True when copying the graph DB file hit a transient lock."""
    return getattr(exc, "winerror", None) in _RETRYABLE_GRAPH_COPY_WINERRORS


def _copy_file_with_retry(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, retrying transient Windows file locks with backoff."""
    last_error: Optional[OSError] = None
    for attempt in range(_GRAPH_BACKUP_COPY_ATTEMPTS):
        try:
            shutil.copy2(src, dst)
            return
        except OSError as exc:
            last_error = exc
            if (
                attempt == _GRAPH_BACKUP_COPY_ATTEMPTS - 1
                or not _is_retryable_graph_copy_error(exc)
            ):
                break
            time.sleep(_GRAPH_BACKUP_BACKOFF_SECONDS * (2**attempt))
    assert last_error is not None
    raise last_error


def _result_to_records(result: Any) -> List[Dict[str, Any]]:
    """Convert a Kuzu QueryResult to a list of dicts."""
    cols = result.get_column_names()
    records: List[Dict[str, Any]] = []
    while result.has_next():
        records.append(dict(zip(cols, result.get_next())))
    return records


def _result_first_column(result: Any) -> List[Any]:
    """Return the first column of every row in a Kuzu QueryResult as a list."""
    values: List[Any] = []
    while result.has_next():
        values.append(result.get_next()[0])
    return values


def _db_dict_to_note(
    nd: Dict[str, Any], tags: List[Tag], links: List[Link]
) -> Note:
    """Reconstruct a Note from a graph DB row dict and pre-fetched relations."""
    metadata = json.loads(nd["metadata_json"]) if nd.get("metadata_json") else {}
    return Note(
        id=nd["id"],
        title=nd["title"],
        content=nd["content"],
        note_type=NoteType(nd["note_type"]),
        tags=tags,
        links=links,
        created_at=nd["created_at"],
        updated_at=nd["updated_at"],
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
    2. Kuzu embedded graph database – fast index for queries and graph traversal.
    """

    def __init__(self, notes_dir: Optional[Path] = None):
        """Initialise the repository."""
        self.notes_dir = (
            config.get_absolute_path(notes_dir)
            if notes_dir
            else config.get_absolute_path(config.notes_dir)
        )
        self.notes_dir.mkdir(parents=True, exist_ok=True)

        self.graph_db_path = config.get_graph_db_path()
        self.file_lock = threading.RLock()
        self.read_only = False
        self.db: Optional[kuzu.Database] = None
        self._closed = True
        # Names of markdown files that failed to parse on the most recent rebuild.
        # Surfaced to callers so a shrinking corpus is visible instead of silent.
        self.last_rebuild_skipped: List[str] = []
        # Optional semantic-embedding backend; None when embeddings are disabled
        # (the default), in which case all embedding code paths are skipped and
        # behaviour is unchanged. Built from config once; the model loads lazily.
        self._embedding_provider = build_embedding_provider(config)
        self._open_graph_db(allow_rebuild_if_needed=True)

    def close(self) -> None:
        """Release resources held by this repository."""
        if self._closed:
            return
        close_graph_db(self.graph_db_path, read_only=self.read_only)
        self.db = None
        self._closed = True

    def _open_graph_db(self, *, allow_rebuild_if_needed: bool) -> None:
        """Open the configured graph DB, optionally running startup rebuild checks."""
        self.read_only = False
        bufpool = config.kuzu_buffer_pool_bytes
        try:
            self.db = init_graph_db(self.graph_db_path, buffer_pool_size=bufpool)
        except Exception as exc:
            if not self._is_graph_lock_error(exc):
                raise
            logger.warning(
                "Graph DB at %s is already open elsewhere; falling back to read-only mode.",
                self.graph_db_path,
            )
            self.db = init_graph_db(
                self.graph_db_path, read_only=True, buffer_pool_size=bufpool
            )
            self.read_only = True
        self._closed = False

        if self._embedding_provider is not None and not self.read_only:
            self._ensure_embedding_schema()

        if allow_rebuild_if_needed and not self.read_only:
            self.rebuild_index_if_needed()

    def _reopen_graph_db(self, *, allow_rebuild_if_needed: bool = False) -> None:
        """Close and reopen the graph DB handle in-place."""
        self.close()
        self._open_graph_db(allow_rebuild_if_needed=allow_rebuild_if_needed)

    @staticmethod
    def _is_graph_lock_error(exc: Exception) -> bool:
        """Return True when an exception looks like a cross-process graph lock."""
        message = str(exc).lower()
        return any(marker in message for marker in _GRAPH_LOCK_ERROR_MARKERS)

    def _assert_writable(self) -> None:
        """Fail fast when a mutating operation is attempted in read-only mode."""
        if self.read_only:
            raise GraphDatabaseReadOnlyError(
                "Parazettel is running in read-only graph mode because the database "
                "is already open in another MCP session. Open only one write-enabled "
                "chat at a time to create, update, delete, or rebuild notes."
            )

    def _get_conn(self) -> kuzu.Connection:
        """Create a low-level Kuzu connection.

        Kuzu connections are not thread-safe, so one connection is created per
        repository operation. Callers are responsible for closing the returned
        connection. Internal repository methods should prefer ``_connection()``.
        """
        if self._closed or self.db is None:
            raise RuntimeError("NoteRepository is closed")
        return kuzu.Connection(self.db)

    @contextmanager
    def _connection(self) -> Iterator[kuzu.Connection]:
        """Yield a Kuzu connection and always close it afterwards."""
        conn = self._get_conn()
        try:
            yield conn
        finally:
            conn.close()

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
        """Rebuild the graph index from files when the ID sets diverge."""
        with self._connection() as conn:
            id_result = conn.execute("MATCH (n:Note) RETURN n.id")
            db_ids = set(_result_first_column(id_result))
        file_stems = {p.stem for p in self.notes_dir.glob("*.md")}
        if db_ids != file_stems:
            self.rebuild_index()

    def check_consistency(self) -> Dict[str, Any]:
        """Compare the markdown files (source of truth) against the graph index.

        Read-only: this never modifies either store. It surfaces the three ways
        the file system and the Kuzu index can silently disagree:

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
            db_ids = set(
                _result_first_column(conn.execute("MATCH (n:Note) RETURN n.id"))
            )
            common = file_stems & db_ids
            stored_content: Dict[str, str] = {}
            if common:
                result = conn.execute(
                    "MATCH (n:Note) WHERE n.id IN $ids "
                    "RETURN n.id AS id, n.content AS content",
                    {"ids": list(common)},
                )
                while result.has_next():
                    row = result.get_next()
                    stored_content[row[0]] = row[1]

        missing_from_index = sorted(file_stems - db_ids)
        missing_from_files = sorted(db_ids - file_stems)

        content_drift: List[str] = []
        unreadable_files: List[str] = []
        dangling_refs: List[str] = []
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
            # Compare the on-disk note body to exactly what the graph stored.
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
                    if entry not in dangling_refs:
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

    def _build_graph_backup_path(self) -> Path:
        """Return a timestamped backup path for the graph DB snapshot."""
        timestamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = self.graph_db_path.with_name(
            f"{self.graph_db_path.name}.{timestamp}.bak"
        )
        counter = 1
        while backup_path.exists():
            backup_path = self.graph_db_path.with_name(
                f"{self.graph_db_path.name}.{timestamp}.{counter}.bak"
            )
            counter += 1
        return backup_path

    def _create_graph_backup(self) -> Optional[Path]:
        """Create a file snapshot backup of the current graph before rebuild."""
        graph_db_path = self.graph_db_path
        if not graph_db_path.exists():
            return None

        backup_path = self._build_graph_backup_path()

        # Kuzu keeps the DB file memory-mapped while open, so copying it while a
        # handle is live raises WinError 33 on Windows. The handle is refcounted,
        # so a plain close() can be a silent no-op when another reference is
        # outstanding; force_close_graph_db guarantees the file is released for
        # the brief stop-the-world snapshot, then we reopen.
        with self.file_lock:
            force_close_graph_db(graph_db_path)
            self.db = None
            self._closed = True
            try:
                if graph_db_path.is_dir():
                    shutil.copytree(graph_db_path, backup_path)
                else:
                    _copy_file_with_retry(graph_db_path, backup_path)
                    for companion_path in graph_db_companions(graph_db_path):
                        suffix = companion_path.name[len(graph_db_path.name):]
                        _copy_file_with_retry(
                            companion_path,
                            backup_path.with_name(f"{backup_path.name}{suffix}"),
                        )
            finally:
                self._open_graph_db(allow_rebuild_if_needed=False)

        logger.info("Created graph database backup before reindex: %s", backup_path)
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
        """Rebuild the graph index from all markdown files.

        The rebuild is the only path that fully reconciles the graph with the
        files, so it must reflect *every* on-disk edit — including tags or links
        removed by hand. Rather than clearing the live database in place (a bulk
        ``DETACH DELETE`` on a large graph segfaults Kuzu 0.11.3 on Windows, and
        an in-place clear can also strand Tag nodes that lost their last note),
        the new graph is built into a fresh temporary database and then
        atomically swapped in. Building from empty means only tags/links present
        in the files survive, so removals are picked up and orphan Tag nodes
        disappear for free.

        Within the fresh build a two-pass strategy is used:
        * Pass 1 – create all Note nodes and Tag nodes.
        * Pass 2 – create all LINKS_TO / HAS_TAG relationships, so edges are
          created even when a link's target note appears later in file order.
        """
        self._assert_writable()
        # Reset up front so a failure before parsing completes (e.g. during backup
        # creation), or a concurrent reader, never sees a previous rebuild's list.
        self.last_rebuild_skipped = []
        backup_path = self._create_graph_backup()

        # Snapshot retrieval signals from the live graph: they exist only in the
        # index (never in markdown), so a fresh build would otherwise wipe them.
        try:
            retrieval_signals = self._fetch_retrieval_signals()
        except Exception as exc:  # best-effort: signals must never block rebuild
            logger.warning("Could not snapshot retrieval signals: %s", exc)
            retrieval_signals = []

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

        # Build into an isolated subdirectory so the temp DB and its sidecars can
        # never collide with (or be mistaken for a companion of) the live DB file
        # in the same directory.
        tmp_dir = self.graph_db_path.with_name(
            f".rebuild.{os.getpid()}.{uuid.uuid4().hex}"
        )
        tmp_db_path = tmp_dir / self.graph_db_path.name
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            self._build_graph_into(
                tmp_db_path, notes, retrieval_signals=retrieval_signals
            )

            # Close every handle to the live DB so the file can be replaced,
            # then atomically swap the freshly built DB into place and reopen.
            with self.file_lock:
                force_close_graph_db(self.graph_db_path)
                self.db = None
                self._closed = True
                swap_graph_db_file(tmp_db_path, self.graph_db_path)
                self._open_graph_db(allow_rebuild_if_needed=False)
        finally:
            self._cleanup_rebuild_artifacts(tmp_dir)

        return backup_path

    def _build_graph_into(
        self,
        db_path: Path,
        notes: List[Note],
        retrieval_signals: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Build a complete graph for *notes* in a fresh database at *db_path*.

        The database starts empty, so every node is new and no deletes are
        issued. ``init_graph_db`` creates the schema (and FTS indexes), and the
        handle is fully released afterwards so the file can be swapped in.
        ``retrieval_signals`` (snapshotted from the previous graph) are restored
        onto the new Note nodes so graph-only operational data survives.
        """
        db = init_graph_db(
            db_path, buffer_pool_size=config.kuzu_buffer_pool_bytes
        )
        try:
            conn = kuzu.Connection(db)
            try:
                if self._embedding_provider is not None:
                    ensure_embedding_schema(conn, config.embedding_dim)
                self._ensure_tag_nodes(
                    conn, (tag.name for note in notes for tag in note.tags)
                )
                # Pass 1: all Note nodes (empty DB, so every note is new).
                for note in notes:
                    self._index_note_nodes_only(
                        note, conn, assume_missing=True, ensure_tags=False
                    )
                # Pass 2: all relationships in chunked batches.
                for i in range(0, len(notes), _GRAPH_BATCH_SIZE):
                    self._index_note_relations_batch(
                        notes[i : i + _GRAPH_BATCH_SIZE],
                        conn,
                        clear_existing=False,
                    )
                # Derived area->member has_part counter edges (graph-only;
                # derived from member area_id frontmatter, see
                # _sync_derived_area_membership).
                self._derive_all_area_memberships(conn)
                # Restore retrieval signals before any vector index is built.
                # MATCH drops signals for notes that no longer exist on disk.
                if retrieval_signals:
                    conn.execute(
                        "UNWIND $rows AS row MATCH (n:Note {id: row.id}) "
                        "SET n.last_retrieved_at = row.last_retrieved_at, "
                        "n.hit_count = row.hit_count",
                        {"rows": retrieval_signals},
                    )
                # Pass 3: embed all notes and build the HNSW index (no-op when
                # embeddings are disabled).
                self._build_embeddings(conn, notes)
            finally:
                conn.close()
        finally:
            force_close_graph_db(db_path)

    def _cleanup_rebuild_artifacts(self, tmp_dir: Path) -> None:
        """Remove the temporary rebuild directory, if any of it remains."""
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                logger.warning("Could not remove rebuild artifact %s: %s", tmp_dir, exc)

    def _index_note_nodes_only(
        self,
        note: Note,
        conn: kuzu.Connection,
        *,
        assume_missing: bool = False,
        ensure_tags: bool = True,
    ) -> None:
        """Create or update only the Note node and Tag nodes (no edges)."""
        params = self._note_params(note, note.content)

        if not assume_missing:
            exists_result = conn.execute(
                "MATCH (n:Note {id: $id}) RETURN n.id", {"id": note.id}
            )
        else:
            exists_result = None

        if exists_result is not None and exists_result.get_num_tuples() > 0:
            update_params = {k: v for k, v in params.items() if k != "created_at"}
            conn.execute(
                """
                MATCH (n:Note {id: $id})
                SET n.title = $title,
                    n.content = $content,
                    n.note_type = $note_type,
                    n.status = $status,
                    n.source = $source,
                    n.due_date = $due_date,
                    n.priority = $priority,
                    n.recurrence_rule = $recurrence_rule,
                    n.estimated_minutes = $estimated_minutes,
                    n.remind_at = $remind_at,
                    n.project_id = $project_id,
                    n.area_id = $area_id,
                    n.origin = $origin,
                    n.last_verified = $last_verified,
                    n.metadata_json = $metadata_json,
                    n.updated_at = $updated_at
                """,
                update_params,
            )
        else:
            conn.execute(
                """
                CREATE (:Note {
                    id: $id,
                    title: $title,
                    content: $content,
                    note_type: $note_type,
                    status: $status,
                    source: $source,
                    due_date: $due_date,
                    priority: $priority,
                    recurrence_rule: $recurrence_rule,
                    estimated_minutes: $estimated_minutes,
                    remind_at: $remind_at,
                    project_id: $project_id,
                    area_id: $area_id,
                    origin: $origin,
                    last_verified: $last_verified,
                    metadata_json: $metadata_json,
                    created_at: $created_at,
                    updated_at: $updated_at
                })
                """,
                params,
            )

        if ensure_tags:
            self._ensure_tag_nodes(conn, (tag.name for tag in note.tags))

    def _ensure_tag_nodes(
        self, conn: kuzu.Connection, tag_names: Iterable[str]
    ) -> None:
        """Create tag nodes in one batched query, de-duping names first."""
        unique_tag_names = list(dict.fromkeys(tag_names))
        if not unique_tag_names:
            return
        conn.execute(
            "UNWIND $tag_names AS tag_name MERGE (:Tag {name: tag_name})",
            {"tag_names": unique_tag_names},
        )

    def _index_note_relations(
        self,
        note: Note,
        conn: kuzu.Connection,
        *,
        clear_existing: bool = True,
    ) -> None:
        """Create HAS_TAG and LINKS_TO relationships for a note."""
        if clear_existing:
            conn.execute(
                "MATCH (n:Note {id: $id})-[r:HAS_TAG]->() DELETE r", {"id": note.id}
            )
            conn.execute(
                "MATCH (n:Note {id: $id})-[r:LINKS_TO]->() DELETE r", {"id": note.id}
            )

        if note.tags:
            conn.execute(
                """
                UNWIND $tag_names AS tag_name
                MATCH (n:Note {id: $note_id}), (t:Tag {name: tag_name})
                CREATE (n)-[:HAS_TAG]->(t)
                """,
                {
                    "note_id": note.id,
                    "tag_names": [tag.name for tag in note.tags],
                },
            )

        link_rows = self._link_rows_for_note(note)
        if link_rows:
            conn.execute(
                """
                UNWIND $links AS link
                MATCH (s:Note {id: $source_id}), (t:Note {id: link.target_id})
                CREATE (s)-[:LINKS_TO {
                    link_type: link.link_type,
                    description: link.description,
                    created_at: link.created_at
                }]->(t)
                """,
                {
                    "source_id": note.id,
                    "links": [
                        {k: v for k, v in row.items() if k != "source_id"}
                        for row in link_rows
                    ],
                },
            )

    def _sync_derived_area_membership(
        self, note: Note, conn: kuzu.Connection
    ) -> None:
        """Maintain the derived area->member ``has_part`` counter edge.

        Direct area membership is bidirectional: the member carries an explicit
        ``part_of`` link in its markdown, and the area gets a ``has_part`` edge
        in the graph — derived here from the member's ``area_id`` frontmatter
        rather than materialized into the area's ## Links (areas can have
        hundreds of direct members; a 500-line links section would make the
        area file and its embedding unusable). Because the edge derives from
        the member file (the source of truth), rebuilds reproduce it.

        Projects are excluded: an area's ``has_part`` to its projects is
        already materialized in the area's markdown (bounded membership), and a
        project-routed note's container is the project, not the area.
        """
        if note.note_type == NoteType.AREA:
            # Re-indexing an area cleared its outgoing edges (including derived
            # member edges); re-derive them for all current direct members.
            member_ids = _result_first_column(
                conn.execute(
                    "MATCH (m:Note) "
                    "WHERE m.area_id = $id AND m.id <> $id "
                    "AND m.project_id IS NULL "
                    "AND m.note_type <> 'project' AND m.note_type <> 'area' "
                    "RETURN m.id",
                    {"id": note.id},
                )
            )
            self._create_derived_membership_edges(
                conn, [(note.id, member_id) for member_id in member_ids]
            )
            return
        if note.note_type == NoteType.PROJECT:
            return
        # Member note: drop its stale derived edge (if any) and re-derive for
        # the current routing. Only derived edges are touched — a has_part an
        # area carries in its own markdown is left alone.
        conn.execute(
            "MATCH (s:Note)-[r:LINKS_TO {link_type: 'has_part'}]->"
            "(n:Note {id: $id}) "
            "WHERE s.note_type = 'area' AND r.description = $marker "
            "WITH r DELETE r",
            {"id": note.id, "marker": _AREA_MEMBERSHIP_DESC},
        )
        if note.area_id and not note.project_id and note.area_id != note.id:
            area_exists = _result_first_column(
                conn.execute(
                    "MATCH (a:Note {id: $area_id}) "
                    "WHERE a.note_type = 'area' RETURN a.id",
                    {"area_id": note.area_id},
                )
            )
            if area_exists:
                self._create_derived_membership_edges(
                    conn, [(note.area_id, note.id)]
                )

    @staticmethod
    def _create_derived_membership_edges(
        conn: kuzu.Connection, pairs: List[Tuple[str, str]]
    ) -> None:
        """Batch-create derived area->member has_part edges for (area, member) pairs."""
        if not pairs:
            return
        now = datetime.datetime.now()
        conn.execute(
            """
            UNWIND $rows AS row
            MATCH (a:Note {id: row.area_id}), (m:Note {id: row.member_id})
            CREATE (a)-[:LINKS_TO {
                link_type: row.link_type,
                description: row.description,
                created_at: row.created_at
            }]->(m)
            """,
            {
                "rows": [
                    {
                        "area_id": area_id,
                        "member_id": member_id,
                        "link_type": LinkType.HAS_PART.value,
                        "description": _AREA_MEMBERSHIP_DESC,
                        "created_at": now,
                    }
                    for area_id, member_id in pairs
                ]
            },
        )

    def _derive_all_area_memberships(self, conn: kuzu.Connection) -> None:
        """Create every derived area->member has_part edge in one pass.

        Used by the fresh rebuild, where the database starts empty and every
        edge is created exactly once (see _sync_derived_area_membership for the
        membership rules).
        """
        pair_result = conn.execute(
            "MATCH (a:Note), (m:Note) "
            "WHERE a.note_type = 'area' AND m.area_id = a.id AND m.id <> a.id "
            "AND m.project_id IS NULL "
            "AND m.note_type <> 'project' AND m.note_type <> 'area' "
            "RETURN a.id AS area_id, m.id AS member_id"
        )
        pairs: List[Tuple[str, str]] = []
        while pair_result.has_next():
            row = pair_result.get_next()
            pairs.append((row[0], row[1]))
        self._create_derived_membership_edges(conn, pairs)

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
            and not (
                link.link_type == LinkType.HAS_PART
                and link.description == _AREA_MEMBERSHIP_DESC
            )
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

    def _index_note_relations_batch(
        self,
        notes: List[Note],
        conn: kuzu.Connection,
        *,
        clear_existing: bool = True,
    ) -> None:
        """Create HAS_TAG and LINKS_TO relationships for many notes at once."""
        if not notes:
            return

        if clear_existing:
            note_ids = [note.id for note in notes]
            conn.execute(
                """
                UNWIND $note_ids AS note_id
                MATCH (n:Note {id: note_id})-[r:HAS_TAG]->()
                DELETE r
                """,
                {"note_ids": note_ids},
            )
            conn.execute(
                """
                UNWIND $note_ids AS note_id
                MATCH (n:Note {id: note_id})-[r:LINKS_TO]->()
                DELETE r
                """,
                {"note_ids": note_ids},
            )

        tag_rows = [
            {"note_id": note.id, "tag_name": tag.name}
            for note in notes
            for tag in note.tags
        ]
        if tag_rows:
            conn.execute(
                """
                UNWIND $rows AS row
                MATCH (n:Note {id: row.note_id}), (t:Tag {name: row.tag_name})
                CREATE (n)-[:HAS_TAG]->(t)
                """,
                {"rows": tag_rows},
            )

        link_rows = [
            row for note in notes for row in self._link_rows_for_note(note)
        ]
        if link_rows:
            conn.execute(
                """
                UNWIND $rows AS row
                MATCH (s:Note {id: row.source_id}), (t:Note {id: row.target_id})
                CREATE (s)-[:LINKS_TO {
                    link_type: row.link_type,
                    description: row.description,
                    created_at: row.created_at
                }]->(t)
                """,
                {"rows": link_rows},
            )

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
    def _parse_inline_refs(
        note_id: str, content: str, links: List[Link]
    ) -> List[str]:
        """Extract note IDs wiki-linked from prose (outside ``## Links``).

        Only id-shaped targets count — a ``[[Some Title]]`` style link is not a
        managed reference. Targets already covered by an explicit ## Links entry
        are skipped so the graph doesn't grow a duplicate edge. Fenced code
        blocks are ignored.
        """
        explicit_targets = {link.target_id for link in links}
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

        # Derived links must never be rendered into ## Links: INLINE links
        # already live in the prose body, and derived area-membership has_part
        # edges exist only in the graph (an area's markdown would otherwise
        # gain one line per member note).
        serializable_links = [
            link
            for link in note.links
            if link.link_type != LinkType.INLINE
            and not (
                link.link_type == LinkType.HAS_PART
                and link.description == _AREA_MEMBERSHIP_DESC
            )
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
        """Resolve link target titles in one graph query for markdown serialisation."""
        target_ids = {link.target_id for link in links}
        if not target_ids:
            return {}

        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note) WHERE n.id IN $ids RETURN n.id AS id, n.title AS title",
                {"ids": list(target_ids)},
            )
            title_map: Dict[str, str] = {}
            while result.has_next():
                row = result.get_next()
                title_map[row[0]] = row[1]
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
        """Build the Kuzu parameter dict for a Note upsert."""
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
            "created_at": note.created_at,
            "updated_at": note.updated_at,
        }

    # --- Semantic embeddings (only active when a provider is configured) ------

    def _embedding_text(self, note: Note) -> str:
        """Return the text embedded for a note: its title plus body."""
        return f"{note.title or ''}\n\n{note.content or ''}".strip()

    @staticmethod
    def _title_text(note: Note) -> str:
        """Return the note's title — embedded as a second 'atomic claim' vector."""
        return (note.title or "").strip()

    def _ensure_embedding_schema(self) -> None:
        """Idempotently add the embedding columns to the live graph DB."""
        try:
            with self._connection() as conn:
                ensure_embedding_schema(conn, config.embedding_dim)
        except Exception as exc:  # never block opening the DB on this
            logger.warning("Could not ensure embedding schema: %s", exc)

    def _store_embeddings(
        self,
        conn: kuzu.Connection,
        notes: List[Note],
        vectors: List[List[float]],
        title_vectors: List[List[float]],
    ) -> None:
        """Write precomputed doc + title vectors onto their Note nodes.

        Runs during the fresh rebuild *before* the HNSW indexes are created, so the
        columns are still writable; these vectors are then folded into the indexes.
        """
        if len(vectors) != len(notes) or len(title_vectors) != len(notes):
            # A provider returning the wrong count would silently leave some
            # notes unembedded (or drop vectors); skip rather than build the
            # index over a partially-populated column.
            logger.warning(
                "Embedding provider returned %d doc / %d title vectors for %d "
                "notes; skipping embedding storage for this rebuild.",
                len(vectors),
                len(title_vectors),
                len(notes),
            )
            return
        model_id = self._embedding_provider.model_id  # type: ignore[union-attr]
        now = datetime.datetime.now()
        for note, vector, title_vector in zip(notes, vectors, title_vectors):
            conn.execute(
                "MATCH (n:Note {id: $id}) "
                "SET n.embedding = $embedding, n.title_embedding = $title_embedding, "
                "n.embedded_at = $embedded_at, n.embedding_model = $model",
                {
                    "id": note.id,
                    "embedding": vector,
                    "title_embedding": title_vector,
                    "embedded_at": now,
                    "model": model_id,
                },
            )

    def _build_embeddings(self, conn: kuzu.Connection, notes: List[Note]) -> None:
        """Embed every note and build the HNSW index, inside a fresh rebuild DB.

        Best-effort: if embedding fails (e.g. the model dependency is missing),
        the rebuild still completes without vectors rather than failing — search
        falls back to BM25. Runs during the exclusive rebuild, so the embedding
        cost is paid once per rebuild rather than on the hot write path.
        """
        provider = self._embedding_provider
        if provider is None or not notes:
            return
        total = len(notes)
        try:
            logger.info(
                "Embedding %d notes (provider=%s, batch=%d)...",
                total,
                provider.model_id,
                # Log the configured batch size (clamped like the providers do)
                # rather than reaching into a provider-private attribute.
                max(1, int(getattr(config, "embedding_batch_size", 16))),
            )
            # Chunk the bulk embed so progress is visible during the (slow) embed
            # phase — a single embed_documents call over the whole vault is opaque
            # and can run for many minutes on CPU. Each chunk embeds its doc texts
            # then its title texts in one provider round-trip, then splits the
            # result (halves remote calls/latency vs. embedding them separately).
            for start in range(0, total, _EMBED_LOG_CHUNK):
                chunk = notes[start : start + _EMBED_LOG_CHUNK]
                doc_texts = [self._embedding_text(note) for note in chunk]
                title_texts = [self._title_text(note) for note in chunk]
                all_vectors = provider.embed_documents(doc_texts + title_texts)
                if len(all_vectors) != 2 * len(chunk):
                    # A wrong count would split into mismatched halves, leave the
                    # embedding columns unpopulated (_store_embeddings bails), and
                    # then the indexes below would still be built over NULLs. Abort
                    # the whole build instead (caught below → no index created,
                    # search falls back to BM25 — never an index over incomplete
                    # embeddings).
                    raise RuntimeError(
                        f"Embedding provider returned {len(all_vectors)} vectors "
                        f"for {2 * len(chunk)} expected (doc+title); aborting "
                        "embedding build."
                    )
                vectors = all_vectors[: len(chunk)]
                title_vectors = all_vectors[len(chunk):]
                self._store_embeddings(conn, chunk, vectors, title_vectors)
                logger.info(
                    "  embedded %d/%d notes", min(start + len(chunk), total), total
                )
            create_note_vector_index(conn, config.embedding_metric)
            create_note_vector_index(
                conn,
                config.embedding_metric,
                index_name=NOTE_TITLE_VECTOR_INDEX,
                column="title_embedding",
            )
            logger.info(
                "Built HNSW vector index over %d notes (metric=%s)",
                total,
                config.embedding_metric,
            )
        except Exception as exc:
            logger.warning(
                "Embedding build failed; rebuilt index without vectors "
                "(search will use BM25): %s",
                exc,
            )

    def _set_note_embedding(self, conn: kuzu.Connection, note: Note) -> None:
        """Embed a single note on create/update into the dirty pending table.

        The vector goes to the un-indexed ``PendingEmbedding`` table — once the
        HNSW index exists, ``Note.embedding`` is locked against ``SET``, so per-note
        writes must land here. The brute-force fallback reads it until the next
        rebuild folds the note into the index. Best-effort: a failure (e.g. a
        missing model) is logged and never blocks note creation. (The embedding is
        computed here under the global write lock; moving the compute outside the
        lock is a planned optimization.)
        """
        provider = self._embedding_provider
        if provider is None:
            return
        try:
            vector, title_vector = provider.embed_documents(
                [self._embedding_text(note), self._title_text(note)]
            )
            conn.execute(
                "MERGE (p:PendingEmbedding {id: $id}) "
                "SET p.embedding = $embedding, p.title_embedding = $title_embedding, "
                "p.embedded_at = $embedded_at, p.embedding_model = $model",
                {
                    "id": note.id,
                    "embedding": vector,
                    "title_embedding": title_vector,
                    "embedded_at": datetime.datetime.now(),
                    "model": provider.model_id,
                },
            )
        except Exception as exc:
            logger.warning("Could not embed note %s: %s", note.id, exc)

    def _search_by_vector(
        self, query_vector: List[float], limit: int
    ) -> Dict[str, float]:
        """Return {note_id: distance} (lower = closer) for a precomputed vector.

        Combines the HNSW index (only when it was built with the *current* model;
        a stale-model index is skipped) with a brute-force pass over the dirty
        ``PendingEmbedding`` set — its join to ``Note`` drops deleted notes, and
        its distance matches the configured metric. Dirty vectors override stale
        index entries for the same id.
        """
        if self._embedding_provider is None:
            return {}
        model_id = self._embedding_provider.model_id
        metric = (config.embedding_metric or "cosine").strip().lower()
        doc_expr = _BRUTE_FORCE_DISTANCE.get(metric, _BRUTE_FORCE_DISTANCE["cosine"])
        title_expr = _BRUTE_FORCE_DISTANCE_TITLE.get(
            metric, _BRUTE_FORCE_DISTANCE_TITLE["cosine"]
        )

        def merge_min(target: Dict[str, float], note_id: str, dist: float) -> None:
            if note_id not in target or dist < target[note_id]:
                target[note_id] = dist

        # Over-fetch per source: the doc- and title-index top-k can overlap, and
        # HNSW recall is approximate, so taking only `limit` from each can yield
        # fewer than `limit` distinct notes (or miss a true match). Fetch a wider
        # band from each source; the caller re-sorts the merged map by distance
        # and truncates to `limit`.
        fetch_k = max(1, limit) * 2

        index_dist: Dict[str, float] = {}
        pending_dist: Dict[str, float] = {}
        try:
            with self._connection() as conn:
                # Query the doc index and the title index; keep the closer of the
                # two per note (a note surfaces if its body OR its title matches).
                if self._indexed_embedding_model(conn) == model_id:
                    for index_name in (NOTE_VECTOR_INDEX, NOTE_TITLE_VECTOR_INDEX):
                        if not note_vector_index_exists(conn, index_name):
                            continue
                        try:
                            hnsw = conn.execute(
                                "CALL QUERY_VECTOR_INDEX("
                                f"'Note', '{index_name}', $q, $k"
                                ") RETURN node.id AS id, distance ORDER BY distance",
                                {"q": query_vector, "k": fetch_k},
                            )
                            while hnsw.has_next():
                                row = hnsw.get_next()
                                merge_min(index_dist, row[0], float(row[1]))
                        except Exception as exc:
                            logger.warning(
                                "HNSW vector query (%s) failed: %s", index_name, exc
                            )
                # Brute-force the dirty set over both doc and title vectors.
                for column, expr in (("embedding", doc_expr),
                                     ("title_embedding", title_expr)):
                    try:
                        brute = conn.execute(
                            "MATCH (p:PendingEmbedding) MATCH (n:Note {id: p.id}) "
                            f"WHERE p.{column} IS NOT NULL "
                            "AND p.embedding_model = $model "
                            f"RETURN p.id AS id, {expr} AS dist "
                            "ORDER BY dist LIMIT $k",
                            {"q": query_vector, "model": model_id, "k": fetch_k},
                        )
                        while brute.has_next():
                            row = brute.get_next()
                            merge_min(pending_dist, row[0], float(row[1]))
                    except Exception as exc:
                        logger.warning(
                            "Brute-force vector fallback (%s) failed: %s", column, exc
                        )
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)
            return {}
        # Dirty (pending) vectors override stale index entries for the same id.
        distance_by_id = dict(index_dist)
        distance_by_id.update(pending_dist)
        return distance_by_id

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

    def get_note_embedding(self, note_id: str) -> Optional[List[float]]:
        """Return a note's stored *current-model* document embedding, freshest first.

        Prefers the dirty ``PendingEmbedding`` over the indexed ``Note.embedding``,
        and only returns a vector produced by the active model — a stale vector
        from a previous model would be incompatible with ``_search_by_vector``
        (which searches current-model vectors). Returns ``None`` when embeddings
        are disabled or no current-model vector is stored yet.
        """
        if self._embedding_provider is None:
            return None
        model_id = self._embedding_provider.model_id
        try:
            with self._connection() as conn:
                for query in (
                    "MATCH (p:PendingEmbedding {id: $id}) "
                    "WHERE p.embedding IS NOT NULL AND p.embedding_model = $model "
                    "RETURN p.embedding",
                    "MATCH (n:Note {id: $id}) "
                    "WHERE n.embedding IS NOT NULL AND n.embedding_model = $model "
                    "RETURN n.embedding",
                ):
                    result = conn.execute(query, {"id": note_id, "model": model_id})
                    if result.has_next():
                        vector = result.get_next()[0]
                        if vector is not None:
                            return [float(x) for x in vector]
        except Exception as exc:
            logger.warning("Could not read embedding for %s: %s", note_id, exc)
        return None

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
        where = ["NOT r.link_type IN $routing"]
        params: Dict[str, Any] = {
            "id": note_id,
            "routing": list(routing_link_values),
        }
        if exclude_reference:
            where.append("r.link_type <> $reference")
            params["reference"] = LinkType.REFERENCE.value
        query = (
            "MATCH (s:Note)-[r:LINKS_TO]->(n:Note {id: $id}) "
            "WHERE " + " AND ".join(where) + " RETURN DISTINCT s.id AS id"
        )
        with self._connection() as conn:
            return set(_result_first_column(conn.execute(query, params)))

    def _indexed_embedding_model(self, conn: kuzu.Connection) -> Optional[str]:
        """Return the model id of the embeddings folded into the HNSW index."""
        try:
            result = conn.execute(
                "MATCH (n:Note) WHERE n.embedding_model IS NOT NULL "
                "RETURN n.embedding_model LIMIT 1"
            )
            if result.has_next():
                return result.get_next()[0]
        except Exception:  # pragma: no cover - best-effort
            return None
        return None

    def _index_note(
        self, note: Note, rendered_content: Optional[str] = None
    ) -> None:
        """Upsert a note and its tags/links into the graph database."""
        content_for_db = (
            rendered_content if rendered_content is not None else note.content
        )
        params = self._note_params(note, content_for_db)

        with self._connection() as conn:
            exists_result = conn.execute(
                "MATCH (n:Note {id: $id}) RETURN n.id", {"id": note.id}
            )
            node_exists = exists_result.get_num_tuples() > 0

            if node_exists:
                update_params = {k: v for k, v in params.items() if k != "created_at"}
                conn.execute(
                    """
                    MATCH (n:Note {id: $id})
                    SET n.title = $title,
                        n.content = $content,
                        n.note_type = $note_type,
                        n.status = $status,
                        n.source = $source,
                        n.due_date = $due_date,
                        n.priority = $priority,
                        n.recurrence_rule = $recurrence_rule,
                        n.estimated_minutes = $estimated_minutes,
                        n.remind_at = $remind_at,
                        n.project_id = $project_id,
                        n.area_id = $area_id,
                        n.origin = $origin,
                        n.last_verified = $last_verified,
                        n.metadata_json = $metadata_json,
                        n.updated_at = $updated_at
                    """,
                    update_params,
                )
                conn.execute(
                    "MATCH (n:Note {id: $id})-[r:HAS_TAG]->() DELETE r",
                    {"id": note.id},
                )
                conn.execute(
                    "MATCH (n:Note {id: $id})-[r:LINKS_TO]->() DELETE r",
                    {"id": note.id},
                )
            else:
                conn.execute(
                    """
                    CREATE (:Note {
                        id: $id,
                        title: $title,
                        content: $content,
                        note_type: $note_type,
                        status: $status,
                        source: $source,
                        due_date: $due_date,
                        priority: $priority,
                        recurrence_rule: $recurrence_rule,
                        estimated_minutes: $estimated_minutes,
                        remind_at: $remind_at,
                        project_id: $project_id,
                        area_id: $area_id,
                        origin: $origin,
                        last_verified: $last_verified,
                        metadata_json: $metadata_json,
                        created_at: $created_at,
                        updated_at: $updated_at
                    })
                    """,
                    params,
                )

            self._ensure_tag_nodes(conn, (tag.name for tag in note.tags))
            self._index_note_relations(note, conn, clear_existing=node_exists)
            self._sync_derived_area_membership(note, conn)
            if self._embedding_provider is not None:
                self._set_note_embedding(conn, note)

    def _fetch_notes_by_ids(
        self, conn: kuzu.Connection, ids: List[str]
    ) -> List[Note]:
        """Reconstruct full Note objects for the given IDs from the graph DB."""
        if not ids:
            return []

        note_result = conn.execute(
            f"MATCH (n:Note) WHERE n.id IN $ids RETURN {_NOTE_SELECT}",
            {"ids": ids},
        )
        notes_data = _result_to_records(note_result)
        if not notes_data:
            return []

        tag_result = conn.execute(
            "MATCH (n:Note)-[:HAS_TAG]->(t:Tag) WHERE n.id IN $ids "
            "RETURN n.id AS note_id, t.name AS tag_name",
            {"ids": ids},
        )
        tags_by_note: Dict[str, List[str]] = defaultdict(list)
        while tag_result.has_next():
            row = tag_result.get_next()
            tags_by_note[row[0]].append(row[1])

        link_result = conn.execute(
            "MATCH (n:Note)-[r:LINKS_TO]->(m:Note) WHERE n.id IN $ids "
            "RETURN n.id AS source_id, m.id AS target_id, "
            "r.link_type AS link_type, r.description AS description, "
            "r.created_at AS created_at",
            {"ids": ids},
        )
        links_by_note: Dict[str, List[Any]] = defaultdict(list)
        while link_result.has_next():
            row = link_result.get_next()
            links_by_note[row[0]].append(row[1:])

        note_map: Dict[str, Note] = {}
        for nd in notes_data:
            note_id = nd["id"]
            tags = [Tag(name=name) for name in tags_by_note.get(note_id, [])]
            links = [
                Link(
                    source_id=note_id,
                    target_id=row[0],
                    link_type=LinkType(row[1]),
                    description=row[2],
                    created_at=row[3],
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
        conn: kuzu.Connection,
        index_name: str,
        query: str,
        *,
        conjunctive: bool = False,
    ) -> List[Tuple[str, float]]:
        """Return (note_id, BM25 score) pairs from a full-text query, ranked by score."""
        normalized_query = query.strip()
        if not normalized_query:
            return []

        query_sql = (
            "CALL QUERY_FTS_INDEX("
            f"'Note', '{index_name}', $query"
        )
        if conjunctive:
            query_sql += ", conjunctive := true"
        query_sql += ") RETURN node.id AS id, score ORDER BY score DESC"

        result = conn.execute(
            query_sql,
            {"query": normalized_query},
        )
        pairs: List[Tuple[str, float]] = []
        while result.has_next():
            row = result.get_next()
            pairs.append((row[0], float(row[1])))
        return pairs

    def _query_fts_index(
        self,
        conn: kuzu.Connection,
        index_name: str,
        query: str,
        *,
        conjunctive: bool = False,
    ) -> List[str]:
        """Return note IDs from a full-text search query ordered by score."""
        return [
            note_id
            for note_id, _score in self._query_fts_index_scored(
                conn, index_name, query, conjunctive=conjunctive
            )
        ]

    def text_fts_scores(self, text: str) -> Dict[str, float]:
        """Return {note_id: BM25 score} for *text* against the combined note index.

        Exposes the relevance scores Kuzu already computes for the full-text query
        so callers can rank by BM25 instead of re-deriving order with lexical
        substring heuristics.
        """
        normalized = text.strip() if isinstance(text, str) else ""
        if not normalized:
            return {}
        with self._connection() as conn:
            return {
                note_id: score
                for note_id, score in self._query_fts_index_scored(
                    conn, "note_text_fts", normalized
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
        self, conn: kuzu.Connection, kwargs: Dict[str, Any]
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
            ordered_scored.append(
                self._query_fts_index_scored(conn, "note_text_fts", text_query)
            )

        title_query = kwargs.get("title")
        if isinstance(title_query, str):
            ordered_scored.append(
                self._query_fts_index_scored(
                    conn, "note_title_fts", title_query, conjunctive=True
                )
            )

        content_query = kwargs.get("content")
        if isinstance(content_query, str):
            ordered_scored.append(
                self._query_fts_index_scored(
                    conn, "note_content_fts", content_query, conjunctive=True
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
        """Get a note by exact title (graph-indexed lookup)."""
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note {title: $title}) RETURN n.id AS id", {"title": title}
            )
            if result.get_num_tuples() == 0:
                return None
            note_id = result.get_next()[0]
        return self.get(note_id)

    def get_all(self) -> List[Note]:
        """Return all notes, reconstructed from the graph index."""
        with self._connection() as conn:
            id_result = conn.execute("MATCH (n:Note) RETURN n.id AS id")
            ids = _result_first_column(id_result)
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
            logger.error("Failed to update note in graph database: %s", e)
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
        file_path = self.notes_dir / f"{id}.md"
        if not file_path.exists():
            raise ValueError(f"Note with ID {id} does not exist")

        # Read the note before removal so inline refs can be replaced with its
        # title when they carry no alias.
        deleted_note = self.get(id)
        deleted_title = deleted_note.title if deleted_note else id

        # Incoming sources come from the graph, which includes inline edges, so
        # prose-only referencers are scrubbed too.
        source_notes = self.find_linked_notes(id, "incoming")

        with self.file_lock:
            os.remove(file_path)
        _cache_evict(str(file_path))

        inline_ref_pattern = re.compile(
            r"\[\[\s*" + re.escape(id) + r"(?:\|([^\]]*))?\s*\]\]"
        )

        def _unlink_ref(match: "re.Match[str]") -> str:
            alias = (match.group(1) or "").strip()
            return alias or deleted_title

        for source_note in source_notes:
            file_backed_source = self.get(source_note.id)
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
            conn.execute("MATCH (n:Note {id: $id}) DETACH DELETE n", {"id": id})

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

            match_clauses = ["MATCH (n:Note)"]
            where_parts: List[str] = []
            params: Dict[str, Any] = {}

            if "tag" in kwargs:
                match_clauses.append("MATCH (n)-[:HAS_TAG]->(zt:Tag {name: $tag})")
                params["tag"] = kwargs["tag"]
            elif "tags" in kwargs:
                tag_names = kwargs["tags"]
                if isinstance(tag_names, list):
                    match_clauses.append("MATCH (n)-[:HAS_TAG]->(zt:Tag)")
                    where_parts.append("zt.name IN $tags")
                    params["tags"] = tag_names

            if "linked_to" in kwargs:
                match_clauses.append(
                    "MATCH (n)-[:LINKS_TO]->(lt_target:Note {id: $linked_to})"
                )
                params["linked_to"] = kwargs["linked_to"]

            if "linked_from" in kwargs:
                match_clauses.append(
                    "MATCH (lf_source:Note {id: $linked_from})-[:LINKS_TO]->(n)"
                )
                params["linked_from"] = kwargs["linked_from"]

            if candidate_ids is not None:
                where_parts.append("n.id IN $candidate_ids")
                params["candidate_ids"] = candidate_ids

            _scalar: Dict[str, str] = {
                "note_type": "n.note_type = $note_type",
                "status": "n.status = $status",
                "source": "n.source = $source",
                "priority": "n.priority = $priority",
                "project_id": "n.project_id = $project_id",
                "area_id": "n.area_id = $area_id",
            }
            for kwarg, clause in _scalar.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    v = kwargs[kwarg]
                    params[kwarg] = v.value if hasattr(v, "value") else v

            _ts: Dict[str, str] = {
                "created_after": "n.created_at >= $created_after",
                "created_before": "n.created_at <= $created_before",
                "updated_after": "n.updated_at >= $updated_after",
                "updated_before": "n.updated_at <= $updated_before",
            }
            for kwarg, clause in _ts.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    params[kwarg] = kwargs[kwarg]

            _date: Dict[str, str] = {
                "due_date_before": "n.due_date <= $due_date_before",
                "due_date_after": "n.due_date >= $due_date_after",
                "remind_at_before": "n.remind_at <= $remind_at_before",
                "remind_at_after": "n.remind_at >= $remind_at_after",
            }
            for kwarg, clause in _date.items():
                if kwarg in kwargs:
                    where_parts.append(clause)
                    v = kwargs[kwarg]
                    params[kwarg] = v.isoformat() if hasattr(v, "isoformat") else v

            query_parts = ["\n".join(match_clauses)]
            if where_parts:
                query_parts.append("WHERE " + " AND ".join(where_parts))
            query_parts.append("RETURN DISTINCT n.id AS id")

            result = conn.execute("\n".join(query_parts), params)
            ids = _result_first_column(result)
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
        with self._connection() as conn:
            if direction == "outgoing":
                result = conn.execute(
                    "MATCH (s:Note {id: $id})-[:LINKS_TO]->(t:Note) "
                    "RETURN DISTINCT t.id AS id",
                    {"id": note_id},
                )
                ids = _result_first_column(result)
            elif direction == "incoming":
                result = conn.execute(
                    "MATCH (s:Note)-[:LINKS_TO]->(t:Note {id: $id}) "
                    "RETURN DISTINCT s.id AS id",
                    {"id": note_id},
                )
                ids = _result_first_column(result)
            elif direction == "both":
                out_result = conn.execute(
                    "MATCH (s:Note {id: $id})-[:LINKS_TO]->(t:Note) "
                    "RETURN DISTINCT t.id AS id",
                    {"id": note_id},
                )
                in_result = conn.execute(
                    "MATCH (s:Note)-[:LINKS_TO]->(t:Note {id: $id}) "
                    "RETURN DISTINCT s.id AS id",
                    {"id": note_id},
                )
                ids_set: Set[str] = set(_result_first_column(out_result))
                ids_set.update(_result_first_column(in_result))
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

        Derived from HAS_TAG edges rather than Tag-node existence so the result
        reflects tags actually in use. A Tag node only exists to classify notes,
        so a node with no incoming HAS_TAG edge is orphaned cruft (left behind
        when a tag is removed from its last note) and is intentionally excluded.
        """
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (:Note)-[:HAS_TAG]->(t:Tag) "
                "RETURN DISTINCT t.name AS name"
            )
            return [Tag(name=name) for name in _result_first_column(result)]

    def get_link(
        self, source_id: str, target_id: str, link_type: str
    ) -> Optional[Dict[str, Any]]:
        """Return the stored properties of a specific directed link, or None."""
        with self._connection() as conn:
            result = conn.execute(
                """
                MATCH (s:Note {id: $source_id})
                      -[r:LINKS_TO {link_type: $link_type}]->
                      (t:Note {id: $target_id})
                RETURN r.created_at AS created_at
                """,
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "link_type": link_type,
                },
            )
            if result.get_num_tuples() == 0:
                return None
            return {"created_at": result.get_next()[0]}

    def record_retrieval(self, note_ids: List[str]) -> None:
        """Bump retrieval signals (last_retrieved_at, hit_count) for *note_ids*.

        Graph-only operational data: deliberately NOT written to markdown, so
        reading a note never rewrites its file. Signals survive rebuilds via the
        explicit carry-over in :meth:`rebuild_index`. No-op in read-only mode.
        """
        if not note_ids or self.read_only:
            return
        with self._connection() as conn:
            conn.execute(
                "UNWIND $ids AS nid MATCH (n:Note {id: nid}) "
                "SET n.last_retrieved_at = $now, "
                "n.hit_count = coalesce(n.hit_count, 0) + 1",
                {"ids": list(note_ids), "now": datetime.datetime.now()},
            )

    def get_retrieval_signals(
        self, note_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Return {note_id: {last_retrieved_at, hit_count}} for ids with signals."""
        if not note_ids:
            return {}
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note) WHERE n.id IN $ids AND n.hit_count IS NOT NULL "
                "RETURN n.id AS id, n.last_retrieved_at AS last_retrieved_at, "
                "n.hit_count AS hit_count",
                {"ids": list(note_ids)},
            )
            signals: Dict[str, Dict[str, Any]] = {}
            while result.has_next():
                row = result.get_next()
                signals[row[0]] = {
                    "last_retrieved_at": row[1],
                    "hit_count": row[2],
                }
            return signals

    def _fetch_retrieval_signals(self) -> List[Dict[str, Any]]:
        """Snapshot all retrieval signals for carry-over into a rebuilt graph."""
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note) WHERE n.hit_count IS NOT NULL "
                "RETURN n.id AS id, n.last_retrieved_at AS last_retrieved_at, "
                "n.hit_count AS hit_count"
            )
            rows: List[Dict[str, Any]] = []
            while result.has_next():
                row = result.get_next()
                rows.append(
                    {
                        "id": row[0],
                        "last_retrieved_at": row[1],
                        "hit_count": row[2],
                    }
                )
            return rows

    def find_orphaned_note_ids(self) -> List[str]:
        """Return IDs of notes that have no links in either direction."""
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note) "
                "OPTIONAL MATCH (n)-[:LINKS_TO]->(out:Note) "
                "WITH n, count(DISTINCT out) AS outgoing "
                "OPTIONAL MATCH (incoming:Note)-[:LINKS_TO]->(n) "
                "WITH n, outgoing, count(DISTINCT incoming) AS incoming "
                "WHERE outgoing = 0 AND incoming = 0 "
                "RETURN n.id AS id"
            )
            return _result_first_column(result)

    def get_connection_counts(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Return (note_id, connection_count) for the most-connected notes."""
        with self._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note) "
                "OPTIONAL MATCH (n)-[:LINKS_TO]->(out:Note) "
                "WITH n, count(DISTINCT out) AS outgoing "
                "OPTIONAL MATCH (incoming:Note)-[:LINKS_TO]->(n) "
                "WITH n, outgoing, count(DISTINCT incoming) AS incoming "
                "WITH n.id AS id, outgoing + incoming AS cnt "
                "WHERE cnt > 0 "
                "RETURN id, cnt "
                "ORDER BY cnt DESC, id ASC "
                "LIMIT $limit",
                {"limit": limit},
            )

            ranked: List[Tuple[str, int]] = []
            while result.has_next():
                row = result.get_next()
                ranked.append((row[0], row[1]))
            return ranked
