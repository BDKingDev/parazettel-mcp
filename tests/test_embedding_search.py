"""Tests for hybrid (BM25 + vector) search wiring.

The deterministic ``hash`` provider carries no real semantics, so these tests
exercise the *mechanics*: the brute-force fallback for not-yet-indexed notes,
the RRF fusion (which preserves BM25 scores for dedup-safety), and graceful
degradation to BM25 when embeddings are disabled.
"""

from unittest.mock import MagicMock

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import Note, NoteType
from parazettel_mcp.services.search_service import SearchResult, SearchService
from parazettel_mcp.services.zettel_service import ZettelService
from parazettel_mcp.storage.note_repository import NoteRepository


def _enable_hash_embeddings(monkeypatch, dim=16):
    """Enable the deterministic hash embedding provider for the test process."""
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_provider", "hash")
    monkeypatch.setattr(config, "embedding_dim", dim)
    monkeypatch.setattr(config, "embedding_metric", "cosine")


def _note(nid, title, content):
    """Build a permanent Note with a fixed id for fusion tests."""
    return Note(id=nid, title=title, content=content, note_type=NoteType.PERMANENT)


def test_vector_search_fallback_finds_unindexed_note(test_config, monkeypatch):
    """A note created after the last rebuild is found via the brute-force pass."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        for title, body in [("A", "alpha"), ("B", "beta"), ("C", "gamma")]:
            repo.create(Note(title=title, content=body, note_type=NoteType.PERMANENT))
        repo.rebuild_index()  # builds HNSW; existing notes marked indexed=true
        fresh = repo.create(
            Note(title="Delta", content="delta unique body", note_type=NoteType.PERMANENT)
        )
        # `fresh` is dirty (not in the HNSW index) -> only the fallback can find it.
        ids = repo.vector_search_ids(repo._embedding_text(fresh), limit=10)
        assert fresh.id in ids
    finally:
        repo.close()


def test_vector_search_matches_on_indexed_title_vector(test_config, monkeypatch):
    """A note whose TITLE equals the query is found via the title HNSW index,
    even though its body — and thus its doc vector — is unrelated.

    With the hash provider, embed_query(text) == the title's passage vector, so
    the title-index distance is ~0; a doc-only index could not produce that.
    """
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        target = repo.create(
            Note(title="frantic intake", content="zebra giraffe orbit telescope",
                 note_type=NoteType.PERMANENT)
        )
        for t, b in [("A", "alpha"), ("B", "beta"), ("C", "gamma")]:
            repo.create(Note(title=t, content=b, note_type=NoteType.PERMANENT))
        repo.rebuild_index()  # builds both the doc and the title HNSW indexes
        hits = dict(repo.vector_search("frantic intake", limit=10))
        assert target.id in hits
        assert hits[target.id] < 1e-3  # exact title-vector match
    finally:
        repo.close()


def test_vector_search_matches_on_pending_title_vector(test_config, monkeypatch):
    """The title-vector match also works for a dirty (not-yet-indexed) note via
    the brute-force fallback over PendingEmbedding.title_embedding."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        for t, b in [("A", "alpha"), ("B", "beta")]:
            repo.create(Note(title=t, content=b, note_type=NoteType.PERMANENT))
        repo.rebuild_index()
        fresh = repo.create(
            Note(title="frantic intake", content="zebra giraffe orbit",
                 note_type=NoteType.PERMANENT)
        )  # dirty -> only the title brute-force can match it at ~0
        hits = dict(repo.vector_search("frantic intake", limit=10))
        assert fresh.id in hits
        assert hits[fresh.id] < 1e-3
    finally:
        repo.close()


def test_vector_search_empty_when_disabled(test_config):
    """vector_search_ids returns [] when embeddings are disabled."""
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        repo.create(Note(title="A", content="a", note_type=NoteType.PERMANENT))
        assert repo._embedding_provider is None
        assert repo.vector_search_ids("anything") == []
    finally:
        repo.close()


def test_vector_search_excludes_deleted_note(test_config, monkeypatch):
    """A deleted note's lingering pending embedding is not returned (Note join)."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        note = repo.create(
            Note(title="Ephemeral", content="temporary body",
                 note_type=NoteType.PERMANENT)
        )
        text = repo._embedding_text(note)
        assert note.id in repo.vector_search_ids(text)  # findable before delete
        repo.delete(note.id)
        assert note.id not in repo.vector_search_ids(text)  # gone after delete
    finally:
        repo.close()


def test_fuse_hybrid_adds_vector_only_hits_and_preserves_bm25_score():
    """RRF includes a vector-only hit (score 0) and keeps each BM25 result's score."""
    zettel = MagicMock()
    zettel.repository.vector_search_ids.return_value = ["v1", "b1"]
    zettel.get_note.side_effect = lambda nid: (
        _note("v1", "Vector only", "semantic neighbour") if nid == "v1" else None
    )
    service = SearchService(zettel_service=zettel)

    bm25 = [
        SearchResult(
            note=_note("b1", "BM25 hit", "lexical match"),
            score=3.2,
            matched_terms={"match"},
            matched_context="Title: BM25 hit",
        )
    ]
    fused = service._fuse_hybrid("query", bm25, {})

    ids = [r.note.id for r in fused]
    assert set(ids) == {"b1", "v1"}
    # b1 appears in both rank lists -> highest RRF score -> first.
    assert ids[0] == "b1"
    by_id = {r.note.id: r for r in fused}
    assert by_id["b1"].score == 3.2  # BM25 score preserved (dedup-safe)
    assert by_id["v1"].score == 0.0  # vector-only carries no BM25 score


def test_fuse_hybrid_returns_bm25_unchanged_when_no_vector_hits():
    """With no vector hits, fusion returns the BM25 results unchanged."""
    zettel = MagicMock()
    zettel.repository.vector_search_ids.return_value = []
    service = SearchService(zettel_service=zettel)
    bm25 = [
        SearchResult(note=_note("b1", "x", "y"), score=1.0, matched_terms=set(),
                     matched_context="")
    ]
    assert service._fuse_hybrid("q", bm25, {}) == bm25


def test_search_combined_degrades_to_bm25_when_disabled(test_config):
    """search_combined returns plain BM25 results when embeddings are disabled."""
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = SearchService(zettel_service=ZettelService(repository=repo))
    try:
        repo.create(
            Note(title="Kuzu graph", content="graph database notes",
                 note_type=NoteType.PERMANENT)
        )
        results = service.search_combined(text="graph")
        assert results
        assert any(r.score > 0 for r in results)  # BM25 lexical match scored
    finally:
        repo.close()


def test_search_combined_hybrid_preserves_bm25_match(test_config, monkeypatch):
    """With embeddings on, hybrid search still surfaces the BM25 lexical match."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = SearchService(zettel_service=ZettelService(repository=repo))
    try:
        repo.create(
            Note(title="Kuzu vector index", content="hnsw semantic search",
                 note_type=NoteType.PERMANENT)
        )
        repo.create(
            Note(title="Meal planning", content="dinners and groceries",
                 note_type=NoteType.PERMANENT)
        )
        repo.rebuild_index()
        results = service.search_combined(text="vector")
        # Hybrid path ran; the lexical match still surfaces with a BM25 score.
        assert any(r.score > 0 for r in results)
    finally:
        repo.close()
