"""Integration tests for the Kuzu embedding schema + HNSW vector-index helpers.

These exercise the real Kuzu vector extension with tiny hand-made vectors, so no
embedding model (and no torch/onnx) is required.
"""

import kuzu
import pytest

from parazettel_mcp.models.graph_db import (
    NOTE_VECTOR_INDEX,
    close_graph_db,
    create_note_vector_index,
    drop_note_vector_index,
    ensure_embedding_schema,
    init_graph_db,
    note_vector_index_exists,
)


def _rows(result):
    out = []
    while result.has_next():
        out.append(result.get_next())
    return out


def _open(tmp_path):
    path = tmp_path / "g.kuzu"
    db = init_graph_db(path)
    return path, kuzu.Connection(db)


def test_ensure_embedding_schema_adds_columns_idempotently(tmp_path):
    path, conn = _open(tmp_path)
    try:
        ensure_embedding_schema(conn, 4)
        ensure_embedding_schema(conn, 4)  # idempotent: must not raise
        cols = {r[1] for r in _rows(conn.execute("CALL TABLE_INFO('Note') RETURN *"))}
        assert {"embedding", "embedded_at", "embedding_model"} <= cols
    finally:
        conn.close()
        close_graph_db(path)


def test_ensure_embedding_schema_rejects_bad_dim(tmp_path):
    path, conn = _open(tmp_path)
    try:
        with pytest.raises(ValueError):
            ensure_embedding_schema(conn, 0)
    finally:
        conn.close()
        close_graph_db(path)


def test_create_and_query_vector_index_end_to_end(tmp_path):
    path, conn = _open(tmp_path)
    try:
        ensure_embedding_schema(conn, 4)
        for nid, emb in [
            ("a", [1.0, 0.0, 0.0, 0.0]),
            ("b", [0.9, 0.1, 0.0, 0.0]),
            ("c", [0.0, 1.0, 0.0, 0.0]),
        ]:
            conn.execute(
                "CREATE (n:Note {id:$id, title:$id, embedding:$e})",
                parameters={"id": nid, "e": emb},
            )
        assert not note_vector_index_exists(conn)
        create_note_vector_index(conn, metric="cosine")
        assert note_vector_index_exists(conn)
        create_note_vector_index(conn, metric="cosine")  # no-op when present
        assert note_vector_index_exists(conn)

        results = _rows(
            conn.execute(
                f"CALL QUERY_VECTOR_INDEX('Note', '{NOTE_VECTOR_INDEX}', $q, 2) "
                "RETURN node.id, distance ORDER BY distance",
                parameters={"q": [1.0, 0.05, 0.0, 0.0]},
            )
        )
        ids = [r[0] for r in results]
        assert ids[0] == "a"  # nearest
        assert "c" not in ids  # orthogonal note excluded from top-2

        drop_note_vector_index(conn)
        drop_note_vector_index(conn)  # idempotent drop
        assert not note_vector_index_exists(conn)
    finally:
        conn.close()
        close_graph_db(path)


def test_create_vector_index_rejects_bad_metric(tmp_path):
    path, conn = _open(tmp_path)
    try:
        ensure_embedding_schema(conn, 4)
        with pytest.raises(ValueError):
            create_note_vector_index(conn, metric="euclidean")
    finally:
        conn.close()
        close_graph_db(path)


def test_create_vector_index_normalizes_metric(tmp_path):
    # A mixed-case/padded metric (as a human might set via env) is accepted.
    path, conn = _open(tmp_path)
    try:
        ensure_embedding_schema(conn, 4)
        conn.execute(
            "CREATE (n:Note {id:'a', title:'a', embedding:$e})",
            parameters={"e": [1.0, 0.0, 0.0, 0.0]},
        )
        create_note_vector_index(conn, metric="  COSINE ")
        assert note_vector_index_exists(conn)
    finally:
        conn.close()
        close_graph_db(path)
