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
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import frontmatter
import kuzu

from parazettel_mcp.config import config
from parazettel_mcp.models.graph_db import close_graph_db, init_graph_db
from parazettel_mcp.models.schema import (
    Link,
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
    Tag,
)
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

_NOTE_SELECT = (
    "n.id AS id, n.title AS title, n.content AS content, n.note_type AS note_type, "
    "n.status AS status, n.source AS source, n.due_date AS due_date, "
    "n.priority AS priority, n.recurrence_rule AS recurrence_rule, "
    "n.estimated_minutes AS estimated_minutes, n.remind_at AS remind_at, "
    "n.project_id AS project_id, n.area_id AS area_id, "
    "n.metadata_json AS metadata_json, n.created_at AS created_at, n.updated_at AS updated_at"
)


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
        self.db = init_graph_db(self.graph_db_path)
        self.file_lock = threading.RLock()
        self._closed = False

        self.rebuild_index_if_needed()

    def close(self) -> None:
        """Release resources held by this repository."""
        if self._closed:
            return
        close_graph_db(self.graph_db_path)
        self._closed = True

    def _get_conn(self) -> kuzu.Connection:
        """Create a new Kuzu connection for a single operation.

        Kuzu connections are not thread-safe, so one connection is created per
        repository operation.  This is the standard Kuzu pattern for concurrent
        access: multiple ``Connection`` objects share the same underlying
        ``Database`` which manages concurrency at the storage layer.
        """
        return kuzu.Connection(self.db)

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
                    tmp_path.replace(file_path)
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

    def rebuild_index_if_needed(self) -> None:
        """Rebuild the graph index from files when the ID sets diverge."""
        conn = self._get_conn()
        id_result = conn.execute("MATCH (n:Note) RETURN n.id")
        db_ids = set(_result_first_column(id_result))
        file_stems = {p.stem for p in self.notes_dir.glob("*.md")}
        if db_ids != file_stems:
            self.rebuild_index()

    def rebuild_index(self) -> None:
        """Rebuild the graph index from all markdown files.

        Uses a two-pass strategy:
        * Pass 1 – create all Note nodes and Tag nodes.
        * Pass 2 – create all LINKS_TO / HAS_TAG relationships.

        This guarantees that LINKS_TO edges are created even when the source
        note appears before the target note in filesystem order.
        """
        conn = self._get_conn()
        conn.execute("MATCH (n:Note) DETACH DELETE n")
        conn.execute("MATCH (t:Tag) DETACH DELETE t")

        note_files = list(self.notes_dir.glob("*.md"))
        notes: List[Note] = []
        for file_path in note_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                note = self._parse_note_from_markdown(content)
                notes.append(note)
            except Exception as e:
                logger.error("Error processing file %s: %s", file_path, e)

        # Pass 1: create all Note nodes and Tags (no relationships yet)
        batch_size = 100
        for i in range(0, len(notes), batch_size):
            for note in notes[i : i + batch_size]:
                self._index_note_nodes_only(note, conn)

        # Pass 2: create all relationships
        for i in range(0, len(notes), batch_size):
            for note in notes[i : i + batch_size]:
                self._index_note_relations(note, conn)

    def _index_note_nodes_only(self, note: Note, conn: kuzu.Connection) -> None:
        """Create or update only the Note node and Tag nodes (no edges)."""
        params = self._note_params(note, note.content)

        exists_result = conn.execute(
            "MATCH (n:Note {id: $id}) RETURN n.id", {"id": note.id}
        )
        if exists_result.get_num_tuples() > 0:
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
                    metadata_json: $metadata_json,
                    created_at: $created_at,
                    updated_at: $updated_at
                })
                """,
                params,
            )

        for tag in note.tags:
            conn.execute("MERGE (:Tag {name: $name})", {"name": tag.name})

    def _index_note_relations(self, note: Note, conn: kuzu.Connection) -> None:
        """Create HAS_TAG and LINKS_TO relationships for a note."""
        conn.execute(
            "MATCH (n:Note {id: $id})-[r:HAS_TAG]->() DELETE r", {"id": note.id}
        )
        conn.execute(
            "MATCH (n:Note {id: $id})-[r:LINKS_TO]->() DELETE r", {"id": note.id}
        )
        for tag in note.tags:
            conn.execute(
                """
                MATCH (n:Note {id: $note_id}), (t:Tag {name: $tag_name})
                CREATE (n)-[:HAS_TAG]->(t)
                """,
                {"note_id": note.id, "tag_name": tag.name},
            )
        for link in note.links:
            conn.execute(
                """
                MATCH (s:Note {id: $source_id}), (t:Note {id: $target_id})
                CREATE (s)-[:LINKS_TO {
                    link_type: $link_type,
                    description: $description,
                    created_at: $created_at
                }]->(t)
                """,
                {
                    "source_id": link.source_id,
                    "target_id": link.target_id,
                    "link_type": link.link_type.value,
                    "description": link.description,
                    "created_at": link.created_at,
                },
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
        tags = [Tag(name=name) for name in tag_names]

        links: List[Link] = []
        links_section = False
        for line in post.content.split("\n"):
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
                        if len(id_and_description) > 1:
                            description = id_and_description[1].strip()
                        try:
                            link_type = LinkType(link_type_str)
                        except ValueError:
                            link_type = LinkType.REFERENCE
                        links.append(
                            Link(
                                source_id=note_id,
                                target_id=target_id,
                                link_type=link_type,
                                description=description,
                                created_at=datetime.datetime.now(),
                            )
                        )
                except Exception as e:
                    logger.error("Error parsing link: %s - %s", line, e)

        created_at = _coerce_datetime(metadata.get("created"), datetime.datetime.now())
        updated_at = _coerce_datetime(metadata.get("updated"), created_at)

        _action_keys = {
            "id", "title", "type", "tags", "created", "updated",
            "status", "source", "due_date", "priority", "recurrence_rule",
            "estimated_minutes", "remind_at", "project_id", "area_id",
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
        )

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

        if note.links:
            unique_links: Dict[str, Link] = {}
            for link in note.links:
                key = f"{link.target_id}:{link.link_type.value}"
                unique_links[key] = link
            title_map = self._get_link_title_map(note, list(unique_links.values()))
            content += "\n\n## Links\n"
            for link in unique_links.values():
                desc = f" {link.description}" if link.description else ""
                target_ref = self._format_wiki_link_target(link.target_id, title_map)
                content += f"- {link.link_type.value} [[{target_ref}]]{desc}\n"

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

        conn = self._get_conn()
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
            "metadata_json": (
                json.dumps(note.metadata, default=_json_default)
                if note.metadata
                else None
            ),
            "created_at": note.created_at,
            "updated_at": note.updated_at,
        }

    def _index_note(
        self, note: Note, rendered_content: Optional[str] = None
    ) -> None:
        """Upsert a note and its tags/links into the graph database."""
        conn = self._get_conn()
        content_for_db = (
            rendered_content if rendered_content is not None else note.content
        )
        params = self._note_params(note, content_for_db)

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
                    n.metadata_json = $metadata_json,
                    n.updated_at = $updated_at
                """,
                update_params,
            )
            conn.execute(
                "MATCH (n:Note {id: $id})-[r:HAS_TAG]->() DELETE r", {"id": note.id}
            )
            conn.execute(
                "MATCH (n:Note {id: $id})-[r:LINKS_TO]->() DELETE r", {"id": note.id}
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
                    metadata_json: $metadata_json,
                    created_at: $created_at,
                    updated_at: $updated_at
                })
                """,
                params,
            )

        for tag in note.tags:
            conn.execute("MERGE (:Tag {name: $name})", {"name": tag.name})
            conn.execute(
                """
                MATCH (n:Note {id: $note_id}), (t:Tag {name: $tag_name})
                CREATE (n)-[:HAS_TAG]->(t)
                """,
                {"note_id": note.id, "tag_name": tag.name},
            )

        for link in note.links:
            conn.execute(
                """
                MATCH (s:Note {id: $source_id}), (t:Note {id: $target_id})
                CREATE (s)-[:LINKS_TO {
                    link_type: $link_type,
                    description: $description,
                    created_at: $created_at
                }]->(t)
                """,
                {
                    "source_id": link.source_id,
                    "target_id": link.target_id,
                    "link_type": link.link_type.value,
                    "description": link.description,
                    "created_at": link.created_at,
                },
            )

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

        notes = []
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
            notes.append(_db_dict_to_note(nd, tags, links))
        return notes

    def create(self, note: Note) -> Note:
        """Create a new note."""
        if not note.id:
            from parazettel_mcp.models.schema import generate_id

            note.id = generate_id()

        markdown = self._note_to_markdown(note)
        file_path = self.notes_dir / f"{note.id}.md"
        self._write_markdown_atomically(file_path, markdown)

        rendered_body = frontmatter.loads(markdown).content
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
        conn = self._get_conn()
        result = conn.execute(
            "MATCH (n:Note {title: $title}) RETURN n.id AS id", {"title": title}
        )
        if result.get_num_tuples() == 0:
            return None
        return self.get(result.get_next()[0])

    def get_all(self) -> List[Note]:
        """Return all notes, reconstructed from the graph index."""
        conn = self._get_conn()
        id_result = conn.execute("MATCH (n:Note) RETURN n.id AS id")
        ids = _result_first_column(id_result)
        return self._fetch_notes_by_ids(conn, ids)

    def update(self, note: Note) -> Note:
        """Update a note."""
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
        try:
            self._index_note(note, rendered_content=rendered_body)
        except Exception as e:
            logger.error("Failed to update note in graph database: %s", e)
            raise

        return note

    def delete(self, id: str) -> None:
        """Delete a note by ID."""
        file_path = self.notes_dir / f"{id}.md"
        if not file_path.exists():
            raise ValueError(f"Note with ID {id} does not exist")

        source_notes = self.find_linked_notes(id, "incoming")

        with self.file_lock:
            os.remove(file_path)
        _cache_evict(str(file_path))

        for source_note in source_notes:
            file_backed_source = self.get(source_note.id)
            if not file_backed_source:
                continue
            existing_source = file_backed_source.model_copy(deep=True)
            file_backed_source.remove_link(id)
            self.update_preserving_updated_at(
                file_backed_source,
                existing_note=existing_source,
                existing_links_source=source_note,
            )

        conn = self._get_conn()
        conn.execute("MATCH (n:Note {id: $id}) DETACH DELETE n", {"id": id})

    def search(self, **kwargs: Any) -> List[Note]:
        """Search for notes based on criteria.

        Supported keyword arguments
        ---------------------------
        content, title, note_type, tag, tags, linked_to, linked_from,
        created_after, created_before, updated_after, updated_before,
        status, source, due_date_before, due_date_after, priority,
        remind_at_before, remind_at_after, project_id, area_id
        """
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

        if "content" in kwargs:
            where_parts.append(
                "(n.content CONTAINS $content OR n.title CONTAINS $content)"
            )
            params["content"] = kwargs["content"]

        if "title" in kwargs:
            where_parts.append("LOWER(n.title) CONTAINS $title_lower")
            params["title_lower"] = kwargs["title"].lower()

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

        conn = self._get_conn()
        result = conn.execute("\n".join(query_parts), params)
        ids = _result_first_column(result)

        return self._fetch_notes_by_ids(conn, ids)

    def find_by_tag(self, tag: Union[str, Tag]) -> List[Note]:
        """Find notes by tag."""
        tag_name = tag.name if isinstance(tag, Tag) else tag
        return self.search(tag=tag_name)

    def find_linked_notes(
        self, note_id: str, direction: str = "outgoing"
    ) -> List[Note]:
        """Find notes linked to/from the given note."""
        conn = self._get_conn()

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
        """Return all tags stored in the graph."""
        conn = self._get_conn()
        result = conn.execute("MATCH (t:Tag) RETURN t.name AS name")
        return [Tag(name=name) for name in _result_first_column(result)]

    def get_link(
        self, source_id: str, target_id: str, link_type: str
    ) -> Optional[Dict[str, Any]]:
        """Return the stored properties of a specific directed link, or None."""
        conn = self._get_conn()
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

    def find_orphaned_note_ids(self) -> List[str]:
        """Return IDs of notes that have no links in either direction."""
        conn = self._get_conn()
        all_ids = set(_result_first_column(conn.execute("MATCH (n:Note) RETURN n.id AS id")))
        linked = set(_result_first_column(
            conn.execute("MATCH (n:Note)-[:LINKS_TO]->(:Note) RETURN DISTINCT n.id AS id")
        ))
        linked.update(_result_first_column(
            conn.execute("MATCH (:Note)-[:LINKS_TO]->(n:Note) RETURN DISTINCT n.id AS id")
        ))
        return list(all_ids - linked)

    def get_connection_counts(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Return (note_id, connection_count) for the most-connected notes."""
        conn = self._get_conn()

        out_result = conn.execute(
            "MATCH (n:Note)-[:LINKS_TO]->(:Note) "
            "RETURN n.id AS id, count(*) AS cnt"
        )
        in_result = conn.execute(
            "MATCH (:Note)-[:LINKS_TO]->(n:Note) "
            "RETURN n.id AS id, count(*) AS cnt"
        )

        degree: Dict[str, int] = {}
        while out_result.has_next():
            row = out_result.get_next()
            degree[row[0]] = degree.get(row[0], 0) + row[1]
        while in_result.has_next():
            row = in_result.get_next()
            degree[row[0]] = degree.get(row[0], 0) + row[1]

        ranked = sorted(degree.items(), key=lambda x: x[1], reverse=True)
        return ranked[:limit]
