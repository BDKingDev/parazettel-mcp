"""MCP server implementation for the Zettelkasten."""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy import exc as sqlalchemy_exc

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import (
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
    Tag,
)
from parazettel_mcp.services.search_service import SearchService
from parazettel_mcp.services.zettel_service import ZettelService

logger = logging.getLogger(__name__)


class ZettelkastenMcpServer:
    """MCP server for Zettelkasten."""

    def __init__(self):
        """Initialize the MCP server."""
        self.mcp = FastMCP(config.server_name, version=config.server_version)
        # Services
        self.zettel_service = ZettelService()
        self.search_service = SearchService(self.zettel_service)
        # Initialize services
        self.initialize()
        # Register tools
        self._register_tools()
        self._register_resources()
        self._register_prompts()

    def initialize(self) -> None:
        """Initialize services."""
        self.zettel_service.initialize()
        self.search_service.initialize()
        logger.info("Zettelkasten MCP server initialized")

    def close(self) -> None:
        """Release resources held by the MCP server."""
        self.zettel_service.close()

    def format_error_response(self, error: Exception) -> str:
        """Format an error response in a consistent way.

        Args:
            error: The exception that occurred

        Returns:
            Formatted error message with appropriate level of detail
        """
        # Generate a unique error ID for traceability in logs
        error_id = str(uuid.uuid4())[:8]

        if isinstance(error, ValueError):
            # Domain validation errors - typically safe to show to users
            logger.error(f"Validation error [{error_id}]: {str(error)}")
            return f"Error: {str(error)}"
        elif isinstance(error, (IOError, OSError)):
            # File system errors - don't expose paths or detailed error messages
            logger.error(f"File system error [{error_id}]: {str(error)}", exc_info=True)
            # return f"Unable to access the requested resource. Error ID: {error_id}"
            return f"Error: {str(error)}"
        else:
            # Unexpected errors - log with full stack trace but return generic message
            logger.error(f"Unexpected error [{error_id}]: {str(error)}", exc_info=True)
            # return f"An unexpected error occurred. Error ID: {error_id}"
            return f"Error: {str(error)}"

    @staticmethod
    def _format_note_result(note: Note) -> str:
        """Render a note using the standard MCP note output."""
        result = f"ID: {note.id}\n"
        result += f"Type: {note.note_type.value}\n"
        result += f"Created: {note.created_at.isoformat()}\n"
        result += f"Updated: {note.updated_at.isoformat()}\n"
        if note.project_id:
            result += f"Project ID: {note.project_id}\n"
        if note.area_id:
            result += f"Area ID: {note.area_id}\n"
        if note.tags:
            result += f"Tags: {', '.join(tag.name for tag in note.tags)}\n"
        result += f"\n{note.content}\n"
        return result

    def _resolve_note_identifier(self, identifier: str) -> Optional[Note]:
        """Resolve a note by ID first, then by title."""
        normalized = str(identifier).strip()
        if not normalized:
            return None
        note = self.zettel_service.get_note(normalized)
        if note:
            return note
        return self.zettel_service.get_note_by_title(normalized)

    @staticmethod
    def _normalize_identifier_list(identifiers: List[str]) -> List[str]:
        """Strip empty values and de-duplicate while preserving order."""
        seen = set()
        normalized: List[str] = []
        for identifier in identifiers:
            value = str(identifier).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _get_project_preview_tasks(
        tasks: List[Note], limit: int = 5
    ) -> List[Note]:
        """Return the most actionable project tasks for summary views."""
        actionable_statuses = {NoteStatus.ACTIVE, NoteStatus.READY}
        status_order = {NoteStatus.ACTIVE: 0, NoteStatus.READY: 1}
        far_future = datetime.max.date()
        actionable = [task for task in tasks if task.status in actionable_statuses]
        return sorted(
            actionable,
            key=lambda task: (
                status_order.get(task.status, 99),
                task.due_date or far_future,
                -(task.priority or 0),
                task.title.lower(),
            ),
        )[:limit]

    @staticmethod
    def _format_project_preview_task(task: Note) -> str:
        """Render one actionable task in a compact, parseable summary format."""
        line = f"- [{task.status.value if task.status else 'none'}] {task.title} (ID: {task.id})"
        details = []
        if task.priority:
            details.append(f"P{task.priority}")
        if task.due_date:
            details.append(f"due {task.due_date}")
        if details:
            line += " - " + ", ".join(details)
        return line

    def _register_tools(self) -> None:
        """Register MCP tools."""

        # Create a new note
        @self.mcp.tool(name="pzk_create_note")
        def pzk_create_note(
            title: str,
            content: str,
            note_type: str = "permanent",
            tags: Optional[str] = None,
            source: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            area_id: Optional[str] = None,
        ) -> str:
            """Create a new Zettelkasten note.
            Args:
                title: The title of the note
                content: The main content of the note
                note_type: Type of note. Knowledge types: fleeting, literature, permanent,
                    structure, hub. Action-item types: task, project, area.
                    For tasks prefer pzk_create_task which exposes task-specific fields.
                tags: Comma-separated list of tags (optional)
                source: Origin of the note. Required for all note types except area.
                status: Optional workflow status such as inbox, evergreen, or archived.
                project_id: Optional project to route the note under; inherits the project's area.
                area_id: ID of the area this note belongs to when project_id is not provided.
            """
            try:
                # Convert note_type string to enum
                try:
                    note_type_enum = NoteType(note_type.lower())
                except ValueError:
                    return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                # Convert tags string to list
                tag_list = []
                if tags:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                note_source = NoteSource.MANUAL
                if source:
                    try:
                        note_source = NoteSource(source.lower())
                    except ValueError:
                        return (
                            f"Invalid source: {source}. "
                            f"Valid: {', '.join(s.value for s in NoteSource)}"
                        )
                elif note_type_enum != NoteType.AREA:
                    return (
                        "source is required for all note types except area. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )

                note_status = None
                if status is not None:
                    normalized_status = status.strip().lower()
                    if normalized_status:
                        try:
                            note_status = NoteStatus(normalized_status)
                        except ValueError:
                            return (
                                f"Invalid status: {status}. "
                                f"Valid: {', '.join(s.value for s in NoteStatus)}"
                            )

                resolved_area_id = area_id
                if note_type_enum == NoteType.AREA:
                    if project_id:
                        return "Area notes cannot specify project_id."
                    if area_id:
                        return (
                            "Area notes assign their own area_id automatically. "
                            "Do not pass area_id."
                        )
                else:
                    if not project_id and not area_id:
                        return (
                            "area_id or project_id is required for all note types "
                            "except area."
                        )
                    if project_id:
                        project = self.zettel_service.get_note(project_id)
                        if not project or project.note_type != NoteType.PROJECT:
                            return (
                                f"project_id {project_id} is not a valid project note."
                            )
                        if not project.area_id:
                            return (
                                f"project_id {project_id} does not have an area_id."
                            )
                        if area_id and area_id != project.area_id:
                            return (
                                f"area_id {area_id} does not match project "
                                f"{project_id} area_id {project.area_id}."
                            )
                        resolved_area_id = project.area_id
                    if resolved_area_id:
                        area = self.zettel_service.get_note(resolved_area_id)
                        if not area or area.note_type != NoteType.AREA:
                            return (
                                f"area_id {resolved_area_id} is not a valid area note."
                            )

                # Create the note
                note = self.zettel_service.create_note(
                    title=title,
                    content=content,
                    note_type=note_type_enum,
                    tags=tag_list,
                    source=note_source,
                    status=note_status,
                    project_id=project_id,
                    area_id=resolved_area_id,
                )
                return f"Note created successfully with ID: {note.id}"
            except Exception as e:
                return self.format_error_response(e)

        # Get a note by ID or title
        @self.mcp.tool(name="pzk_get_note")
        def pzk_get_note(identifier: str) -> str:
            """Retrieve a note by ID or title.
            Args:
                identifier: The ID or title of the note
            """
            try:
                identifier = str(identifier)
                note = self._resolve_note_identifier(identifier)
                if not note:
                    return f"Note not found: {identifier}"
                return self._format_note_result(note)
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_notes")
        def pzk_get_notes(identifiers: List[str]) -> str:
            """Retrieve multiple notes by ID or title in one call.
            Args:
                identifiers: Note IDs or titles to retrieve
            """
            try:
                normalized = self._normalize_identifier_list(identifiers)
                if not normalized:
                    return "Provide at least one note identifier."

                notes: List[Note] = []
                seen_note_ids = set()
                missing: List[str] = []
                for identifier in normalized:
                    note = self._resolve_note_identifier(identifier)
                    if note:
                        if note.id not in seen_note_ids:
                            notes.append(note)
                            seen_note_ids.add(note.id)
                    else:
                        missing.append(identifier)

                if not notes:
                    out = "No notes found for the provided identifiers."
                    if missing:
                        out += "\n\nMissing identifiers:\n"
                        out += "\n".join(f"- {identifier}" for identifier in missing)
                    return out

                out = f"Notes retrieved ({len(notes)}/{len(normalized)}):\n\n"
                out += "\n---\n\n".join(
                    self._format_note_result(note) for note in notes
                )
                if missing:
                    out += "\n\nMissing identifiers:\n"
                    out += "\n".join(f"- {identifier}" for identifier in missing)
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_notes_by_tag")
        def pzk_get_notes_by_tag(tag: str, limit: int = 50) -> str:
            """Retrieve notes with an exact tag match.
            Args:
                tag: Tag name to retrieve
                limit: Maximum results
            """
            try:
                normalized_tag = str(tag).strip()
                if not normalized_tag:
                    return "Provide a tag name."
                if limit <= 0:
                    return "Limit must be greater than 0."

                notes = self.zettel_service.get_notes_by_tag(normalized_tag)
                notes = notes[:limit]
                if not notes:
                    return f"No notes found with tag '{normalized_tag}'."

                out = f"Notes tagged '{normalized_tag}' ({len(notes)}):\n\n"
                out += "\n---\n\n".join(
                    self._format_note_result(note) for note in notes
                )
                return out
            except Exception as e:
                return self.format_error_response(e)

        # Update a note
        @self.mcp.tool(name="pzk_update_note")
        def pzk_update_note(
            note_id: str,
            title: Optional[str] = None,
            content: Optional[str] = None,
            note_type: Optional[str] = None,
            tags: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            area_id: Optional[str] = None,
        ) -> str:
            """Update an existing note.
            Args:
                note_id: The ID of the note to update
                title: New title (optional)
                content: New content (optional)
                note_type: New note type (optional)
                tags: New comma-separated list of tags (optional)
                status: New workflow status (optional). Pass empty string to clear it.
                project_id: New project routing (optional). Pass empty string to clear it.
                parent_project_id: New parent project / project routing (optional). Pass empty string to clear it.
                area_id: New area routing (optional). Pass empty string to clear it.
            """
            try:
                # Get the note
                note = self.zettel_service.get_note(str(note_id))
                if not note:
                    return f"Note not found: {note_id}"

                # Convert note_type string to enum if provided
                note_type_enum = None
                if note_type:
                    try:
                        note_type_enum = NoteType(note_type.lower())
                    except ValueError:
                        return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                # Convert tags string to list if provided
                tag_list = None
                if tags is not None:  # Allow empty string to clear tags
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                update_kwargs = {
                    "note_id": note_id,
                    "title": title,
                    "content": content,
                    "note_type": note_type_enum,
                    "tags": tag_list,
                }
                if status is not None:
                    normalized_status = status.strip().lower()
                    if normalized_status:
                        try:
                            update_kwargs["status"] = NoteStatus(normalized_status)
                        except ValueError:
                            return (
                                f"Invalid status: {status}. "
                                f"Valid: {', '.join(s.value for s in NoteStatus)}"
                            )
                    else:
                        update_kwargs["status"] = None
                if parent_project_id is not None:
                    normalized_parent_project_id = parent_project_id.strip()
                    if project_id is not None:
                        normalized_project_id = project_id.strip()
                        if normalized_project_id != normalized_parent_project_id:
                            return (
                                "project_id and parent_project_id must match when "
                                "both are provided."
                            )
                    update_kwargs["project_id"] = (
                        normalized_parent_project_id or None
                    )
                if project_id is not None:
                    normalized_project_id = project_id.strip()
                    update_kwargs.setdefault(
                        "project_id", normalized_project_id or None
                    )
                if area_id is not None:
                    normalized_area_id = area_id.strip()
                    update_kwargs["area_id"] = normalized_area_id or None

                # Update the note
                updated_note = self.zettel_service.update_note(**update_kwargs)
                return f"Note updated successfully: {updated_note.id}"
            except Exception as e:
                return self.format_error_response(e)

        # Delete a note
        @self.mcp.tool(name="pzk_delete_note")
        def pzk_delete_note(note_id: str) -> str:
            """Delete a note.
            Args:
                note_id: The ID of the note to delete
            """
            try:
                # Check if note exists
                note = self.zettel_service.get_note(note_id)
                if not note:
                    return f"Note not found: {note_id}"

                # Delete the note
                self.zettel_service.delete_note(str(note_id))
                return f"Note deleted successfully: {note_id}"
            except Exception as e:
                return self.format_error_response(e)

        # Add a link between notes
        @self.mcp.tool(name="pzk_create_link")
        def pzk_create_link(
            source_id: str,
            target_id: str,
            link_type: str = "reference",
            description: Optional[str] = None,
            bidirectional: bool = False,
        ) -> str:
            """Create a link between two notes.
            Args:
                source_id: ID of the source note
                target_id: ID of the target note
                link_type: Type of link (reference, extends, refines, contradicts, questions, supports, related)
                description: Optional description of the link
                bidirectional: Whether to create a link in both directions
            """
            try:
                # Convert link_type string to enum
                try:
                    source_id_str = str(source_id)
                    target_id_str = str(target_id)
                    link_type_enum = LinkType(link_type.lower())
                except ValueError:
                    return f"Invalid link type: {link_type}. Valid types are: {', '.join(t.value for t in LinkType)}"

                # Create the link
                source_note, target_note = self.zettel_service.create_link(
                    source_id=source_id,
                    target_id=target_id,
                    link_type=link_type_enum,
                    description=description,
                    bidirectional=bidirectional,
                )
                if bidirectional:
                    return f"Bidirectional link created between {source_id} and {target_id}"
                else:
                    return f"Link created from {source_id} to {target_id}"
            except (Exception, sqlalchemy_exc.IntegrityError) as e:
                if "UNIQUE constraint failed" in str(e):
                    return f"A link of this type already exists between these notes. Try a different link type."
                return self.format_error_response(e)

        self.pzk_create_link = pzk_create_link

        # Remove a link between notes
        @self.mcp.tool(name="pzk_remove_link")
        def pzk_remove_link(
            source_id: str, target_id: str, bidirectional: bool = False
        ) -> str:
            """Remove a link between two notes.
            Args:
                source_id: ID of the source note
                target_id: ID of the target note
                bidirectional: Whether to remove the link in both directions
            """
            try:
                # Remove the link
                source_note, target_note = self.zettel_service.remove_link(
                    source_id=str(source_id),
                    target_id=str(target_id),
                    bidirectional=bidirectional,
                )
                if bidirectional:
                    return f"Bidirectional link removed between {source_id} and {target_id}"
                else:
                    return f"Link removed from {source_id} to {target_id}"
            except Exception as e:
                return self.format_error_response(e)

        # Search for notes
        @self.mcp.tool(name="pzk_search_notes")
        def pzk_search_notes(
            query: Optional[str] = None,
            tags: Optional[str] = None,
            note_type: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            area_id: Optional[str] = None,
            limit: int = 10,
        ) -> str:
            """Search for notes by text, tags, type, status, or PARA routing fields.
            Args:
                query: Text to search for in titles and content
                tags: Comma-separated list of tags to filter by
                note_type: Type of note to filter by
                status: Filter by workflow status
                project_id: Filter to notes routed to this project
                area_id: Filter to notes routed to this area
                limit: Maximum number of results to return
            """
            try:
                # Convert tags string to list if provided
                tag_list = None
                if tags:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                # Convert note_type string to enum if provided
                note_type_enum = None
                if note_type:
                    try:
                        note_type_enum = NoteType(note_type.lower())
                    except ValueError:
                        return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                status_enum = None
                if status:
                    try:
                        status_enum = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"

                # Perform search
                results = self.search_service.search_combined(
                    text=query,
                    tags=tag_list,
                    note_type=note_type_enum,
                    status=status_enum,
                    project_id=project_id,
                    area_id=area_id,
                )

                # Limit results
                results = results[:limit]
                if not results:
                    return "No matching notes found."

                # Format results
                output = f"Found {len(results)} matching notes:\n\n"
                for i, result in enumerate(results, 1):
                    note = result.note
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    output += f"   Created: {note.created_at.strftime('%Y-%m-%d')}\n"
                    # Add a snippet of content (first 150 chars)
                    content_preview = note.content[:150].replace("\n", " ")
                    if len(note.content) > 150:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Get linked notes
        @self.mcp.tool(name="pzk_get_linked_notes")
        def pzk_get_linked_notes(note_id: str, direction: str = "both") -> str:
            """Get notes linked to/from a note.
            Args:
                note_id: ID of the note
                direction: Direction of links (outgoing, incoming, both)
            """
            try:
                if direction not in ["outgoing", "incoming", "both"]:
                    return f"Invalid direction: {direction}. Use 'outgoing', 'incoming', or 'both'."
                # Get linked notes
                linked_notes = self.zettel_service.get_linked_notes(
                    str(note_id), direction
                )
                if not linked_notes:
                    return f"No {direction} links found for note {note_id}."
                # Format results
                output = f"Found {len(linked_notes)} {direction} linked notes for {note_id}:\n\n"
                for i, note in enumerate(linked_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Try to determine link type
                    if direction in ["outgoing", "both"]:
                        # Check source note's outgoing links
                        source_note = self.zettel_service.get_note(str(note_id))
                        if source_note:
                            for link in source_note.links:
                                if str(link.target_id) == str(
                                    note.id
                                ):  # Explicit string conversion for comparison
                                    output += f"   Link type: {link.link_type.value}\n"
                                    if link.description:
                                        output += (
                                            f"   Description: {link.description}\n"
                                        )
                                    break
                    if direction in ["incoming", "both"]:
                        # Check target note's outgoing links
                        for link in note.links:
                            if str(link.target_id) == str(
                                note_id
                            ):  # Explicit string conversion for comparison
                                output += (
                                    f"   Incoming link type: {link.link_type.value}\n"
                                )
                                if link.description:
                                    output += f"   Description: {link.description}\n"
                                break
                    output += "\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        self.pzk_get_linked_notes = pzk_get_linked_notes

        # Get all tags
        @self.mcp.tool(name="pzk_get_all_tags")
        def pzk_get_all_tags() -> str:
            """Get all tags in the Zettelkasten."""
            try:
                tags = self.zettel_service.get_all_tags()
                if not tags:
                    return "No tags found in the Zettelkasten."

                # Format results
                output = f"Found {len(tags)} tags:\n\n"
                # Sort alphabetically
                tags.sort(key=lambda t: t.name.lower())
                for i, tag in enumerate(tags, 1):
                    output += f"{i}. {tag.name}\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find similar notes
        @self.mcp.tool(name="pzk_find_similar_notes")
        def pzk_find_similar_notes(
            note_id: str, threshold: float = 0.3, limit: int = 5
        ) -> str:
            """Find notes similar to a given note.
            Args:
                note_id: ID of the reference note
                threshold: Similarity threshold (0.0-1.0)
                limit: Maximum number of results to return
            """
            try:
                # Get similar notes
                similar_notes = self.zettel_service.find_similar_notes(
                    str(note_id), threshold
                )
                # Limit results
                similar_notes = similar_notes[:limit]
                if not similar_notes:
                    return f"No similar notes found for {note_id} with threshold {threshold}."

                # Format results
                output = f"Found {len(similar_notes)} similar notes for {note_id}:\n\n"
                for i, (note, similarity) in enumerate(similar_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   Similarity: {similarity:.2f}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find central notes
        @self.mcp.tool(name="pzk_find_central_notes")
        def pzk_find_central_notes(limit: int = 10) -> str:
            """Find notes with the most connections (incoming + outgoing links).
            Notes are ranked by their total number of connections, determining
            their centrality in the knowledge network. Due to database constraints,
            only one link of each type is counted between any pair of notes.

            Args:
                limit: Maximum number of results to return (default: 10)
            """
            try:
                # Get central notes
                central_notes = self.search_service.find_central_notes(limit)
                if not central_notes:
                    return "No notes found with connections."

                # Format results
                output = "Central notes in the Zettelkasten (most connected):\n\n"
                for i, (note, connection_count) in enumerate(central_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   Connections: {connection_count}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find orphaned notes
        @self.mcp.tool(name="pzk_find_orphaned_notes")
        def pzk_find_orphaned_notes() -> str:
            """Find notes with no connections to other notes."""
            try:
                # Get orphaned notes
                orphans = self.search_service.find_orphaned_notes()
                if not orphans:
                    return "No orphaned notes found."

                # Format results
                output = f"Found {len(orphans)} orphaned notes:\n\n"
                for i, note in enumerate(orphans, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # List notes by date range
        @self.mcp.tool(name="pzk_list_notes_by_date")
        def pzk_list_notes_by_date(
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
            use_updated: bool = False,
            limit: int = 10,
        ) -> str:
            """List notes created or updated within a date range.
            Args:
                start_date: Start date in ISO format (YYYY-MM-DD)
                end_date: End date in ISO format (YYYY-MM-DD)
                use_updated: Whether to use updated_at instead of created_at
                limit: Maximum number of results to return
            """
            try:
                # Parse dates
                start_datetime = None
                if start_date:
                    start_datetime = datetime.fromisoformat(f"{start_date}T00:00:00")
                end_datetime = None
                if end_date:
                    end_datetime = datetime.fromisoformat(f"{end_date}T23:59:59")

                # Get notes
                notes = self.search_service.find_notes_by_date_range(
                    start_date=start_datetime,
                    end_date=end_datetime,
                    use_updated=use_updated,
                )

                # Limit results
                notes = notes[:limit]
                if not notes:
                    date_type = "updated" if use_updated else "created"
                    date_range = ""
                    if start_date and end_date:
                        date_range = f" between {start_date} and {end_date}"
                    elif start_date:
                        date_range = f" after {start_date}"
                    elif end_date:
                        date_range = f" before {end_date}"
                    return f"No notes found {date_type}{date_range}."

                # Format results
                date_type = "updated" if use_updated else "created"
                output = f"Notes {date_type}"
                if start_date or end_date:
                    if start_date and end_date:
                        output += f" between {start_date} and {end_date}"
                    elif start_date:
                        output += f" after {start_date}"
                    elif end_date:
                        output += f" before {end_date}"
                output += f" (showing {len(notes)} results):\n\n"
                for i, note in enumerate(notes, 1):
                    date = note.updated_at if use_updated else note.created_at
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   {date_type.capitalize()}: {date.strftime('%Y-%m-%d %H:%M')}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except ValueError as e:
                # Special handling for date parsing errors
                logger.error(f"Date parsing error: {str(e)}")
                return f"Error parsing date: {str(e)}"
            except Exception as e:
                return self.format_error_response(e)

        # Rebuild the index
        @self.mcp.tool(name="pzk_rebuild_index")
        def pzk_rebuild_index() -> str:
            """Rebuild the database index from files."""
            try:
                # Get count before rebuild
                note_count_before = len(self.zettel_service.get_all_notes())

                # Perform the rebuild
                backup_path = self.zettel_service.rebuild_index()

                # Get count after rebuild
                note_count_after = len(self.zettel_service.get_all_notes())
                backup_message = (
                    f"Backup created: {backup_path}\n"
                    if backup_path
                    else "Backup created: none (database file did not exist)\n"
                )

                # Return a detailed success message
                return (
                    f"Database index rebuilt successfully.\n"
                    f"{backup_message}"
                    f"Notes processed: {note_count_after}\n"
                    f"Change in note count: {note_count_after - note_count_before}"
                )
            except Exception as e:
                # Provide a detailed error message
                logger.error(f"Failed to rebuild index: {e}", exc_info=True)
                return self.format_error_response(e)

        # ----------------------------------------------------------------
        # Action-item tools (PARA / GTD)
        # ----------------------------------------------------------------

        @self.mcp.tool(name="pzk_create_task")
        def pzk_create_task(
            title: str,
            content: str,
            project_id: str = "",
            status: str = "inbox",
            tags: Optional[str] = None,
            area_id: Optional[str] = None,
            due_date: Optional[str] = None,
            remind_at: Optional[str] = None,
            priority: Optional[int] = None,
            recurrence_rule: Optional[str] = None,
            estimated_minutes: Optional[int] = None,
            source: str = "manual",
            context: Optional[str] = None,
            energy_level: Optional[str] = None,
        ) -> str:
            """Create a task note. Tasks must belong to a project.
            Args:
                title: Task title
                content: Task description
                project_id: ID of the project this task belongs to (required)
                status: inbox, ready, scheduled, active, waiting, someday, done, cancelled
                tags: Comma-separated tags
                area_id: Override area (auto-filled from project if omitted)
                due_date: Due date YYYY-MM-DD
                remind_at: Reminder date YYYY-MM-DD
                priority: 1 (low) to 4 (critical)
                recurrence_rule: daily, weekly, monthly, quarterly, yearly
                estimated_minutes: Estimated effort in minutes
                source: manual, inbox, email, meeting, voice, transcript, book, article, chat, web, pdf, recurring
                context: GTD context — auto-applies @{context} tag (e.g. 'home' → '@home')
                energy_level: high, medium, or low — auto-applies {level}-energy tag
            """
            try:
                import datetime as _dt

                if not project_id:
                    return "project_id is required. Tasks must belong to a project."
                try:
                    task_status = NoteStatus(status.lower())
                except ValueError:
                    return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return f"Invalid source: {source}. Valid: {', '.join(s.value for s in NoteSource)}"
                parsed_due = None
                if due_date:
                    try:
                        parsed_due = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                parsed_remind = None
                if remind_at:
                    try:
                        parsed_remind = _dt.date.fromisoformat(remind_at)
                    except ValueError:
                        return f"Invalid remind_at: {remind_at}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                # Auto-apply @context tag
                if context:
                    ctx = context.lstrip("@").strip()
                    if ctx:
                        tag_list.append(f"@{ctx}")
                # Auto-apply energy tag
                _energy_tags = {
                    "high": "high-energy",
                    "medium": "mid-energy",
                    "low": "low-energy",
                }
                if energy_level:
                    el = energy_level.lower()
                    if el not in _energy_tags:
                        return f"Invalid energy_level: {energy_level}. Valid: high, medium, low"
                    tag_list.append(_energy_tags[el])
                task = self.zettel_service.create_task(
                    title=title,
                    content=content,
                    status=task_status,
                    tags=tag_list,
                    project_id=project_id,
                    area_id=area_id,
                    due_date=parsed_due,
                    remind_at=parsed_remind,
                    priority=priority,
                    recurrence_rule=recurrence_rule,
                    estimated_minutes=estimated_minutes,
                    source=note_source,
                )
                return f"Task created successfully: {task.title} (ID: {task.id})"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_update_task")
        def pzk_update_task(
            task_id: str,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            due_date: Optional[str] = None,
            remind_at: Optional[str] = None,
            priority: Optional[int] = None,
            estimated_minutes: Optional[int] = None,
            recurrence_rule: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Update any fields on an existing task.

            This is the only task-update tool. Use it for both ordinary field edits and
            status transitions. When a recurring task is marked done, the next instance
            is spawned automatically after non-status edits are persisted. Passing tags
            replaces the task's existing tag list.

            Args:
                task_id: ID of the task note
                status: inbox, ready, scheduled, active, waiting, someday, done, cancelled
                project_id: ID of the project this task belongs to
                parent_project_id: Alternate name for the project this task belongs to
                due_date: Due date YYYY-MM-DD
                remind_at: Reminder date YYYY-MM-DD
                priority: 1 (low) to 4 (critical)
                estimated_minutes: Estimated effort in minutes
                recurrence_rule: daily, weekly, monthly, quarterly, yearly
                tags: Comma-separated tags (replaces existing tags)
            """
            try:
                import datetime as _dt

                task = self.zettel_service.get_note(task_id)
                if not task:
                    return f"Task not found: {task_id}"
                if task.note_type != NoteType.TASK:
                    return f"Note {task_id} is not a task (type: {task.note_type.value})"

                # Validate all inputs before applying any changes
                new_status = None
                if status is not None:
                    try:
                        new_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                parsed_due = None
                if due_date is not None:
                    try:
                        parsed_due = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                parsed_remind = None
                if remind_at is not None:
                    try:
                        parsed_remind = _dt.date.fromisoformat(remind_at)
                    except ValueError:
                        return f"Invalid remind_at: {remind_at}. Use YYYY-MM-DD."
                update_kwargs = {}
                if new_status is not None:
                    update_kwargs["status"] = new_status
                if parent_project_id is not None:
                    normalized_parent_project_id = parent_project_id.strip()
                    if project_id is not None:
                        normalized_project_id = project_id.strip()
                        if normalized_project_id != normalized_parent_project_id:
                            return (
                                "project_id and parent_project_id must match when "
                                "both are provided."
                            )
                    if not normalized_parent_project_id:
                        return (
                            "parent_project_id is required. Tasks must belong to a project."
                        )
                    update_kwargs["project_id"] = normalized_parent_project_id
                if project_id is not None:
                    normalized_project_id = project_id.strip()
                    if not normalized_project_id:
                        return "project_id is required. Tasks must belong to a project."
                    update_kwargs.setdefault("project_id", normalized_project_id)
                if due_date is not None:
                    update_kwargs["due_date"] = parsed_due
                if remind_at is not None:
                    update_kwargs["remind_at"] = parsed_remind
                if priority is not None:
                    update_kwargs["priority"] = priority
                if estimated_minutes is not None:
                    update_kwargs["estimated_minutes"] = estimated_minutes
                if recurrence_rule is not None:
                    update_kwargs["recurrence_rule"] = recurrence_rule
                if tags is not None:
                    update_kwargs["tags"] = [
                        t.strip() for t in tags.split(",") if t.strip()
                    ]

                msg = f"Task {task_id} updated successfully."
                if update_kwargs:
                    updated = self.zettel_service.update_task(task_id, **update_kwargs)
                else:
                    updated = task
                if new_status is not None:
                    msg += f" Status set to '{new_status.value}'."
                    if new_status == NoteStatus.DONE and updated.recurrence_rule:
                        msg += " New recurring instance created."

                return msg
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_tasks")
        def pzk_get_tasks(
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            due_date: Optional[str] = None,
            overdue_only: bool = False,
            priority: Optional[int] = None,
            limit: int = 20,
        ) -> str:
            """Query tasks with optional filters.
            Args:
                status: Filter by status (inbox, ready, active, waiting, someday, done, cancelled)
                project_id: Filter to tasks linked to this project
                due_date: Filter to tasks due on or before this date (YYYY-MM-DD)
                overdue_only: Only return tasks with due_date before today
                priority: Filter by priority (1-4)
                limit: Maximum results
            """
            try:
                import datetime as _dt

                task_status = None
                if status:
                    try:
                        task_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                due_before = None
                if overdue_only:
                    due_before = _dt.date.today() - _dt.timedelta(days=1)
                elif due_date:
                    try:
                        due_before = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                tasks = self.zettel_service.get_tasks(
                    status=task_status,
                    project_id=project_id,
                    due_date_before=due_before,
                    priority=priority,
                    limit=limit,
                )
                if not tasks:
                    return "No matching tasks found."
                out = f"Found {len(tasks)} task(s):\n\n"
                for i, t in enumerate(tasks, 1):
                    out += f"{i}. {t.title} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}"
                    if t.due_date:
                        out += f"  Due: {t.due_date}"
                    if t.priority:
                        out += f"  Priority: {t.priority}"
                    out += "\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_todays_tasks")
        def pzk_get_todays_tasks(include_overdue: bool = True) -> str:
            """Return tasks due today and optionally overdue tasks.
            Args:
                include_overdue: Include tasks with past due dates (default: True)
            """
            try:
                tasks = self.zettel_service.get_todays_tasks(include_overdue)
                if not tasks:
                    return "No tasks due today."
                out = f"Today's tasks ({len(tasks)}):\n\n"
                for i, t in enumerate(tasks, 1):
                    priority_str = f" [P{t.priority}]" if t.priority else ""
                    due_str = f" — due {t.due_date}" if t.due_date else ""
                    out += f"{i}.{priority_str} {t.title}{due_str} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_project")
        def pzk_create_project(
            title: str,
            content: str,
            source: str,
            area_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            outcome: Optional[str] = None,
            deadline: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create a project note linked to an area or parent project.
            Args:
                title: Project title
                content: Project description
                source: Origin of the project note
                area_id: ID of the area this project belongs to
                parent_project_id: Optional parent project ID for subprojects
                outcome: The desired outcome/goal
                deadline: Target completion date (YYYY-MM-DD)
                tags: Comma-separated tags
            """
            try:
                import datetime as _dt

                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return (
                        f"Invalid source: {source}. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )
                normalized_parent_project_id = (
                    parent_project_id.strip() if parent_project_id is not None else None
                )
                parent_project_id = normalized_parent_project_id or None
                resolved_area_id = area_id
                if parent_project_id:
                    parent_project = self.zettel_service.get_note(parent_project_id)
                    if not parent_project or parent_project.note_type != NoteType.PROJECT:
                        return (
                            f"parent_project_id {parent_project_id} "
                            "is not a valid project note."
                        )
                    if not parent_project.area_id:
                        return (
                            f"parent_project_id {parent_project_id} "
                            "does not have an area_id."
                        )
                    if (
                        resolved_area_id
                        and resolved_area_id != parent_project.area_id
                    ):
                        return (
                            f"area_id {resolved_area_id} does not match project "
                            f"{parent_project_id} area_id {parent_project.area_id}"
                        )
                    resolved_area_id = parent_project.area_id
                elif not resolved_area_id:
                    return "area_id is required for top-level projects."
                area = self.zettel_service.get_note(resolved_area_id)
                if not area or area.note_type != NoteType.AREA:
                    return f"area_id {resolved_area_id} is not a valid area note."
                parsed_deadline = None
                if deadline:
                    try:
                        parsed_deadline = _dt.date.fromisoformat(deadline)
                    except ValueError:
                        return f"Invalid deadline: {deadline}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                project = self.zettel_service.create_project_note(
                    title=title,
                    content=content,
                    outcome=outcome,
                    deadline=parsed_deadline,
                    area_id=resolved_area_id,
                    project_id=parent_project_id,
                    tags=tag_list,
                    source=note_source,
                )
                return f"Project created successfully with ID: {project.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_subproject")
        def pzk_create_subproject(
            parent_project_id: str,
            title: str,
            content: str,
            source: str,
            outcome: Optional[str] = None,
            deadline: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create a subproject under an existing parent project.
            Args:
                parent_project_id: ID of the parent project
                title: Subproject title
                content: Subproject description
                source: Origin of the subproject note
                outcome: The desired outcome/goal
                deadline: Target completion date (YYYY-MM-DD)
                tags: Comma-separated tags
            """
            try:
                import datetime as _dt

                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return (
                        f"Invalid source: {source}. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )
                normalized_parent_project_id = parent_project_id.strip()
                if not normalized_parent_project_id:
                    return "parent_project_id is required."
                parent_project_id = normalized_parent_project_id
                parent_project = self.zettel_service.get_note(parent_project_id)
                if not parent_project or parent_project.note_type != NoteType.PROJECT:
                    return (
                        f"parent_project_id {parent_project_id} "
                        "is not a valid project note."
                    )
                if not parent_project.area_id:
                    return (
                        f"parent_project_id {parent_project_id} "
                        "does not have an area_id."
                    )
                parsed_deadline = None
                if deadline:
                    try:
                        parsed_deadline = _dt.date.fromisoformat(deadline)
                    except ValueError:
                        return f"Invalid deadline: {deadline}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                project = self.zettel_service.create_project_note(
                    title=title,
                    content=content,
                    outcome=outcome,
                    deadline=parsed_deadline,
                    area_id=parent_project.area_id,
                    project_id=parent_project_id,
                    tags=tag_list,
                    source=note_source,
                )
                return f"Subproject created successfully with ID: {project.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project")
        def pzk_get_project(project_id: str) -> str:
            """Get a project note with task, note, and subproject context.
            Args:
                project_id: ID of the project note
            """
            try:
                project = self.zettel_service.get_note(project_id)
                if not project:
                    return f"Project not found: {project_id}"
                if project.note_type != NoteType.PROJECT:
                    return f"Note {project_id} is not a project (type: {project.note_type.value})"
                tasks = self.zettel_service.get_project_tasks(project_id)
                project_notes = self.zettel_service.get_project_notes(project_id)
                parent_project = self.zettel_service.get_parent_project(project_id)
                subprojects = self.zettel_service.get_subprojects(project_id)
                preview_tasks = self._get_project_preview_tasks(tasks)
                counts: dict = {}
                for t in tasks:
                    s = t.status.value if t.status else "none"
                    counts[s] = counts.get(s, 0) + 1
                outcome = project.metadata.get("outcome", "")
                out = f"ID: {project.id}\n"
                if project.area_id:
                    out += f"Area ID: {project.area_id}\n"
                if outcome:
                    out += f"Outcome: {outcome}\n"
                out += f"Tasks: {len(tasks)} total"
                if counts:
                    out += " (" + ", ".join(f"{v} {k}" for k, v in counts.items()) + ")"
                out += "\n\nNext Tasks:\n"
                if preview_tasks:
                    for task in preview_tasks:
                        out += self._format_project_preview_task(task) + "\n"
                else:
                    out += "- None\n"
                if parent_project:
                    out += "\nParent Project:\n"
                    out += f"- {parent_project.title} (ID: {parent_project.id})\n"
                out += "\nSubprojects:\n"
                if subprojects:
                    for subproject in subprojects:
                        out += f"- {subproject.title} (ID: {subproject.id})\n"
                else:
                    out += "- None\n"
                out += "\nNotes:\n"
                if project_notes:
                    for note in project_notes:
                        out += (
                            f"- {note.title} (ID: {note.id}, type: "
                            f"{note.note_type.value})\n"
                        )
                else:
                    out += "- None\n"
                out += f"\n\n{project.content}\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project_notes")
        def pzk_get_project_notes(project_id: str, limit: int = 50) -> str:
            """Get all non-task notes routed to a project.
            Args:
                project_id: ID of the project note
                limit: Maximum results
            """
            try:
                notes = self.zettel_service.get_project_notes(project_id)
                notes = notes[:limit]
                if not notes:
                    return f"No project notes found for project {project_id}."

                out = f"Project notes for {project_id} ({len(notes)}):\n\n"
                rendered_notes = [self._format_note_result(note) for note in notes]
                out += "\n---\n\n".join(rendered_notes)
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project_tasks")
        def pzk_get_project_tasks(
            project_id: str,
            status: Optional[str] = None,
            limit: int = 50,
        ) -> str:
            """Get all tasks linked to a project.
            Args:
                project_id: ID of the project note
                status: Filter by status
                limit: Maximum results
            """
            try:
                task_status = None
                if status:
                    try:
                        task_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}."
                tasks = self.zettel_service.get_project_tasks(project_id, task_status)
                tasks = tasks[:limit]
                if not tasks:
                    return f"No tasks found for project {project_id}."
                out = f"Tasks for project {project_id} ({len(tasks)}):\n\n"
                for i, t in enumerate(tasks, 1):
                    out += f"{i}. {t.title} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}"
                    if t.due_date:
                        out += f"  Due: {t.due_date}"
                    out += "\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_area")
        def pzk_create_area(
            title: str,
            content: str,
            cadence: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create an area note (ongoing responsibility with no end date).
            Args:
                title: Area title
                content: Area description
                cadence: Review cadence (e.g. 'weekly review', 'monthly check-in')
                tags: Comma-separated tags
            """
            try:
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                area = self.zettel_service.create_area_note(
                    title=title, content=content, cadence=cadence, tags=tag_list
                )
                return f"Area created successfully with ID: {area.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_list_projects")
        def pzk_list_projects(include_done: bool = False, limit: int = 20) -> str:
            """List all project notes, sorted by due date.
            Args:
                include_done: Include completed/cancelled projects (default: False)
                limit: Maximum results
            """
            try:
                import datetime as _dt

                projects = self.zettel_service.search_notes(note_type=NoteType.PROJECT)
                if not include_done:
                    projects = [
                        p
                        for p in projects
                        if p.status not in (NoteStatus.DONE, NoteStatus.CANCELLED)
                    ]
                projects.sort(key=lambda p: (p.due_date or _dt.date.max))
                projects = projects[:limit]
                if not projects:
                    return "No active projects found."
                out = f"Projects ({len(projects)}):\n\n"
                for i, p in enumerate(projects, 1):
                    out += f"{i}. {p.title} (ID: {p.id})\n"
                    if p.due_date:
                        out += f"   Deadline: {p.due_date}\n"
                    outcome = p.metadata.get("outcome", "")
                    if outcome:
                        out += f"   Outcome: {outcome}\n"
                    out += "\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_list_areas")
        def pzk_list_areas(limit: int = 20) -> str:
            """List all area notes.
            Args:
                limit: Maximum results
            """
            try:
                areas = self.zettel_service.search_notes(note_type=NoteType.AREA)
                areas = areas[:limit]
                if not areas:
                    return "No areas found."
                out = f"Areas ({len(areas)}):\n\n"
                for i, a in enumerate(areas, 1):
                    out += f"{i}. {a.title} (ID: {a.id})\n"
                    cadence = a.metadata.get("cadence", "")
                    if cadence:
                        out += f"   Cadence: {cadence}\n"
                    out += "\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_area")
        def pzk_get_area(area_id: str) -> str:
            """Get an area note with its linked projects and open task counts.
            Args:
                area_id: ID of the area note
            """
            try:
                area = self.zettel_service.get_note(area_id)
                if not area:
                    return f"Area not found: {area_id}"
                if area.note_type != NoteType.AREA:
                    return f"Note {area_id} is not an area (type: {area.note_type.value})"
                projects = self.zettel_service.search_notes(
                    note_type=NoteType.PROJECT, area_id=area_id
                )
                cadence = area.metadata.get("cadence", "")
                out = f"ID: {area.id}\n"
                if cadence:
                    out += f"Cadence: {cadence}\n"
                out += f"Projects: {len(projects)}\n"
                out += f"\n{area.content}\n"
                if projects:
                    out += "\n## Projects\n"
                    for p in projects:
                        task_count = len(self.zettel_service.get_project_tasks(p.id))
                        out += f"- {p.title} (ID: {p.id}) — {task_count} task(s)\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_reminders")
        def pzk_get_reminders(limit: int = 20) -> str:
            """Return notes and tasks with remind_at <= today, sorted by remind_at.
            Args:
                limit: Maximum results (default 20)
            """
            try:
                notes = self.zettel_service.get_reminders(limit)
                if not notes:
                    return "No reminders due today."
                out = f"Reminders due ({len(notes)}):\n\n"
                for i, n in enumerate(notes, 1):
                    out += f"{i}. {n.title} (ID: {n.id})\n"
                    out += f"   Type: {n.note_type.value}  Remind: {n.remind_at}\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

    def _register_resources(self) -> None:
        """Register MCP resources."""
        # Currently, we don't define resources for the Zettelkasten server
        pass

    def _register_prompts(self) -> None:
        """Register MCP prompts."""
        # Currently, we don't define prompts for the Zettelkasten server
        pass

    def run(self) -> None:
        """Run the MCP server."""
        self.mcp.run()
