"""Service for searching and discovering notes in the Zettelkasten."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from parazettel_mcp.models.schema import LinkType, Note, NoteStatus, NoteType, Tag
from parazettel_mcp.services.zettel_service import ZettelService

# Reciprocal Rank Fusion constant (standard default); larger flattens the
# contribution of top ranks. Vector candidate pool size for fusion.
_RRF_K = 60
_VECTOR_TOPK = 50


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
    its real BM25 ``score`` (from ``bm25_scores``); a small lexical ``boost`` breaks
    ties only. The boost weights, strongest first: full-query substring in the title
    (+1.0), full-query substring in the content (+0.25), each query term present in
    the title (+0.1), each query term present in the content (+0.02). So an exact
    title match edges out an equally-ranked partial, and a content term hit can edge
    out a BM25-equal note with no term hit, while title hits still outrank content
    hits. The boost never overrides BM25 order, and scores are never collapsed to a
    flat constant the way the previous substring heuristic did. When ``bm25_scores``
    is empty (no provider, or a mocked repository in tests) every score is 0.0 and
    the stable sort preserves the repository order.
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
                    if term not in matched_terms:
                        # Small tiebreaker so a content term hit can edge out a
                        # BM25-equal note with no term hit at all. Kept below the
                        # title term boost (0.1) so title hits still rank higher.
                        boost += 0.02
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
    ) -> Tuple[List[Note], Dict[str, float]]:
        """Prefilter candidates AND fetch their BM25 scores in one repository pass.

        The repository returns scores from the same FTS index that produced the
        candidate ordering, so a title-only or content-only search is scored by
        its own index rather than the combined title+content index.
        """
        if not include_content and not include_title:
            return [], {}

        repository = self.zettel_service.repository
        if include_content and include_title:
            search_kwargs = {"text": query}
        elif include_title:
            search_kwargs = {"title": query}
        else:
            search_kwargs = {"content": query}

        scored = getattr(repository, "search_scored", None)
        if callable(scored):
            return scored(**search_kwargs)
        # Fallback for repositories (or mocks) without the one-pass API: candidates
        # only, no BM25 scores — _score_text_results then keeps repository order.
        return repository.search(**search_kwargs), {}

    def search_by_text(
        self, query: str, include_content: bool = True, include_title: bool = True
    ) -> List[SearchResult]:
        """Search for notes by text content, ranked by BM25 relevance."""
        if not query:
            return []

        # One FTS pass narrows, ranks, and scores the candidate set.
        all_notes, bm25_scores = self._get_text_candidates(
            query, include_content=include_content, include_title=include_title
        )
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

    def _fuse_hybrid(
        self,
        query: str,
        bm25_results: List[SearchResult],
        search_kwargs: Dict[str, Any],
    ) -> List[SearchResult]:
        """Blend BM25 results with semantic vector hits via Reciprocal Rank Fusion.

        Ordering is fused on *rank* (RRF, which sidesteps BM25-vs-cosine score
        incompatibility), but every result keeps its BM25 ``score`` so
        score-threshold consumers (e.g. dedup-on-create) are unaffected;
        vector-only hits carry score 0.0. When embeddings are disabled or
        unavailable the vector list is empty and the BM25 ordering is returned
        unchanged — so search never breaks because of an embedding hiccup.
        """
        repository = self.zettel_service.repository
        vector_search = getattr(repository, "vector_search_ids", None)
        if not callable(vector_search):
            return bm25_results
        try:
            vector_ids = vector_search(query, limit=_VECTOR_TOPK)
        except Exception:  # pragma: no cover - vector path is best-effort
            return bm25_results
        # Tolerate repositories/mocks without a real list result (e.g. tests with
        # a MagicMock repository): only fuse when we got an actual id list.
        if not isinstance(vector_ids, list) or not vector_ids:
            return bm25_results

        # Apply the same structural filters to vector hits by intersecting with a
        # filter-only id set (reuses the repository filter logic; no duplication).
        structural = {k: v for k, v in search_kwargs.items() if k != "text"}
        if structural:
            try:
                allowed = {note.id for note in repository.search(**structural)}
                vector_ids = [nid for nid in vector_ids if nid in allowed]
            except Exception:  # pragma: no cover
                vector_ids = []
        if not vector_ids:
            return bm25_results

        results_by_id: Dict[str, SearchResult] = {
            r.note.id: r for r in bm25_results
        }
        fused: Dict[str, float] = {}
        for rank, result in enumerate(bm25_results):
            fused[result.note.id] = fused.get(result.note.id, 0.0) + 1.0 / (
                _RRF_K + rank + 1
            )
        for rank, note_id in enumerate(vector_ids):
            fused[note_id] = fused.get(note_id, 0.0) + 1.0 / (_RRF_K + rank + 1)

        # Materialize results for vector-only hits (semantic matches BM25 missed).
        for note_id in vector_ids:
            if note_id in results_by_id:
                continue
            note = self.zettel_service.get_note(note_id)
            if note is None:
                continue
            results_by_id[note_id] = SearchResult(
                note=note,
                score=0.0,  # no BM25 (lexical) match; surfaced by semantic rank
                matched_terms=set(),
                matched_context=(
                    f"Content: {note.content[:80]}" if note.content else ""
                ),
            )

        return sorted(
            results_by_id.values(),
            key=lambda r: fused.get(r.note.id, 0.0),
            reverse=True,
        )

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

        repository = self.zettel_service.repository
        if text:
            # One FTS pass returns BM25-ordered candidates AND their scores.
            scored = getattr(repository, "search_scored", None)
            if callable(scored):
                filtered_notes, bm25_scores = scored(**search_kwargs)
            else:  # fallback: candidates only, then a separate score lookup
                filtered_notes = repository.search(**search_kwargs)
                bm25_scores = self._text_fts_scores(text)
            bm25_results = _score_text_results(filtered_notes, text, bm25_scores)
            # Blend in semantic vector hits (no-op when embeddings are disabled).
            return self._fuse_hybrid(text, bm25_results, search_kwargs)

        filtered_notes = repository.search(**search_kwargs)

        # No text query: structural/tag filter only — uniform score, repository order.
        return [
            SearchResult(
                note=note, score=1.0, matched_terms=set(), matched_context=""
            )
            for note in filtered_notes
        ]
