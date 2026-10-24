"""Integration tests for embedding generation in the NoteRepository.

Uses the deterministic ``hash`` provider so the full pipeline (create/update,
rebuild, exact vector search) is exercised against the real index without any
embedding model / torch / onnx dependency.
"""

import _index_helpers as index_helpers
from parazettel_mcp.config import config
from parazettel_mcp.models.schema import Note, NoteType
from parazettel_mcp.storage.note_repository import NoteRepository


def _enable_hash_embeddings(monkeypatch, dim=16):
    """Enable the deterministic hash embedding provider for the test process."""
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_provider", "hash")
    monkeypatch.setattr(config, "embedding_dim", dim)
    monkeypatch.setattr(config, "embedding_metric", "cosine")


def test_create_stores_embedding(test_config, monkeypatch):
    """create() stores doc + title vectors and the note is immediately searchable."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        saved = repo.create(
            Note(
                title="Atomic notes",
                content="Small single-claim notes.",
                note_type=NoteType.PERMANENT,
            )
        )
        vector = index_helpers.stored_vector(repo, saved.id)
        assert vector is not None and len(vector) == 16
        assert index_helpers.stored_vector(repo, saved.id, title=True) is not None
        assert index_helpers.stored_embedding_model(repo, saved.id) == "hash:deterministic:16"
        assert saved.id in repo.vector_search_ids(repo._embedding_text(saved))
    finally:
        repo.close()


def test_rebuild_embeds_every_note_and_vector_query_works(test_config, monkeypatch):
    """rebuild stores a vector per note, and a note's own vector finds it first."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        notes = [
            repo.create(Note(title=t, content=c, note_type=NoteType.PERMANENT))
            for t, c in [
                ("Vector index", "Semantic search over notes"),
                ("Weekly meal planning", "Dinners and grocery lists"),
                ("Bookkeeping basics", "Ledgers and reconciliation"),
            ]
        ]
        repo.rebuild_index()

        assert index_helpers.embedding_count(repo) == len(notes)
        target = notes[0]
        stored = repo.get_note_embedding(target.id)
        top = repo.vector_search_by_vector(stored, limit=1)
        assert top and top[0][0] == target.id
        assert top[0][1] < 1e-4  # its own vector -> ~zero cosine distance
    finally:
        repo.close()


def test_rebuild_reuses_unchanged_vectors(test_config, monkeypatch):
    """Only notes whose embedded text changed are re-embedded on rebuild."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        kept = repo.create(Note(title="Kept", content="same", note_type=NoteType.PERMANENT))
        changed = repo.create(
            Note(title="Changed", content="before", note_type=NoteType.PERMANENT)
        )
        path = repo.notes_dir / f"{changed.id}.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("before", "after"), encoding="utf-8"
        )
        embedded = []
        original = repo._embedding_provider.embed_documents

        def spy(texts):
            embedded.extend(texts)
            return original(texts)

        monkeypatch.setattr(repo._embedding_provider, "embed_documents", spy)
        repo.rebuild_index()
        assert any("after" in text for text in embedded)
        assert not any(text.startswith("Kept") for text in embedded)
        assert index_helpers.embedding_count(repo) == 2
        assert kept.id in repo.vector_search_ids("Kept same", limit=2)
    finally:
        repo.close()


def test_compute_embeddings_covers_only_the_requested_notes(test_config, monkeypatch):
    """Embedding a subset never returns vectors for other notes — including
    stored vectors with no text hash (as imported from Kuzu)."""
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        a = repo.create(Note(title="A", content="a", note_type=NoteType.PERMANENT))
        b = repo.create(Note(title="B", content="b", note_type=NoteType.PERMANENT))
        with repo._connection() as conn:
            conn.execute("UPDATE embeddings SET text_hash = NULL")
        result = repo._compute_embeddings([repo.get(a.id)])
        assert set(result) == {a.id}
        assert b.id not in result
    finally:
        repo.close()


def test_delete_removes_embedding(test_config, monkeypatch):
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        doomed = repo.create(Note(title="Doomed", content="x", note_type=NoteType.PERMANENT))
        repo.delete(doomed.id)
        assert index_helpers.embedding_count(repo) == 0
        assert doomed.id not in repo.vector_search_ids("Doomed x")
    finally:
        repo.close()


def test_disabled_by_default_stores_no_vectors(test_config):
    """With embeddings off, nothing is embedded and vector search is a no-op."""
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        assert repo._embedding_provider is None
        repo.create(Note(title="x", content="y", note_type=NoteType.PERMANENT))
        assert index_helpers.embedding_count(repo) == 0
        assert repo.vector_search("x") == []
    finally:
        repo.close()
