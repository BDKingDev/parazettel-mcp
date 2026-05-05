"""Service layer for Zettelkasten operations."""

import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

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
        """Update the parent note once so it reflects the child relationship."""
        if not parent_id:
            return
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
        note, _ = self.create_link(note.id, area_id, LinkType.REFERENCE)
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
            note, _ = self.create_link(note.id, parent_id, LinkType.PART_OF, bidirectional=True)
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
        """Update an existing note."""
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
        """Delete a note."""
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
        """Add a tag to a note."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")
        note.add_tag(tag)
        return self.repository.update(note)

    def remove_tag_from_note(self, note_id: str, tag: str) -> Note:
        """Remove a tag from a note."""
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
        """Create a link between notes with proper bidirectional semantics.

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
        """Remove a link between notes."""
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
                    bidirectional_type = _INVERSE_LINK_TYPES.get(link_type, link_type)
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
        """Rebuild the database index from files."""
        self.repository.rebuild_index()
        return self.repository.last_rebuild_backup_path

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
        """Find notes similar to the given note based on shared tags and links."""
        note = self.repository.get(note_id)
        if not note:
            raise ValueError(f"Note with ID {note_id} not found")

        # Get all notes
        all_notes = self.repository.get_all()
        results = []

        # Set of this note's tags and links
        note_tags = {tag.name for tag in note.tags}
        note_links = {link.target_id for link in note.links}

        # Add notes linked to this note
        incoming_notes = self.repository.find_linked_notes(note_id, "incoming")
        note_incoming = {n.id for n in incoming_notes}

        # For each note, calculate similarity
        for other_note in all_notes:
            if other_note.id == note_id:
                continue

            # Calculate tag overlap
            other_tags = {tag.name for tag in other_note.tags}
            tag_overlap = len(note_tags.intersection(other_tags))

            # Calculate link overlap (outgoing)
            other_links = {link.target_id for link in other_note.links}
            link_overlap = len(note_links.intersection(other_links))

            # Check if other note links to this note
            incoming_overlap = 1 if other_note.id in note_incoming else 0

            # Check if this note links to other note
            outgoing_overlap = 1 if other_note.id in note_links else 0

            # Calculate similarity score
            # Weight: 40% tags, 20% outgoing links, 20% incoming links, 20% direct connections
            total_possible = (
                max(len(note_tags), len(other_tags)) * 0.4
                + max(len(note_links), len(other_links)) * 0.2
                + 1 * 0.2  # Possible incoming link
                + 1 * 0.2  # Possible outgoing link
            )

            # Avoid division by zero
            if total_possible == 0:
                similarity = 0.0
            else:
                similarity = (
                    (tag_overlap * 0.4)
                    + (link_overlap * 0.2)
                    + (incoming_overlap * 0.2)
                    + (outgoing_overlap * 0.2)
                ) / total_possible

            if similarity >= threshold:
                results.append((other_note, similarity))

        # Sort by similarity (descending)
        results.sort(key=lambda x: x[1], reverse=True)
        return results

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
        """Update task fields, including project reassignment before status changes."""
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
            return self.update_task_status(note_id, status)
        return task

    def update_task_status(self, note_id: str, new_status: NoteStatus) -> Note:
        """Update the status of a task. Spawns a new task when a recurring one is completed."""
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
