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


def _coerce_score_map(scores: Any) -> Dict[str, float]:
    """Return a real {id: score} dict, or {} for anything else (e.g. a test mock)."""
    if isinstance(scores, dict):
        return scores
    return {}


def _score_text_results(
    notes: List[Note],
    query: str,
    bm25_scores: Dict[str, float],
    *,
    include_title: bool = True,
    include_content: bool = True,
) -> List[SearchResult]:
    """Rank text-search notes by BM25 relevance, with lexical hits only as a tiebreaker.

    ``notes`` already arrive in BM25 order from the repository. Each result carries
    its real BM25 ``score`` (from ``bm25_scores``); a small lexical ``boost`` — exact
    phrase and per-term hits in the title/content — breaks ties so an exact title
    match edges out an equally-ranked partial. The boost never overrides BM25 order,
    and scores are never collapsed to a flat constant the way the previous substring
    heuristic did. When ``bm25_scores`` is empty (no provider, or a mocked repository
    in tests) every score is 0.0 and the stable sort preserves the repository order.
    """
    bm25_scores = _coerce_score_map(bm25_scores)
    query_lower = query.lower()
    terms = {t for t in query_lower.split() if t}
    ranked: List[Tuple[float, float, SearchResult]] = []

    for note in notes:
        bm25 = float(bm25_scores.get(note.id, 0.0))
        matched_terms: Set[str] = set()
        matched_context = ""
        boost = 0.0

        title_lower = note.title.lower() if (include_title and note.title) else ""
        if title_lower:
            if query_lower and query_lower in title_lower:
                boost += 1.0
                matched_context = f"Title: {note.title}"
            for term in terms:
                if term in title_lower:
                    boost += 0.1
                    matched_terms.add(term)

        content_lower = note.content.lower() if (include_content and note.content) else ""
        if content_lower:
            if query_lower and query_lower in content_lower:
                boost += 0.25
                if not matched_context:
                    index = content_lower.find(query_lower)
                    start = max(0, index - 40)
                    end = min(len(content_lower), index + len(query_lower) + 40)
                    matched_context = f"Content: ...{note.content[start:end]}..."
            for term in terms:
                if term in content_lower:
                    matched_terms.add(term)

        if not matched_context:
            if include_title and note.title:
                matched_context = f"Title: {note.title}"
            elif include_content and note.content:
                matched_context = f"Content: {note.content[:80]}"

        ranked.append(
            (
                bm25,
                boost,
                SearchResult(
                    note=note,
                    score=bm25,
                    matched_terms=matched_terms,
                    matched_context=matched_context,
                ),
            )
        )

    # Primary key: BM25 score (desc). Secondary: lexical boost (desc), a tiebreaker
    # only. Python's stable sort preserves the repository's BM25 order within ties.
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [result for _bm25, _boost, result in ranked]


class SearchService:
    """Service for searching notes in the Zettelkasten."""

    def __init__(self, zettel_service: Optional[ZettelService] = None):
        """Initialize the search service."""
        self.zettel_service = zettel_service or ZettelService()

    def initialize(self) -> None:
        """Initialize the service and dependencies."""
        # Initialize the zettel service if it hasn't been initialized
        self.zettel_service.initialize()

    def _get_text_candidates(
        self, query: str, *, include_content: bool, include_title: bool
    ) -> List[Note]:
        """Use the repository to prefilter text-search candidates."""
        if not include_content and not include_title:
            return []

        repository = self.zettel_service.repository
        if include_content and include_title:
            return repository.search(text=query)
        if include_title:
            return repository.search(title=query)
        return repository.search(content=query)

    def search_by_text(
        self, query: str, include_content: bool = True, include_title: bool = True
    ) -> List[SearchResult]:
        """Search for notes by text content, ranked by BM25 relevance."""
        if not query:
            return []

        # Use the graph index to narrow AND rank the candidate set (BM25 order).
        all_notes = self._get_text_candidates(
            query, include_content=include_content, include_title=include_title
        )
        bm25_scores = self._text_fts_scores(query)
        return _score_text_results(
            all_notes,
            query,
            bm25_scores,
            include_title=include_title,
            include_content=include_content,
        )

    def _text_fts_scores(self, query: str) -> Dict[str, float]:
        """Fetch BM25 scores from the repository; tolerate repos that lack the API."""
        repository = self.zettel_service.repository
        getter = getattr(repository, "text_fts_scores", None)
        if not callable(getter):
            return {}
        try:
            return _coerce_score_map(getter(query))
        except Exception:  # pragma: no cover - scoring is best-effort, never fatal
            return {}

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
        if text:
            search_kwargs["text"] = text

        filtered_notes = self.zettel_service.repository.search(**search_kwargs)

        if text:
            # Notes arrive in BM25 order; rank by the index's real relevance score.
            bm25_scores = self._text_fts_scores(text)
            return _score_text_results(filtered_notes, text, bm25_scores)

        # No text query: structural/tag filter only — uniform score, repository order.
        return [
            SearchResult(
                note=note, score=1.0, matched_terms=set(), matched_context=""
            )
            for note in filtered_notes
        ]
