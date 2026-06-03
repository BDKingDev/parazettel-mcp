"""Tests for embedding-based find_similar_notes (deterministic hash provider).

The hash provider gives identical text identical vectors, so a content "twin"
is a reliable semantic match, while unrelated text is not — enough to exercise
the semantic path without a real model. The structural signal is checked
independently so the blend (max(structural, weight*semantic)) is covered.
"""

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import Note, NoteType, Tag
from parazettel_mcp.services.zettel_service import ZettelService
from parazettel_mcp.storage.note_repository import NoteRepository


def _enable_hash_embeddings(monkeypatch, dim=16):
    """Enable the deterministic hash embedding provider for the test process."""
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_provider", "hash")
    monkeypatch.setattr(config, "embedding_dim", dim)
    monkeypatch.setattr(config, "embedding_metric", "cosine")


def test_find_similar_surfaces_semantic_twin(test_config, monkeypatch):
    """A content twin (no shared tags/links) is found via the semantic signal."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        a = repo.create(
            Note(title="Atomic notes", content="small single-claim ideas",
                 note_type=NoteType.PERMANENT)
        )
        # Identical text -> identical hash vector -> semantic twin; shares no
        # tags or links with `a`, so only the semantic signal can surface it.
        twin = repo.create(
            Note(title="Atomic notes", content="small single-claim ideas",
                 note_type=NoteType.PERMANENT)
        )
        unrelated = repo.create(
            Note(title="Sales tax", content="quarterly bookkeeping filings",
                 note_type=NoteType.PERMANENT)
        )
        ids = [n.id for n, _score in service.find_similar_notes(a.id, threshold=0.5)]
        assert twin.id in ids
        assert unrelated.id not in ids
    finally:
        repo.close()


def test_semantic_similarity_is_metric_aware(monkeypatch):
    """Distance->similarity recovers cosine for each metric and clamps to [0, 1]."""
    sim = ZettelService._semantic_similarity
    monkeypatch.setattr(config, "embedding_metric", "cosine")
    assert sim(0.0) == 1.0
    assert sim(2.0) == 0.0  # opposite vectors clamp at 0
    monkeypatch.setattr(config, "embedding_metric", "l2")
    assert sim(0.0) == 1.0
    assert 0.0 <= sim(2.0) <= 1.0
    monkeypatch.setattr(config, "embedding_metric", "dotproduct")
    assert sim(-1.0) == 1.0  # dot=1 -> identical
    assert sim(1.0) == 0.0   # dot=-1 -> clamped


def test_find_similar_semantic_mode_keeps_structural_matches(test_config, monkeypatch):
    """With embeddings on, a shared-tag (structural) match still surfaces."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        a = repo.create(
            Note(title="Coffee brewing", content="pour-over technique",
                 note_type=NoteType.PERMANENT, tags=[Tag(name="kitchen")])
        )
        sibling = repo.create(
            Note(title="Knife skills", content="dicing onions cleanly",
                 note_type=NoteType.PERMANENT, tags=[Tag(name="kitchen")])
        )
        ids = [n.id for n, _score in service.find_similar_notes(a.id, threshold=0.3)]
        assert sibling.id in ids  # shared 'kitchen' tag -> structural match kept
    finally:
        repo.close()
