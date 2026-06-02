"""Service layer for Zettelkasten operations."""

import datetime
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import (
    Link,
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
    Tag,
)
from parazettel_mcp.storage.note_repository import NoteRepository

logger = logging.getLogger(__name__)
_UNSET = object()
_INVERSE_LINK_TYPES = {
    LinkType.REFERENCE: LinkType.REFERENCE,
    LinkType.EXTENDS: LinkType.EXTENDED_BY,
    LinkType.EXTENDED_BY: LinkType.EXTENDS,
    LinkType.REFINES: LinkType.REFINED_BY,
    LinkType.REFINED_BY: LinkType.REFINES,
    LinkType.CONTRADICTS: LinkType.CONTRADICTED_BY,
    LinkType.CONTRADICTED_BY: LinkType.CONTRADICTS,
    LinkType.QUESTIONS: LinkType.QUESTIONED_BY,
    LinkType.QUESTIONED_BY: LinkType.QUESTIONS,
    LinkType.SUPPORTS: LinkType.SUPPORTED_BY,
    LinkType.SUPPORTED_BY: LinkType.SUPPORTS,
    LinkType.RELATED: LinkType.RELATED,
    LinkType.PART_OF: LinkType.HAS_PART,
    LinkType.HAS_PART: LinkType.PART_OF,
    LinkType.BLOCKS: LinkType.BLOCKED_BY,
    LinkType.BLOCKED_BY: LinkType.BLOCKS,
}


class ZettelService:
    """Service for managing Zettelkasten notes."""

    def __init__(self, repository: Optional[NoteRepository] = None):
        """Initialize the service."""
        self.repository = repository or NoteRepository()
        # Per-note locks guard read-modify-write sequences (get -> mutate ->
        # update) against concurrent edits to the *same* note, which would
        # otherwise last-writer-win. A registry lock guards the registry itself.
        self._note_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._note_locks_registry_lock = threading.Lock()
        # Note ids whose lock the *current thread* already holds. Lets nested
        # acquisitions (e.g. a routing helper that also touches the parent note
        # while an update already holds the child's lock) skip re-acquiring an
        # owned lock instead of deadlocking on the non-reentrant Lock.
        self._held_note_ids = threading.local()

    def _owned_ids(self) -> Set[str]:
        owned = getattr(self._held_note_ids, "ids", None)
        if owned is None:
            owned = set()
            self._held_note_ids.ids = owned
        return owned

    @contextmanager
    def _note_lock(self, note_id: str) -> Iterator[None]:
        """Serialize mutations to a single note (see _note_locks_for)."""
        with self._note_locks_for(note_id):
            yield

    @contextmanager
    def _note_locks_for(self, *note_ids: str) -> Iterator[None]:
        """Acquire per-note locks for one or more notes, deadlock- and reentrancy-safe.

        Locks the requested notes that this thread does not already hold, always
        in a global stable order (sorted ids), so two operations touching an
        overlapping set can't deadlock by grabbing them in opposite orders. Ids
        already owned by the current thread are skipped (the underlying Lock is
        non-reentrant), which lets a routing helper acquire the parent/area lock
        while the enclosing update already holds the child's lock.

        Locks are taken at the service layer so they span the whole
        read-modify-write, and always before the repository's file lock.
        """
        owned = self._owned_ids()
        to_acquire = sorted({nid for nid in note_ids if nid and nid not in owned})
        with self._note_locks_registry_lock:
            locks = [(nid, self._note_locks[nid]) for nid in to_acquire]
        acquired = []
        try:
            for nid, lock in locks:
                lock.acquire()
                acquired.append((nid, lock))
                owned.add(nid)
            yield
        finally:
            for nid, lock in reversed(acquired):
                owned.discard(nid)
                lock.release()

    @contextmanager
    def _locked_for_routing(
        self, note_id: str, project_id: Any, area_id: Any
    ) -> Iterator[None]:
        """Lock a note plus all its routing endpoints, closing the read-vs-lock race.

        The endpoint ids (current + new parent/area) are read without a lock to
        size the set, so a concurrent reassignment could change them between that
        read and acquisition. We therefore lock the computed set, re-resolve the
        endpoints *under* the locks, and if a new endpoint appeared, release and
        retry with the superset. This converges (the id set only grows across a
        bounded set of real endpoints) and never holds locks while re-reading.
        """
        seen: Set[str] = set()
        while True:
            related = set(self._related_routing_ids(note_id, project_id, area_id))
            target = sorted({note_id} | related | seen)
            with self._note_locks_for(*target):
                confirmed = set(self._related_routing_ids(note_id, project_id, area_id))
                if confirmed <= (related | seen):
                    yield
                    return
                # A new endpoint appeared after the unlocked read; widen and retry.
                seen |= confirmed | related

    def initialize(self) -> None:
        """Initialize the service and dependencies."""
        # Nothing to do here for synchronous implementation
        # The repository is initialized in its constructor
        pass

    def close(self) -> None:
        """Release resources held by the service."""
        self.repository.close()

    def _get_area_for_routing(self, area_id: str) -> Note:
        """Return a validated area note for PARA routing."""
        area = self.repository.get(area_id)
        if not area:
            raise ValueError(f"Area note with ID {area_id} not found")
        if area.note_type != NoteType.AREA:
            raise ValueError(
                f"Note {area_id} is not an area (type: {area.note_type.value})"
            )
        return area

    def _get_project_for_routing(self, project_id: str) -> Note:
        """Return a validated project note for PARA routing."""
        project = self.repository.get(project_id)
        if not project:
            raise ValueError(f"Project note with ID {project_id} not found")
        if project.note_type != NoteType.PROJECT:
            raise ValueError(
                f"Note {project_id} is not a project (type: {project.note_type.value})"
            )
        return project

    def _seed_routing_links(self, note: Note, parent_id: Optional[str] = None) -> Note:
        """Attach stable routing links before the first file write."""
        if note.area_id and note.note_type != NoteType.AREA and note.area_id != note.id:
            note.add_link(note.area_id, LinkType.REFERENCE)
        if parent_id:
            note.add_link(parent_id, LinkType.PART_OF)
        return note

    def _ensure_parent_has_part_link(self, parent_id: Optional[str], child_id: str) -> None:
        """Update the parent note once so it reflects the child relationship.

        Takes the parent's per-note lock (a no-op if the caller already holds it)
        so the parent's HAS_PART read-modify-write isn't clobbered by a
        concurrent edit to that parent.
        """
        if not parent_id:
            return
        with self._note_locks_for(parent_id):
            parent = self.repository.get(parent_id)
            if not parent:
                raise ValueError(f"Parent note with ID {parent_id} not found")
            if any(
                link.target_id == child_id and link.link_type == LinkType.HAS_PART
                for link in parent.links
            ):
                return
            parent.add_link(child_id, LinkType.HAS_PART)
            self.repository.update(parent)

    def _attach_area_reference_link(self, note_id: str, area_id: Optional[str]) -> Note:
        """Ensure a newly created note references its assigned area."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        if not area_id or note.note_type == NoteType.AREA or area_id == note.id:
            return note
        # Already inside a locked update of note.id; use the unlocked impl so we
        # don't re-enter the non-reentrant per-note lock.
        note, _ = self._create_link_locked(note.id, area_id, LinkType.REFERENCE)
        return note

    def _sync_part_of_link(
        self, note_id: str, previous_parent_id: Optional[str], parent_id: Optional[str]
    ) -> Note:
        """Synchronize PART_OF/HAS_PART links with the note's current parent routing."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")

        if previous_parent_id and previous_parent_id != parent_id:
            note.remove_link(previous_parent_id, LinkType.PART_OF)
            note = self.repository.update(note)
            previous_parent = self.repository.get(previous_parent_id)
            if previous_parent:
                previous_parent.remove_link(note.id, LinkType.HAS_PART)
                self.repository.update(previous_parent)

        if parent_id and previous_parent_id != parent_id:
            note, _ = self._create_link_locked(
                note.id, parent_id, LinkType.PART_OF, bidirectional=True
            )
        return note

    def _sync_project_area_links(
        self, note_id: str, previous_area_id: Optional[str], area_id: Optional[str]
    ) -> Note:
        """Synchronize REFERENCE/PART_OF area links for project notes after routing changes."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")

        if previous_area_id and previous_area_id != area_id:
            note.remove_link(previous_area_id, LinkType.REFERENCE)
            note.remove_link(previous_area_id, LinkType.PART_OF)
            note = self.repository.update(note)
            previous_area = self.repository.get(previous_area_id)
            if previous_area:
                previous_area.remove_link(note.id, LinkType.HAS_PART)
                self.repository.update(previous_area)

        if area_id and previous_area_id != area_id:
            note.add_link(area_id, LinkType.REFERENCE)
            note.add_link(area_id, LinkType.PART_OF)
            note = self.repository.update(note)
            self._ensure_parent_has_part_link(area_id, note.id)

        return note

    def _sync_area_reference_link(
        self, note_id: str, previous_area_id: Optional[str], area_id: Optional[str]
    ) -> Note:
        """Keep a note's REFERENCE link to its area in sync after re-routing.

        Applies to any non-area, non-project note (permanent, literature,
        fleeting, task, etc.). Such a note carries a single REFERENCE link to its
        area, seeded at creation in _seed_routing_links. When the note is later
        re-routed to a different area (directly via area_id, or indirectly via a
        project whose area differs), the stale REFERENCE must be removed and a
        fresh one added so the markdown ## Links section matches the note's
        area_id. Projects use _sync_project_area_links instead and are not
        handled here.
        """
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        if previous_area_id == area_id:
            return note

        if previous_area_id and previous_area_id != note.id:
            note.remove_link(previous_area_id, LinkType.REFERENCE)
            note = self.repository.update(note)

        if area_id and area_id != note.id:
            note, _ = self._create_link_locked(note.id, area_id, LinkType.REFERENCE)

        return note

    def create_note(
        self,
        title: str,
        content: str,
        note_type: NoteType = NoteType.PERMANENT,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: NoteSource = NoteSource.MANUAL,
        status: Optional[NoteStatus] = None,
        project_id: Optional[str] = None,
        area_id: Optional[str] = None,
    ) -> Note:
        """Create a new note."""
        if not title:
            raise ValueError("Title is required")
        if not content:
            raise ValueError("Content is required")
        if area_id and note_type != NoteType.AREA:
            self._get_area_for_routing(area_id)

        resolved_area_id = area_id
        if project_id:
            project = self._get_project_for_routing(project_id)
            project_area_id = project.area_id
            if project_area_id:
                if resolved_area_id and resolved_area_id != project_area_id:
                    raise ValueError(
                        f"area_id {resolved_area_id} does not match project "
                        f"{project_id} area_id {project_area_id}"
                    )
                resolved_area_id = project_area_id
            elif not resolved_area_id:
                raise ValueError(
                    f"Project {project_id} does not have an area_id to inherit"
                )

        # Create note object
        note = Note(
            title=title,
            content=content,
            note_type=note_type,
            tags=[Tag(name=tag) for tag in (tags or [])],
            metadata=metadata or {},
            source=source,
            status=status,
            project_id=project_id,
            area_id=resolved_area_id,
        )

        if note_type == NoteType.AREA:
            note.area_id = note.id
        else:
            note = self._seed_routing_links(note, parent_id=project_id)

        note = self.repository.create(note)
        self._ensure_parent_has_part_link(project_id, note.id)
        return note

    def get_note(self, note_id: str) -> Optional[Note]:
        """Retrieve a note by ID."""
        return self.repository.get(note_id)

    def get_note_by_title(self, title: str) -> Optional[Note]:
        """Retrieve a note by title."""
        return self.repository.get_by_title(title)

    def update_note(
        self,
        note_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        note_type: Optional[NoteType] = None,
        tags: Optional[List[str]] = None,
        status: Any = _UNSET,
        metadata: Optional[Dict[str, Any]] = None,
        project_id: Any = _UNSET,
        area_id: Any = _UNSET,
    ) -> Note:
        """Update an existing note (serialized per note against concurrent edits).

        Locks the note plus its current and new parent/area up front, in one
        stable-ordered acquisition (re-resolved under the locks to avoid a
        read-vs-lock race), so routing changes that also rewrite a parent's
        HAS_PART link can't lose writes or deadlock. Note: incoming-link alias
        refresh after a title change updates other source notes outside this lock
        set; those are independent single-note writes guarded by their own locks
        within update_preserving_updated_at's call path.
        """
        with self._locked_for_routing(note_id, project_id, area_id):
            return self._update_note_locked(
                note_id,
                title=title,
                content=content,
                note_type=note_type,
                tags=tags,
                status=status,
                metadata=metadata,
                project_id=project_id,
                area_id=area_id,
            )

    def _related_routing_ids(
        self, note_id: str, project_id: Any, area_id: Any
    ) -> List[str]:
        """Best-effort set of other note ids an update/routing change may mutate.

        Read without a lock purely to size the lock set; the authoritative work
        re-reads under the held locks. Includes the note's current parent/area
        and any incoming new project/area routing, so all endpoints of a
        PART_OF/HAS_PART or area-reference change are locked together.
        """
        ids: Set[str] = set()
        note = self.repository.get(note_id)
        if note:
            if note.project_id:
                ids.add(note.project_id)
            if note.area_id:
                ids.add(note.area_id)
        if isinstance(project_id, str) and project_id:
            ids.add(project_id)
            project = self.repository.get(project_id)
            if project and project.area_id:
                ids.add(project.area_id)
        if isinstance(area_id, str) and area_id:
            ids.add(area_id)
        ids.discard(note_id)
        return sorted(ids)

    def _update_note_locked(
        self,
        note_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        note_type: Optional[NoteType] = None,
        tags: Optional[List[str]] = None,
        status: Any = _UNSET,
        metadata: Optional[Dict[str, Any]] = None,
        project_id: Any = _UNSET,
        area_id: Any = _UNSET,
    ) -> Note:
        """Update implementation; caller holds the per-note lock."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        title_changed = title is not None and title != note.title
        previous_project_id = note.project_id
        previous_area_id = note.area_id

        # Update fields
        if title is not None:
            note.title = title
        if content is not None:
            note.content = content
        if note_type is not None:
            note.note_type = note_type
        if tags is not None:
            note.tags = [Tag(name=tag) for tag in tags]
        if status is not _UNSET:
            note.status = status
        if metadata is not None:
            note.metadata = metadata
        if project_id is not _UNSET:
            note.project_id = project_id
        if area_id is not _UNSET:
            note.area_id = area_id

        if note.note_type == NoteType.AREA:
            if note.project_id:
                raise ValueError("Area notes cannot belong to a project")
            note.project_id = None
            note.area_id = note.id
        elif note.note_type == NoteType.PROJECT:
            if note.project_id:
                project = self._get_project_for_routing(note.project_id)
                if not project.area_id:
                    raise ValueError(
                        f"Project {note.project_id} does not have an area_id to inherit"
                    )
                if (
                    area_id is not _UNSET
                    and note.area_id
                    and note.area_id != project.area_id
                ):
                    raise ValueError(
                        f"area_id {note.area_id} does not match project "
                        f"{note.project_id} area_id {project.area_id}"
                    )
                note.area_id = project.area_id
            elif not note.area_id:
                raise ValueError(
                    "Projects must be associated with an area (area_id required)"
                )
            else:
                self._get_area_for_routing(note.area_id)
        else:
            if note.area_id:
                self._get_area_for_routing(note.area_id)
            if note.project_id:
                project = self._get_project_for_routing(note.project_id)
                if not project.area_id:
                    raise ValueError(
                        f"Project {note.project_id} does not have an area_id to inherit"
                    )
                if (
                    area_id is not _UNSET
                    and note.area_id
                    and note.area_id != project.area_id
                ):
                    raise ValueError(
                        f"area_id {note.area_id} does not match project "
                        f"{note.project_id} area_id {project.area_id}"
                    )
                note.area_id = project.area_id

        note.updated_at = datetime.datetime.now()

        # Save to repository
        note = self.repository.update(note)
        if note.note_type == NoteType.PROJECT:
            note = self._sync_project_area_links(
                note.id, previous_area_id, note.area_id
            )
        elif (
            note.note_type != NoteType.AREA
            and previous_area_id != note.area_id
        ):
            note = self._sync_area_reference_link(
                note.id, previous_area_id, note.area_id
            )
        note = self._sync_part_of_link(
            note.id, previous_project_id, note.project_id
        )
        if title_changed:
            self._refresh_incoming_link_aliases(note.id)
        return note

    def _refresh_incoming_link_aliases(self, note_id: str) -> None:
        """Rewrite incoming source notes so aliases follow the target title."""
        incoming_notes = self.repository.find_linked_notes(note_id, "incoming")
        for incoming_note in incoming_notes:
            source_note = self.repository.get(incoming_note.id)
            if not source_note:
                continue
            existing_source = source_note.model_copy(deep=True)
            self.repository.update_preserving_updated_at(
                source_note,
                existing_note=existing_source,
                existing_links_source=incoming_note,
            )

    def delete_note(self, note_id: str) -> None:
        """Delete a note (serialized per note against concurrent edits)."""
        with self._note_lock(note_id):
            self.repository.delete(note_id)

    def get_all_notes(self) -> List[Note]:
        """Get all notes."""
        return self.repository.get_all()

    def search_notes(self, **kwargs: Any) -> List[Note]:
        """Search for notes based on criteria."""
        return self.repository.search(**kwargs)

    def get_notes_by_tag(self, tag: str) -> List[Note]:
        """Get notes by tag."""
        return self.repository.find_by_tag(tag)

    def add_tag_to_note(self, note_id: str, tag: str) -> Note:
        """Add a tag to a note (serialized per note against concurrent edits)."""
        with self._note_lock(note_id):
            note = self.repository.get(note_id)
            if not note:
                raise ValueError(f"Note with ID {note_id} not found")
            note.add_tag(tag)
            return self.repository.update(note)

    def remove_tag_from_note(self, note_id: str, tag: str) -> Note:
        """Remove a tag from a note (serialized per note against concurrent edits)."""
        with self._note_lock(note_id):
            note = self.repository.get(note_id)
            if not note:
                raise ValueError(f"Note with ID {note_id} not found")
            note.remove_tag(tag)
            return self.repository.update(note)

    def get_all_tags(self) -> List[Tag]:
        """Get all tags in the system."""
        return self.repository.get_all_tags()

    def create_link(
        self,
        source_id: str,
        target_id: str,
        link_type: LinkType = LinkType.REFERENCE,
        description: Optional[str] = None,
        bidirectional: bool = False,
        bidirectional_type: Optional[LinkType] = None,
    ) -> Tuple[Note, Optional[Note]]:
        """Create a link between notes (serialized per note against concurrent edits).

        Locks both endpoints (in a stable order) so a concurrent edit to either
        note can't interleave with this read-modify-write. Internal callers that
        already hold a note's lock use _create_link_locked instead.
        """
        with self._note_locks_for(source_id, target_id):
            return self._create_link_locked(
                source_id,
                target_id,
                link_type=link_type,
                description=description,
                bidirectional=bidirectional,
                bidirectional_type=bidirectional_type,
            )

    def _create_link_locked(
        self,
        source_id: str,
        target_id: str,
        link_type: LinkType = LinkType.REFERENCE,
        description: Optional[str] = None,
        bidirectional: bool = False,
        bidirectional_type: Optional[LinkType] = None,
    ) -> Tuple[Note, Optional[Note]]:
        """Create-link implementation; caller holds the relevant note lock(s).

        Args:
            source_id: ID of the source note
            target_id: ID of the target note
            link_type: Type of link from source to target
            description: Optional description of the link
            bidirectional: Whether to create a link in both directions
            bidirectional_type: Optional custom link type for the reverse direction
                If not provided, an appropriate inverse relation will be used

        Returns:
            Tuple of (source_note, target_note or None)
        """
        source_note = self.repository.get(source_id)
        if not source_note:
            raise ValueError(f"Source note with ID {source_id} not found")
        target_note = self.repository.get(target_id)
        if not target_note:
            raise ValueError(f"Target note with ID {target_id} not found")

        # Check if this link already exists before attempting to add it
        for link in source_note.links:
            if link.target_id == target_id and link.link_type == link_type:
                # Link already exists, no need to add it again
                if not bidirectional:
                    return source_note, None
                break
        else:
            # Only add the link if it doesn't exist
            source_note.add_link(target_id, link_type, description)
            source_note = self.repository.update(source_note)

        # If bidirectional, add link from target to source with appropriate semantics
        reverse_note = None
        if bidirectional:
            # If no explicit bidirectional type is provided, determine appropriate inverse
            if bidirectional_type is None:
                bidirectional_type = _INVERSE_LINK_TYPES.get(link_type, link_type)

            # Check if the reverse link already exists before adding it
            for link in target_note.links:
                if link.target_id == source_id and link.link_type == bidirectional_type:
                    # Reverse link already exists, no need to add it again
                    return source_note, target_note

            # Only add the reverse link if it doesn't exist
            target_note.add_link(source_id, bidirectional_type, description)
            reverse_note = self.repository.update(target_note)

        return source_note, reverse_note

    def remove_link(
        self,
        source_id: str,
        target_id: str,
        link_type: Optional[LinkType] = None,
        bidirectional: bool = False,
        bidirectional_type: Optional[LinkType] = None,
    ) -> Tuple[Note, Optional[Note]]:
        """Remove a link between notes (serialized per note against concurrent edits).

        Locks both endpoints in a stable order so a concurrent edit to either
        note can't interleave with this read-modify-write.
        """
        with self._note_locks_for(source_id, target_id):
            source_note = self.repository.get(source_id)
            if not source_note:
                raise ValueError(f"Source note with ID {source_id} not found")

            # Remove link from source to target
            source_note.remove_link(target_id, link_type)
            source_note = self.repository.update(source_note)

            # If bidirectional, remove link from target to source
            reverse_note = None
            if bidirectional:
                target_note = self.repository.get(target_id)
                if target_note:
                    if bidirectional_type is None and link_type is not None:
                        bidirectional_type = _INVERSE_LINK_TYPES.get(
                            link_type, link_type
                        )
                    target_note.remove_link(source_id, bidirectional_type)
                    reverse_note = self.repository.update(target_note)

            return source_note, reverse_note

    def get_linked_notes(self, note_id: str, direction: str = "outgoing") -> List[Note]:
        """Get notes linked to/from a note."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        return self.repository.find_linked_notes(note_id, direction)

    def _get_project_note(self, project_id: str) -> Note:
        """Backward-compatible alias for project routing validation."""
        return self._get_project_for_routing(project_id)

    def rebuild_index(self) -> Optional[Path]:
        """Rebuild the graph index from files."""
        return self.repository.rebuild_index()

    def check_consistency(self) -> Dict[str, Any]:
        """Report drift between the markdown files and the graph index (read-only)."""
        return self.repository.check_consistency()

    def export_note(self, note_id: str, format: str = "markdown") -> str:
        """Export a note in the specified format."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")

        if format.lower() == "markdown":
            return note.to_markdown()
        else:
            raise ValueError(f"Unsupported export format: {format}")

    def find_similar_notes(
        self, note_id: str, threshold: float = 0.5
    ) -> List[Tuple[Note, float]]:
        """Find notes similar to the given note.

        Similarity blends two signals so that notes about the same idea are found
        even when they share no tags or links yet (the common case for a freshly
        created note):

        * **Structural** — overlap of tags and *knowledge* links (PARA/GTD routing
          links to a note's area/project are excluded; every sibling shares those,
          so they carry no similarity signal).
        * **Lexical** — title+content word overlap, scored with a length-aware
          coefficient (see ``_lexical_overlap``) rather than raw Jaccard, so two
          notes that merely share a few common words do not clear the bar while
          notes with substantial, distinctive overlap do.

        The final score is ``max(structural, lexical_weight * lexical)`` so a
        strong structural match still ranks at full strength, while a note with no
        structural overlap can still surface on content alone. Lexical is weighted
        below 1.0 so a pure word-overlap match does not outrank a genuine
        tag/link relationship. Backwards compatible: same signature, same default
        threshold, same return shape (list of ``(Note, score)`` sorted desc).
        """
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")

        all_notes = self.repository.get_all()
        results = []

        note_tags = {tag.name for tag in note.tags}
        # Only genuine knowledge links count toward structural similarity. PARA
        # routing links (a note's reference to its area, and part_of/has_part to
        # its project/area) are shared by EVERY note in the same area/project, so
        # counting them gives unrelated notes a spurious structural floor — the
        # same scaffolding-dominates-the-graph problem seen in find_central_notes.
        note_links = self._knowledge_link_targets(note)
        note_tokens = self._content_tokens(note)

        incoming_notes = self.repository.find_linked_notes(note_id, "incoming")
        # Exclude the note's own project/area from incoming overlap for the same
        # reason (a parent project/area links to all of its children via has_part).
        routing_ids = {note.area_id, note.project_id} - {None}
        note_incoming = {n.id for n in incoming_notes if n.id not in routing_ids}

        # Lexical contribution is capped below a full structural match so a pure
        # word-overlap neighbour cannot outrank a real tag/link relationship.
        lexical_weight = 0.6

        for other_note in all_notes:
            if other_note.id == note_id:
                continue

            other_tags = {tag.name for tag in other_note.tags}
            tag_overlap = len(note_tags.intersection(other_tags))

            other_links = self._knowledge_link_targets(other_note)
            link_overlap = len(note_links.intersection(other_links))

            incoming_overlap = 1 if other_note.id in note_incoming else 0
            outgoing_overlap = 1 if other_note.id in note_links else 0

            # Structural similarity (unchanged weighting).
            total_possible = (
                max(len(note_tags), len(other_tags)) * 0.4
                + max(len(note_links), len(other_links)) * 0.2
                + 1 * 0.2  # Possible incoming link
                + 1 * 0.2  # Possible outgoing link
            )
            if total_possible == 0:
                structural = 0.0
            else:
                structural = (
                    (tag_overlap * 0.4)
                    + (link_overlap * 0.2)
                    + (incoming_overlap * 0.2)
                    + (outgoing_overlap * 0.2)
                ) / total_possible

            # Lexical similarity: length-aware overlap over title+content words.
            other_tokens = self._content_tokens(other_note)
            lexical = self._lexical_overlap(note_tokens, other_tokens)

            similarity = max(structural, lexical_weight * lexical)

            if similarity >= threshold:
                results.append((other_note, similarity))

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    # Link types that express PARA/GTD routing rather than a knowledge
    # relationship. These connect a note to its area/project (shared by every
    # sibling), so they carry no similarity signal and are excluded from the
    # structural overlap in find_similar_notes.
    _ROUTING_LINK_TYPES = frozenset(
        {LinkType.PART_OF, LinkType.HAS_PART, LinkType.BLOCKS, LinkType.BLOCKED_BY}
    )

    @classmethod
    def _knowledge_link_targets(cls, note: Note) -> Set[str]:
        """Return target ids of a note's genuine knowledge links.

        Excludes PARA/GTD routing links (part_of/has_part/blocks) and the note's
        own reference to its area, both of which are shared by all siblings and
        would otherwise inflate structural similarity between unrelated notes.
        """
        targets: Set[str] = set()
        for link in note.links:
            if link.link_type in cls._ROUTING_LINK_TYPES:
                continue
            # The area reference link uses REFERENCE type but points at the area.
            if note.area_id and link.target_id == note.area_id:
                continue
            targets.add(link.target_id)
        return targets

    # Lightweight English stopwords — enough to keep Jaccard overlap meaningful
    # without pulling in an NLP dependency.
    _SIMILARITY_STOPWORDS = frozenset(
        """
        a an the and or but if then else for to of in on at by with without from into
        over under again further is are was were be been being do does did doing have
        has had having this that these those it its as so than too very can will just
        not no nor only own same out up down off about above below i you he she we they
        them his her their our your my me us him
        """.split()
    )

    # Minimum shared content words before lexical overlap counts at all. Two
    # short notes sharing one or two common words should not register as similar;
    # real topical overlap shows several shared distinctive terms.
    _LEXICAL_MIN_SHARED = 3

    @classmethod
    def _lexical_overlap(cls, tokens_a: Set[str], tokens_b: Set[str]) -> float:
        """Length-aware lexical similarity in [0, 1] for two content-word sets.

        Plain Jaccard over-rewards tiny notes (a 6-word and 8-word note sharing 2
        common words score ~0.2 even when unrelated). Instead:

        * require at least ``_LEXICAL_MIN_SHARED`` shared words before scoring
          anything, so incidental one/two-word overlaps read as 0; and
        * normalise the shared count by the *smaller* token set (overlap
          coefficient), so a focused note that is largely a topical subset of a
          longer note scores high, while a couple of shared stopword-survivors in
          otherwise disjoint notes stay low.
        """
        if not tokens_a or not tokens_b:
            return 0.0
        shared = len(tokens_a & tokens_b)
        if shared < cls._LEXICAL_MIN_SHARED:
            return 0.0
        return shared / min(len(tokens_a), len(tokens_b))

    @classmethod
    def _content_tokens(cls, note: Note) -> Set[str]:
        """Return the set of lowercased content words from a note's title + body.

        Strips the rendered ``## Links`` section, wiki-link IDs, punctuation, very
        short tokens, and stopwords so the overlap reflects topical words.
        """
        import re

        title = note.title or ""
        body = note.content or ""
        # Drop the generated ## Links section so link IDs don't dominate overlap.
        body = body.split("## Links", 1)[0]
        text = f"{title} {body}".lower()
        tokens = re.findall(r"[a-z0-9]+", text)
        return {
            tok
            for tok in tokens
            if len(tok) >= 3 and tok not in cls._SIMILARITY_STOPWORDS
        }

    # ------------------------------------------------------------------
    # Action-item methods (PARA / GTD)
    # ------------------------------------------------------------------

    def create_task(
        self,
        title: str,
        content: str,
        status: NoteStatus = NoteStatus.INBOX,
        tags: Optional[List[str]] = None,
        project_id: Optional[str] = None,
        area_id: Optional[str] = None,
        due_date: Optional[datetime.date] = None,
        priority: Optional[int] = None,
        recurrence_rule: Optional[str] = None,
        estimated_minutes: Optional[int] = None,
        remind_at: Optional[datetime.date] = None,
        source: NoteSource = NoteSource.MANUAL,
    ) -> Note:
        """Create a task note.

        project_id is required. area_id is auto-filled from the project if not provided.
        """
        if not project_id:
            raise ValueError(
                "Tasks must be associated with a project (project_id required)"
            )
        project = self._get_project_for_routing(project_id)
        if project.area_id:
            if area_id and area_id != project.area_id:
                raise ValueError(
                    f"area_id {area_id} does not match project "
                    f"{project_id} area_id {project.area_id}"
                )
            area_id = project.area_id
        elif not area_id:
            raise ValueError(
                "Tasks must resolve to an area from the linked project or explicit area_id"
            )
        if area_id:
            self._get_area_for_routing(area_id)
        task = Note(
            title=title,
            content=content,
            note_type=NoteType.TASK,
            tags=[Tag(name=t) for t in (tags or [])],
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
        task = self._seed_routing_links(task, parent_id=project_id)
        task = self.repository.create(task)
        self._ensure_parent_has_part_link(project_id, task.id)
        return task

    def update_task(
        self,
        note_id: str,
        *,
        status: Any = _UNSET,
        project_id: Any = _UNSET,
        due_date: Any = _UNSET,
        remind_at: Any = _UNSET,
        priority: Any = _UNSET,
        estimated_minutes: Any = _UNSET,
        recurrence_rule: Any = _UNSET,
        tags: Any = _UNSET,
    ) -> Note:
        """Update task fields (serialized per note against concurrent edits).

        Locks the task plus its current and new project/area up front (a project
        reassignment also rewrites the project's HAS_PART link), in one stable
        order, so cross-note routing updates stay consistent and deadlock-free.
        """
        with self._locked_for_routing(note_id, project_id, _UNSET):
            return self._update_task_locked(
                note_id,
                status=status,
                project_id=project_id,
                due_date=due_date,
                remind_at=remind_at,
                priority=priority,
                estimated_minutes=estimated_minutes,
                recurrence_rule=recurrence_rule,
                tags=tags,
            )

    def _update_task_locked(
        self,
        note_id: str,
        *,
        status: Any = _UNSET,
        project_id: Any = _UNSET,
        due_date: Any = _UNSET,
        remind_at: Any = _UNSET,
        priority: Any = _UNSET,
        estimated_minutes: Any = _UNSET,
        recurrence_rule: Any = _UNSET,
        tags: Any = _UNSET,
    ) -> Note:
        """Update-task implementation; caller holds the per-note lock."""
        task = self.repository.get(note_id)
        if not task:
            raise ValueError(f"Note with ID {note_id} not found")
        if task.note_type != NoteType.TASK:
            raise ValueError(
                f"Note {note_id} is not a task (type: {task.note_type.value})"
            )

        previous_project_id = task.project_id
        if project_id is not _UNSET:
            if not project_id:
                raise ValueError(
                    "Tasks must be associated with a project (project_id required)"
                )
            project = self._get_project_for_routing(project_id)
            if not project.area_id:
                raise ValueError(
                    f"Project {project_id} does not have an area_id to inherit"
                )
            task.project_id = project_id
            task.area_id = project.area_id

        pending_updates = {
            "due_date": due_date,
            "remind_at": remind_at,
            "priority": priority,
            "estimated_minutes": estimated_minutes,
            "recurrence_rule": recurrence_rule,
            "tags": tags,
            "project_id": project_id,
        }
        if due_date is not _UNSET:
            task.due_date = due_date
        if remind_at is not _UNSET:
            task.remind_at = remind_at
        if priority is not _UNSET:
            task.priority = priority
        if estimated_minutes is not _UNSET:
            task.estimated_minutes = estimated_minutes
        if recurrence_rule is not _UNSET:
            task.recurrence_rule = recurrence_rule
        if tags is not _UNSET:
            task.tags = [Tag(name=tag) for tag in tags]

        if any(value is not _UNSET for value in pending_updates.values()):
            task.updated_at = datetime.datetime.now()
            task = self.repository.update(task)
            task = self._sync_part_of_link(
                task.id, previous_project_id, task.project_id
            )

        if status is not _UNSET:
            # Already holding this note's lock — call the unlocked impl directly
            # so we don't re-acquire the non-reentrant lock and deadlock.
            return self._update_task_status_locked(note_id, status)
        return task

    def update_task_status(self, note_id: str, new_status: NoteStatus) -> Note:
        """Update task status (serialized per note against concurrent edits)."""
        with self._note_lock(note_id):
            return self._update_task_status_locked(note_id, new_status)

    def _update_task_status_locked(
        self, note_id: str, new_status: NoteStatus
    ) -> Note:
        """Status-update implementation; caller holds the per-note lock.

        Spawns a new task when a recurring one is completed.
        """
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        if note.note_type != NoteType.TASK:
            raise ValueError(
                f"Note {note_id} is not a task (type: {note.note_type.value})"
            )
        note.status = new_status
        note.updated_at = datetime.datetime.now()
        updated = self.repository.update(note)
        if new_status == NoteStatus.DONE and note.recurrence_rule:
            self._spawn_recurring_task(updated)
        return updated

    def _spawn_recurring_task(self, done_note: Note) -> Note:
        """Create the next instance of a recurring task."""
        deltas = {
            "daily": datetime.timedelta(days=1),
            "weekly": datetime.timedelta(weeks=1),
            "monthly": datetime.timedelta(days=30),
            "quarterly": datetime.timedelta(days=91),
            "yearly": datetime.timedelta(days=365),
        }
        rule = (done_note.recurrence_rule or "").lower()
        delta = deltas.get(rule)
        next_due = (
            (done_note.due_date + delta) if (done_note.due_date and delta) else None
        )
        next_remind_at = (
            (done_note.remind_at + delta) if (done_note.remind_at and delta) else None
        )

        new_task = self.create_task(
            title=done_note.title,
            content=done_note.content,
            status=NoteStatus.READY,
            tags=[tag.name for tag in done_note.tags],
            project_id=done_note.project_id,
            area_id=done_note.area_id,
            due_date=next_due,
            priority=done_note.priority,
            recurrence_rule=done_note.recurrence_rule,
            estimated_minutes=done_note.estimated_minutes,
            remind_at=next_remind_at,
            source=NoteSource.RECURRING,
        )

        # Link back to completed instance for audit trail
        new_task.add_link(done_note.id, LinkType.REFERENCE, "recurring from")
        return self.repository.update(new_task)

    def get_tasks(
        self,
        status: Optional[NoteStatus] = None,
        project_id: Optional[str] = None,
        due_date_before: Optional[datetime.date] = None,
        due_date_after: Optional[datetime.date] = None,
        priority: Optional[int] = None,
        limit: int = 50,
    ) -> List[Note]:
        """Query tasks with optional filters."""
        kwargs: Dict[str, Any] = {"note_type": NoteType.TASK}
        if status is not None:
            kwargs["status"] = status
        if due_date_before is not None:
            kwargs["due_date_before"] = due_date_before
        if due_date_after is not None:
            kwargs["due_date_after"] = due_date_after
        if priority is not None:
            kwargs["priority"] = priority
        tasks = self.repository.search(**kwargs)
        if status is None:
            tasks = [
                task
                for task in tasks
                if task.status not in {NoteStatus.DONE, NoteStatus.ARCHIVED}
            ]
        if project_id:
            project_task_ids = {
                n.id
                for n in self.repository.find_linked_notes(project_id, "outgoing")
                if n.note_type == NoteType.TASK
            }
            tasks = [t for t in tasks if t.id in project_task_ids]
        return tasks[:limit]

    def get_todays_tasks(self, include_overdue: bool = True) -> List[Note]:
        """Return tasks due today (and optionally overdue), sorted by priority then due date."""
        today = datetime.date.today()
        cutoff = today if include_overdue else today
        tasks = self.repository.search(
            note_type=NoteType.TASK,
            due_date_before=cutoff,
        )
        active_statuses = {
            NoteStatus.INBOX,
            NoteStatus.READY,
            NoteStatus.ACTIVE,
            NoteStatus.WAITING,
            NoteStatus.SCHEDULED,
        }
        tasks = [t for t in tasks if t.status in active_statuses]
        tasks.sort(
            key=lambda t: (
                -(t.priority or 0),
                t.due_date or datetime.date.max,
            )
        )
        return tasks

    def create_project_note(
        self,
        title: str,
        content: str,
        outcome: Optional[str] = None,
        deadline: Optional[datetime.date] = None,
        area_id: Optional[str] = None,
        project_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source: NoteSource = NoteSource.MANUAL,
    ) -> Note:
        """Create a PROJECT-type note.

        Top-level projects require an ``area_id``. Subprojects pass a parent project
        through ``project_id`` and inherit that parent project's ``area_id``.
        """
        if project_id:
            parent_project = self._get_project_for_routing(project_id)
            if not parent_project.area_id:
                raise ValueError(
                    f"Project {project_id} does not have an area_id to inherit"
                )
            if area_id and area_id != parent_project.area_id:
                raise ValueError(
                    f"area_id {area_id} does not match project "
                    f"{project_id} area_id {parent_project.area_id}"
                )
            area_id = parent_project.area_id
        if not area_id:
            raise ValueError(
                "Projects must be associated with an area (area_id required)"
            )
        self._get_area_for_routing(area_id)
        metadata: Dict[str, Any] = {}
        if outcome:
            metadata["outcome"] = outcome
        project = Note(
            title=title,
            content=content,
            note_type=NoteType.PROJECT,
            tags=[Tag(name=t) for t in (tags or [])],
            metadata=metadata,
            due_date=deadline,
            project_id=project_id,
            area_id=area_id,
            source=source,
        )
        project = self._seed_routing_links(project, parent_id=area_id)
        if project_id:
            project.add_link(project_id, LinkType.PART_OF)
        project = self.repository.create(project)
        self._ensure_parent_has_part_link(area_id, project.id)
        self._ensure_parent_has_part_link(project_id, project.id)
        return project

    def get_parent_project(self, project_id: str) -> Optional[Note]:
        """Return the direct parent project for a project, if any."""
        project = self._get_project_for_routing(project_id)
        if not project.project_id:
            return None
        parent = self.repository.get(project.project_id)
        if parent and parent.note_type == NoteType.PROJECT:
            return parent
        return None

    def get_subprojects(self, project_id: str) -> List[Note]:
        """Return direct child projects routed to the given project."""
        self._get_project_for_routing(project_id)
        notes = self.repository.search(project_id=project_id)
        subprojects = [
            note
            for note in notes
            if note.id != project_id and note.note_type == NoteType.PROJECT
        ]
        return sorted(subprojects, key=lambda note: note.title.lower())

    def get_project_tasks(
        self, project_id: str, status: Optional[NoteStatus] = None
    ) -> List[Note]:
        """Return all tasks linked PART_OF a project."""
        linked = self.repository.find_linked_notes(project_id, "outgoing")
        tasks = [n for n in linked if n.note_type == NoteType.TASK]
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_project_notes(self, project_id: str) -> List[Note]:
        """Return non-task notes explicitly routed to a project."""
        self._get_project_for_routing(project_id)
        notes = self.repository.search(project_id=project_id)
        notes = [
            note
            for note in notes
            if note.id != project_id
            and note.note_type not in {NoteType.TASK, NoteType.PROJECT}
        ]
        return sorted(notes, key=lambda note: note.title.lower())

    def get_linked_projects(self, project_id: str) -> List[Note]:
        """Return directly connected projects using PART_OF/HAS_PART relationships."""
        project = self._get_project_for_routing(project_id)
        linked_projects: Dict[str, Note] = {}

        for link in project.links:
            if link.link_type not in {LinkType.PART_OF, LinkType.HAS_PART}:
                continue
            target = self.repository.get(link.target_id)
            if target and target.note_type == NoteType.PROJECT and target.id != project.id:
                linked_projects[target.id] = target

        for incoming in self.repository.find_linked_notes(project_id, "incoming"):
            if incoming.note_type != NoteType.PROJECT or incoming.id == project.id:
                continue
            if any(
                link.target_id == project_id
                and link.link_type in {LinkType.PART_OF, LinkType.HAS_PART}
                for link in incoming.links
            ):
                linked_projects[incoming.id] = incoming

        return sorted(linked_projects.values(), key=lambda note: note.title.lower())

    def create_area_note(
        self,
        title: str,
        content: str,
        cadence: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Note:
        """Create an AREA-type note (ongoing responsibility)."""
        metadata: Dict[str, Any] = {}
        if cadence:
            metadata["cadence"] = cadence
        return self.create_note(
            title=title,
            content=content,
            note_type=NoteType.AREA,
            tags=tags,
            metadata=metadata,
        )

    def get_reminders(self, limit: int = 20) -> List[Note]:
        """Return notes/tasks with remind_at <= today, sorted by remind_at ASC."""
        today = datetime.date.today()
        notes = self.repository.search(remind_at_before=today)
        notes.sort(key=lambda n: (n.remind_at or datetime.date.min))
        return notes[:limit]
