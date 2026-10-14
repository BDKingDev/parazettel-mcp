"""Tests for the SQLite index primitives (connections, schema, FTS, vectors)."""

import sqlite3
import threading
import time

import numpy as np
import pytest

from parazettel_mcp.storage import index_db
from parazettel_mcp.storage.index_db import (
    EMBEDDING_GENERATION_KEY,
    bump_counter,
    connect,
    ensure_schema,
    fts_index_text,
    fts_match_expression,
    fts_terms,
    transaction,
    ts_in,
    ts_out,
    vector_to_blob,
)
from parazettel_mcp.storage.vector_index import VectorIndex


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "index.sqlite3"
    conn = connect(path)
    ensure_schema(conn)
    conn.close()
    return path


def _add_note(conn, note_id, title="", content=""):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO notes(id, title, content, note_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'permanent', '2026-01-01T00:00:00.000000', "
            "'2026-01-01T00:00:00.000000')",
            (note_id, title, content),
        )
        conn.execute(
            "INSERT INTO note_fts(rowid, title, content, body) VALUES (?, ?, ?, ?)",
            (
                cur.lastrowid,
                fts_index_text(title),
                fts_index_text(content),
                fts_index_text(f"{title}\n{content}"),
            ),
        )


def _match(conn, expression):
    return [
        row[0]
        for row in conn.execute(
            "SELECT n.id FROM note_fts JOIN notes n ON n.pk = note_fts.rowid "
            "WHERE note_fts MATCH ? ORDER BY bm25(note_fts)",
            (expression,),
        )
    ]


# --- connections ------------------------------------------------------------ #


def test_every_connection_gets_the_pragmas(db_path):
    for _ in range(2):
        conn = connect(db_path, busy_timeout_ms=4321)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        conn.close()


def test_probe_capabilities_passes_on_this_build(db_path):
    conn = connect(db_path)
    index_db.probe_capabilities(conn)
    conn.close()


def test_ensure_schema_is_idempotent(db_path):
    conn = connect(db_path)
    ensure_schema(conn)
    ensure_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == index_db.SCHEMA_VERSION
    conn.close()


def test_schema_version_mismatch_keeps_retrieval_signals(db_path):
    conn = connect(db_path)
    _add_note(conn, "n1", "Title")
    with transaction(conn):
        conn.execute("INSERT INTO retrieval_signals(note_id, hit_count) VALUES ('n1', 7)")
    conn.execute("PRAGMA user_version=999")
    ensure_schema(conn)
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert conn.execute("SELECT hit_count FROM retrieval_signals").fetchone()[0] == 7
    conn.close()


def test_transaction_rolls_back_on_error(db_path):
    conn = connect(db_path)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            _ = conn.execute(
                "INSERT INTO notes(id, note_type, created_at, updated_at) "
                "VALUES ('x', 'permanent', 't', 't')"
            )
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    conn.close()


def test_second_writer_waits_for_the_first(db_path):
    """Two connections (as two processes would) serialize on BEGIN IMMEDIATE."""
    first = connect(db_path)
    second = connect(db_path, busy_timeout_ms=5000)
    order = []

    def hold_lock():
        with transaction(first):
            order.append("first-start")
            time.sleep(0.3)
            order.append("first-end")

    thread = threading.Thread(target=hold_lock)
    thread.start()
    time.sleep(0.05)
    with transaction(second):
        order.append("second")
    thread.join()
    assert order == ["first-start", "first-end", "second"]
    first.close()
    second.close()


def test_writer_times_out_when_lock_is_held_too_long(db_path):
    holder = connect(db_path)
    waiter = connect(db_path, busy_timeout_ms=100)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with transaction(waiter):
                pass
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        waiter.close()


def test_readers_see_committed_snapshot_while_writer_is_open(db_path):
    writer = connect(db_path)
    reader = connect(db_path)
    _add_note(writer, "n1", "first")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO notes(id, note_type, created_at, updated_at) "
        "VALUES ('n2', 'permanent', 't', 't')"
    )
    assert reader.execute("SELECT count(*) FROM notes").fetchone()[0] == 1
    writer.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
    writer.close()
    reader.close()


def test_backup_and_prune(db_path, tmp_path):
    conn = connect(db_path)
    _add_note(conn, "n1", "Title")
    backups = []
    for i in range(4):
        dest = db_path.with_name(f"{db_path.name}.2026010{i}T000000.bak")
        index_db.backup_to(conn, dest)
        backups.append(dest)
        time.sleep(0.01)
    copy = sqlite3.connect(str(backups[-1]))
    assert copy.execute("SELECT id FROM notes").fetchall() == [("n1",)]
    copy.close()
    removed = index_db.prune_backups(db_path, keep=2)
    assert len(removed) == 2
    assert all(p.exists() for p in backups[-2:])
    conn.close()


# --- full-text ---------------------------------------------------------------- #


def test_fts_terms_drop_stopwords_and_duplicates():
    assert fts_terms("The library and THE Libraries of the world") == [
        "library",
        "libraries",
        "world",
    ]


def test_porter_stemming_matches_inflections(db_path):
    conn = connect(db_path)
    _add_note(conn, "n1", "Library design", "Notes about libraries")
    expression = fts_match_expression(fts_terms("library"), "body", conjunctive=False)
    assert _match(conn, expression) == ["n1"]
    conn.close()


def test_column_filters_replace_separate_indexes(db_path):
    conn = connect(db_path)
    _add_note(conn, "title-hit", "Onfleet webhooks", "body text")
    _add_note(conn, "content-hit", "Other", "onfleet webhooks in the body")
    title = fts_match_expression(["onfleet"], "title", conjunctive=True)
    content = fts_match_expression(["onfleet"], "content", conjunctive=True)
    body = fts_match_expression(["onfleet"], "body", conjunctive=True)
    assert _match(conn, title) == ["title-hit"]
    assert _match(conn, content) == ["content-hit"]
    assert set(_match(conn, body)) == {"title-hit", "content-hit"}
    conn.close()


def test_disjunctive_vs_conjunctive(db_path):
    conn = connect(db_path)
    _add_note(conn, "both", "alpha beta")
    _add_note(conn, "one", "alpha gamma")
    disjunctive = fts_match_expression(["alpha", "beta"], "body", conjunctive=False)
    conjunctive = fts_match_expression(["alpha", "beta"], "body", conjunctive=True)
    assert set(_match(conn, disjunctive)) == {"both", "one"}
    assert _match(conn, conjunctive) == ["both"]
    conn.close()


def test_common_term_in_every_note_is_still_returned(db_path):
    """FTS5 clamps IDF at a tiny positive value, so a term in every note still hits.

    (Kuzu's IDF went negative for such terms and dropped the matches entirely.)
    """
    conn = connect(db_path)
    for i in range(6):
        _add_note(conn, f"n{i}", f"onfleet note {i}", "onfleet everywhere")
    expression = fts_match_expression(["onfleet"], "body", conjunctive=False)
    assert len(_match(conn, expression)) == 6
    conn.close()


@pytest.mark.parametrize(
    "hostile",
    [
        '"unbalanced quote',
        "NEAR(a b)",
        "a AND b OR NOT c",
        "prefix*",
        "^caret",
        "col: value",
        "## Heading with [[20260101T000000000000000|link]]",
        "emoji 🚀 and ünïcödé",
        "backslash \\ and (parens) {braces}",
        "",
    ],
)
def test_hostile_input_never_breaks_the_query(db_path, hostile):
    conn = connect(db_path)
    _add_note(conn, "n1", "Heading link value caret", "emoji unicode prefix")
    for column in ("title", "content", "body"):
        for conjunctive in (True, False):
            expression = fts_match_expression(
                fts_terms(hostile), column, conjunctive=conjunctive
            )
            if expression is None:
                continue
            _match(conn, expression)  # must not raise
    conn.close()


def test_digits_are_searchable(db_path):
    conn = connect(db_path)
    _add_note(conn, "ticket", "Verify EAZE-119 filtering")
    expression = fts_match_expression(fts_terms("EAZE-119"), "body", conjunctive=True)
    assert _match(conn, expression) == ["ticket"]
    conn.close()


# --- timestamps ---------------------------------------------------------------- #


def test_timestamps_round_trip_and_sort_as_text():
    import datetime

    a = datetime.datetime(2026, 1, 1, 9, 5, 3)
    b = datetime.datetime(2026, 1, 1, 9, 5, 3, 120)
    assert ts_out(ts_in(a)) == a
    assert ts_in(a) < ts_in(b)
    aware = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    assert ts_out(ts_in(aware)).tzinfo is None


# --- vectors ------------------------------------------------------------------ #


def _store_vector(conn, note_id, doc, title=None, model="m"):
    with transaction(conn):
        conn.execute(
            "INSERT INTO embeddings(note_id, model, dim, doc_vec, title_vec) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(note_id) DO UPDATE SET "
            "doc_vec = excluded.doc_vec, title_vec = excluded.title_vec, model = excluded.model",
            (
                note_id,
                model,
                len(doc),
                vector_to_blob(doc),
                vector_to_blob(title) if title is not None else None,
            ),
        )
        bump_counter(conn, EMBEDDING_GENERATION_KEY)


def test_vector_search_exact_cosine_and_title_rescue(db_path):
    conn = connect(db_path)
    for note_id in ("a", "b", "c"):
        _add_note(conn, note_id, note_id)
    _store_vector(conn, "a", [1, 0, 0])
    _store_vector(conn, "b", [0, 1, 0], title=[0.9, 0.1, 0])  # title close to query
    _store_vector(conn, "c", [0, 0, 1])
    index = VectorIndex("cosine")
    result = index.search(conn, [1, 0, 0], "m", k=2)
    assert list(result) == ["a", "b"]
    assert result["a"] == pytest.approx(0.0, abs=1e-6)
    assert result["b"] == pytest.approx(1 - 0.9 / np.linalg.norm([0.9, 0.1]), abs=1e-5)
    conn.close()


def test_vector_cache_reloads_when_another_connection_writes(db_path):
    reader = connect(db_path)
    writer = connect(db_path)
    _add_note(writer, "a", "a")
    _add_note(writer, "b", "b")
    _store_vector(writer, "a", [1, 0])
    index = VectorIndex("cosine")
    assert list(index.search(reader, [0, 1], "m", k=5)) == ["a"]
    _store_vector(writer, "b", [0, 1])
    assert list(index.search(reader, [0, 1], "m", k=5))[0] == "b"
    assert index.loaded_count() == 2
    reader.close()
    writer.close()


def test_vector_search_ignores_other_models_and_deleted_notes(db_path):
    conn = connect(db_path)
    _add_note(conn, "a", "a")
    _add_note(conn, "b", "b")
    _store_vector(conn, "a", [1, 0], model="old")
    _store_vector(conn, "b", [1, 0], model="m")
    index = VectorIndex("cosine")
    assert list(index.search(conn, [1, 0], "m", k=5)) == ["b"]
    with transaction(conn):
        conn.execute("DELETE FROM notes WHERE id = 'b'")  # cascades to embeddings
        bump_counter(conn, EMBEDDING_GENERATION_KEY)
    assert index.search(conn, [1, 0], "m", k=5) == {}
    conn.close()


@pytest.mark.parametrize(
    "metric,expected",
    [("l2", ["a", "b"]), ("dotproduct", ["b", "a"])],
)
def test_other_metrics(db_path, metric, expected):
    conn = connect(db_path)
    _add_note(conn, "a", "a")
    _add_note(conn, "b", "b")
    _store_vector(conn, "a", [1, 0])
    _store_vector(conn, "b", [3, 0])
    index = VectorIndex(metric)
    assert list(index.search(conn, [1, 0], "m", k=2)) == expected
    conn.close()


def test_invalid_metric_is_rejected():
    with pytest.raises(ValueError):
        VectorIndex("hamming")
