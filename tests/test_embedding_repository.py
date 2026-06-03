"""Integration tests for embedding generation in the NoteRepository.

Uses the deterministic ``hash`` provider so the full pipeline (create/update,
rebuild, HNSW index, vector query) is exercised against real Kuzu without any
embedding model / torch / onnx dependency.
"""

import kuzu

from parazettel_mcp.config import config
from parazettel_mcp.models.graph_db import NOTE_VECTOR_INDEX, note_vector_index_exists
from parazettel_mcp.models.schema import Note, NoteType
from parazettel_mcp.storage.note_repository import NoteRepository


def _rows(result):
    out = []
    while result.has_next():
        out.append(result.get_next())
    return out


def _enable_hash_embeddings(monkeypatch, dim=16):
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_provider", "hash")
    monkeypatch.setattr(config, "embedding_dim", dim)
    monkeypatch.setattr(config, "embedding_metric", "cosine")


def test_create_stores_embedding(test_config, monkeypatch):
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
        conn = kuzu.Connection(repo.db)
        try:
            row = conn.execute(
                "MATCH (n:Note {id: $id}) "
                "RETURN n.embedding, n.embedding_model, n.embedded_at",
                {"id": saved.id},
            ).get_next()
            embedding, model, embedded_at = row
            assert embedding is not None and len(embedding) == 16
            assert model == "hash:deterministic:16"
            assert embedded_at is not None
        finally:
            conn.close()
    finally:
        repo.close()


def test_rebuild_builds_index_and_vector_query_works(test_config, monkeypatch):
    _enable_hash_embeddings(monkeypatch)
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        notes = [
            repo.create(Note(title=t, content=c, note_type=NoteType.PERMANENT))
            for t, c in [
                ("Kuzu vector index", "HNSW semantic search inside Kuzu"),
                ("Weekly meal planning", "Dinners and grocery lists"),
                ("Bookkeeping basics", "Ledgers and reconciliation"),
            ]
        ]
        repo.rebuild_index()

        conn = kuzu.Connection(repo.db)
        try:
            assert note_vector_index_exists(conn)
            # Every note carries an embedding after the rebuild backfill.
            embedded = _rows(
                conn.execute(
                    "MATCH (n:Note) WHERE n.embedding IS NOT NULL RETURN count(n)"
                )
            )[0][0]
            assert embedded == len(notes)
            # Querying the index with a note's own stored vector returns that
            # note first (distance ~0) — proving the vector is indexed and found.
            target = notes[0]
            stored = conn.execute(
                "MATCH (n:Note {id: $id}) RETURN n.embedding", {"id": target.id}
            ).get_next()[0]
            top = _rows(
                conn.execute(
                    f"CALL QUERY_VECTOR_INDEX('Note', '{NOTE_VECTOR_INDEX}', $q, 1) "
                    "RETURN node.id, distance ORDER BY distance",
                    {"q": stored},
                )
            )
            assert top and top[0][0] == target.id
            assert top[0][1] < 1e-4  # its own vector -> ~zero cosine distance
        finally:
            conn.close()
    finally:
        repo.close()


def test_disabled_by_default_adds_no_embedding_column(test_config):
    # No embedding flags set -> embeddings disabled -> schema untouched.
    repo = NoteRepository(notes_dir=test_config.notes_dir)
    try:
        assert repo._embedding_provider is None
        repo.create(Note(title="x", content="y", note_type=NoteType.PERMANENT))
        conn = kuzu.Connection(repo.db)
        try:
            cols = {r[1] for r in _rows(conn.execute("CALL TABLE_INFO('Note') RETURN *"))}
            assert "embedding" not in cols
            assert not note_vector_index_exists(conn)
        finally:
            conn.close()
    finally:
        repo.close()
