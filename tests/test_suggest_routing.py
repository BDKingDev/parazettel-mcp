"""Tests for semantic tag/area suggestion (deterministic hash provider).

The hash provider gives identical text identical vectors, so a query whose text
matches a tag (or an area's embedding text) is a perfect cosine match while
unrelated text is not — enough to exercise the ranking without a real model.
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


def test_suggest_tags_ranks_semantic_match_first(test_config, monkeypatch):
    """The tag whose name matches the query text ranks first; all tags are scored."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        repo.create(
            Note(title="n1", content="c1", note_type=NoteType.PERMANENT,
                 tags=[Tag(name="kuzu"), Tag(name="sales-tax")])
        )
        repo.create(
            Note(title="n2", content="c2", note_type=NoteType.PERMANENT,
                 tags=[Tag(name="embeddings")])
        )
        results = service.suggest_tags("kuzu", limit=5)
        assert results, "expected tag suggestions"
        assert results[0][0] == "kuzu"           # exact hash twin ranks first
        assert results[0][1] > 0.99
        assert {name for name, _ in results} >= {"kuzu", "sales-tax", "embeddings"}
        # Sorted high->low.
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)
    finally:
        repo.close()


def test_suggest_tags_respects_limit(test_config, monkeypatch):
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        repo.create(
            Note(title="n", content="c", note_type=NoteType.PERMANENT,
                 tags=[Tag(name="a"), Tag(name="b"), Tag(name="c"), Tag(name="d")])
        )
        assert len(service.suggest_tags("a", limit=2)) == 2
    finally:
        repo.close()


def test_suggest_tags_empty_when_embeddings_disabled(test_config, monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", False)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        repo.create(
            Note(title="n", content="c", note_type=NoteType.PERMANENT,
                 tags=[Tag(name="kuzu")])
        )
        assert service.suggest_tags("kuzu") == []
    finally:
        repo.close()


def test_suggest_areas_ranks_matching_area_first(test_config, monkeypatch):
    """The area whose stored vector matches the query ranks first."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        area_a = repo.create(
            Note(title="Knowledge Management", content="capturing durable ideas",
                 note_type=NoteType.AREA)
        )
        repo.create(
            Note(title="Home Maintenance", content="chores and repairs",
                 note_type=NoteType.AREA)
        )
        # Query with area_a's exact embedding text -> a hash twin of its stored
        # document vector -> cosine 1.0 for area_a, ~0 for the other.
        query = repo._embedding_text(area_a)
        results = service.suggest_areas(query, limit=5)
        assert results, "expected area suggestions"
        assert results[0][0].id == area_a.id
        assert results[0][1] > 0.99
    finally:
        repo.close()


def test_suggest_areas_empty_when_embeddings_disabled(test_config, monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", False)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    service = ZettelService(repository=repo)
    try:
        repo.create(Note(title="Area", content="x", note_type=NoteType.AREA))
        assert service.suggest_areas("Area") == []
    finally:
        repo.close()


def test_embed_tags_caches_per_model(test_config, monkeypatch):
    """Repeat tag-embedding only embeds NEW tags; cached ones are reused."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        calls = []
        real = repo._embedding_provider.embed_documents

        def counting(texts):
            texts = list(texts)
            calls.append(texts)
            return real(texts)

        monkeypatch.setattr(repo._embedding_provider, "embed_documents", counting)
        first = repo.embed_tags(["a", "b"])
        second = repo.embed_tags(["a", "b", "c"])
        assert set(first) == {"a", "b"}
        assert set(second) == {"a", "b", "c"}
        # Only the new tag "c" is embedded on the second call.
        assert calls == [["a", "b"], ["c"]]
    finally:
        repo.close()
