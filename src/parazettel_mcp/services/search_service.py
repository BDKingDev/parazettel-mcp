"""Service for searching and discovering notes in the Zettelkasten."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from parazettel_mcp.models.schema import LinkType, Note, NoteStatus, NoteType, Tag
from parazettel_mcp.services.zettel_service import ZettelService


@dataclass
class SearchResult:
    """A search result with a note and its relevance score."""

    note: Note
    score: float
    matched_terms: Set[str]
    matched_context: str


class SearchService:
    """Service for searching notes in the Zettelkasten."""

    def __init__(self, zettel_service: Optional[ZettelService] = None):
        """Initialize the search service."""
        self.zettel_service = zettel_service or ZettelService()

    def initialize(self) -> None:
        """Initialize the service and dependencies."""
        # Initialize the zettel service if it hasn't been initialized
        self.zettel_service.initialize()

    def search_by_text(
        self, query: str, include_content: bool = True, include_title: bool = True
    ) -> List[SearchResult]:
        """Search for notes by text content."""
        if not query:
            return []

        # Normalize query
        query = query.lower()
        query_terms = set(query.split())

        # Get all notes
        all_notes = self.zettel_service.get_all_notes()
        results = []

        for note in all_notes:
            score = 0.0
            matched_terms: Set[str] = set()
            matched_context = ""

            # Check title
            if include_title and note.title:
                title_lower = note.title.lower()
                # Exact match in title is highest score
                if query in title_lower:
                    score += 2.0
                    matched_context = f"Title: {note.title}"
                # Check for term matches in title
                for term in query_terms:
                    if term in title_lower:
                        score += 0.5
                        matched_terms.add(term)

            # Check content
            if include_content and note.content:
                content_lower = note.content.lower()
                # Exact match in content
                if query in content_lower:
                    score += 1.0
                    # Extract a snippet around the match
                    index = content_lower.find(query)
                    start = max(0, index - 40)
                    end = min(len(content_lower), index + len(query) + 40)
                    snippet = note.content[start:end]
                    matched_context = f"Content: ...{snippet}..."
                # Check for term matches in content
                for term in query_terms:
                    if term in content_lower:
                        score += 0.2
                        matched_terms.add(term)

            # Add to results if score is positive
            if score > 0:
                results.append(
                    SearchResult(
                        note=note,
                        score=score,
                        matched_terms=matched_terms,
                        matched_context=matched_context,
                    )
                )

        # Sort by score (descending)
        results.sort(key=lambda x: x.score, reverse=True)
        return results

    def search_by_tag(self, tags: Union[str, List[str]]) -> List[Note]:
        """Search for notes by tags."""
        if isinstance(tags, str):
            return self.zettel_service.get_notes_by_tag(tags)
        else:
            # If we have multiple tags, find notes with any of the tags
            all_matching_notes = []
            for tag in tags:
                notes = self.zettel_service.get_notes_by_tag(tag)
                all_matching_notes.extend(notes)
            # Remove duplicates by converting to a dictionary by ID
            unique_notes = {note.id: note for note in all_matching_notes}
            return list(unique_notes.values())

    def search_by_link(self, note_id: str, direction: str = "both") -> List[Note]:
        """Search for notes linked to/from a note."""
        return self.zettel_service.get_linked_notes(note_id, direction)

    def find_orphaned_notes(self) -> List[Note]:
        """Find notes with no incoming or outgoing links."""
        repo = self.zettel_service.repository
        orphan_ids = repo.find_orphaned_note_ids()
        notes = []
        for note_id in orphan_ids:
            note = self.zettel_service.get_note(note_id)
            if note:
                notes.append(note)
        return notes

    def find_central_notes(self, limit: int = 10) -> List[Tuple[Note, int]]:
        """Find notes with the most connections (incoming + outgoing links)."""
        repo = self.zettel_service.repository
        counts = repo.get_connection_counts(limit=limit)
        note_connections = []
        for note_id, total in counts:
            note = self.zettel_service.get_note(note_id)
            if note:
                note_connections.append((note, total))
        note_connections.sort(key=lambda x: x[1], reverse=True)
        return note_connections[:limit]

    def find_notes_by_date_range(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        use_updated: bool = False,
    ) -> List[Note]:
        """Find notes created or updated within a date range."""
        all_notes = self.zettel_service.get_all_notes()
        matching_notes = []

        for note in all_notes:
            # Get the relevant date
            date = note.updated_at if use_updated else note.created_at

            # Check if in range
            if start_date and date < start_date:
                continue
            if end_date and date >= end_date + timedelta(seconds=1):
                continue

            matching_notes.append(note)

        # Sort by date (descending)
        matching_notes.sort(
            key=lambda x: x.updated_at if use_updated else x.created_at, reverse=True
        )

        return matching_notes

    def find_similar_notes(self, note_id: str) -> List[Tuple[Note, float]]:
        """Find notes similar to the given note based on shared tags and links."""
        return self.zettel_service.find_similar_notes(note_id)

    def search_combined(
        self,
        text: Optional[str] = None,
        tags: Optional[List[str]] = None,
        note_type: Optional[NoteType] = None,
        status: Optional[NoteStatus] = None,
        project_id: Optional[str] = None,
        area_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[SearchResult]:
        """Perform a combined search with multiple criteria."""
        search_kwargs: Dict[str, Any] = {}
        if tags:
            search_kwargs["tags"] = tags
        if note_type:
            search_kwargs["note_type"] = note_type
        if status:
            search_kwargs["status"] = status
        if project_id:
            search_kwargs["project_id"] = project_id
        if area_id:
            search_kwargs["area_id"] = area_id
        if start_date:
            search_kwargs["created_after"] = start_date
        if end_date:
            search_kwargs["created_before"] = end_date

        all_notes = self.zettel_service.repository.search(**search_kwargs)

        filtered_notes = []
        for note in all_notes:
            filtered_notes.append(note)

        # If we have a text query, score the notes
        results = []
        if text:
            text = text.lower()
            query_terms = set(text.split())

            for note in filtered_notes:
                score = 0.0
                matched_terms: Set[str] = set()
                matched_context = ""

                # Check title
                title_lower = note.title.lower()
                if text in title_lower:
                    score += 2.0
                    matched_context = f"Title: {note.title}"

                for term in query_terms:
                    if term in title_lower:
                        score += 0.5
                        matched_terms.add(term)

                # Check content
                content_lower = note.content.lower()
                if text in content_lower:
                    score += 1.0
                    index = content_lower.find(text)
                    start = max(0, index - 40)
                    end = min(len(content_lower), index + len(text) + 40)
                    snippet = note.content[start:end]
                    matched_context = f"Content: ...{snippet}..."

                for term in query_terms:
                    if term in content_lower:
                        score += 0.2
                        matched_terms.add(term)

                # Add to results if score is positive
                if score > 0:
                    results.append(
                        SearchResult(
                            note=note,
                            score=score,
                            matched_terms=matched_terms,
                            matched_context=matched_context,
                        )
                    )
        else:
            # If no text query, just add all filtered notes with a default score
            results = [
                SearchResult(
                    note=note, score=1.0, matched_terms=set(), matched_context=""
                )
                for note in filtered_notes
            ]

        # Sort by score (descending)
        results.sort(key=lambda x: x.score, reverse=True)
        return results
