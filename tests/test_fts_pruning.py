"""Tests for reducing a long FTS query to its most discriminative terms.

A long query with many moderately-common terms makes Kuzu's FTS match a large
fraction of the corpus and drop even the best match. Keeping only the lowest-DF
(highest-IDF) terms bounds the match set so the best lexical hit stays findable.
"""

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import Note, NoteType

# Eight terms that will appear in most notes (high DF); six that appear in only
# the single distinctive note (DF 1 -> always kept by the focus reduction).
_COMMON_CONTENT = "alpha beta gamma delta epsilon zeta eta theta"
_DISTINCTIVE_TERMS = "wombat narwhal axolotl quokka pangolin tapir"


def _seed_dense_corpus(repo, common_count=8, filler_count=24):
    """A corpus where the common terms are frequent but still < half the notes
    (so they keep a positive BM25 IDF, unlike a tiny corpus), plus one note that
    carries the distinctive terms.

    Returns the id of the single distinctive note.
    """
    # Filler notes keep N large enough that the common terms stay below the
    # negative-IDF (>half the corpus) line, so their document frequency is real.
    for i in range(filler_count):
        repo.create(
            Note(
                title=f"Filler {i}",
                content=f"unrelated filler body number{i} padding prose",
                note_type=NoteType.PERMANENT,
            )
        )
    for i in range(common_count):
        repo.create(
            Note(
                title=f"Common note {i}",
                content=_COMMON_CONTENT,
                note_type=NoteType.PERMANENT,
            )
        )
    distinctive = repo.create(
        Note(
            title="The distinctive one",
            content=_DISTINCTIVE_TERMS,
            note_type=NoteType.PERMANENT,
        )
    )
    return distinctive.id


def _with_df(repo, monkeypatch, df_map):
    """Drive _focus_query_terms with controlled document frequencies.

    Real single-term DF goes through Kuzu's FTS, whose tiny-corpus quirks (a
    term that's first in its only note can read as DF 0) would make a logic test
    flaky. Mocking DF isolates the reduction logic; the real DF path is covered
    end-to-end below and verified live on the production vault.
    """
    monkeypatch.setattr(
        repo, "_term_document_frequency", lambda conn, term: df_map.get(term, 0)
    )


def test_focus_keeps_lowest_df_drops_highest(note_repository, monkeypatch):
    repo = note_repository
    # 14 distinct terms (> fts_max_query_terms=12): 6 rare + 8 common.
    rare = "wombat narwhal axolotl quokka pangolin tapir"
    common = "alpha beta gamma delta epsilon zeta eta theta"
    df_map = {t: 1 for t in rare.split()}
    df_map.update({t: 500 for t in common.split()})
    df_map["alpha"] = 999  # the single most common -> first to be dropped
    _with_df(repo, monkeypatch, df_map)
    with repo._connection() as conn:
        focused = repo._focus_query_terms(conn, f"{common} {rare}")
    kept = set(focused.lower().split())
    for term in rare.split():
        assert term in kept, term  # every lowest-DF term survives
    assert len(kept) == config.fts_max_query_terms  # reduced to the budget
    assert "alpha" not in kept  # the most common term is dropped first


def test_short_query_is_returned_untouched(note_repository):
    repo = note_repository
    _seed_dense_corpus(repo)
    with repo._connection() as conn:
        short = "alpha beta gamma"  # <= fts_max_query_terms, never reduced
        assert repo._focus_query_terms(conn, short) == short


def test_reduction_disabled_when_max_terms_is_zero(note_repository, monkeypatch):
    monkeypatch.setattr(config, "fts_max_query_terms", 0)
    repo = note_repository
    _seed_dense_corpus(repo)
    query = f"{_COMMON_CONTENT} {_DISTINCTIVE_TERMS} extra padding tokens here now"
    with repo._connection() as conn:
        assert repo._focus_query_terms(conn, query) == query


def test_df_zero_terms_do_not_consume_the_budget(note_repository, monkeypatch):
    """Tokens in no note (DF 0) can't help and are dropped in favour of real ones."""
    repo = note_repository
    real = "wombat narwhal axolotl quokka pangolin tapir"
    padding = "qa qb qc qd qe qf qg qh qi qj qk ql qm"  # 13 DF-0 tokens
    df_map = {t: 1 for t in real.split()}  # padding terms default to DF 0
    _with_df(repo, monkeypatch, df_map)
    with repo._connection() as conn:
        focused = repo._focus_query_terms(conn, f"{padding} {real}")
    kept = set(focused.lower().split())
    for term in real.split():
        assert term in kept, term  # real terms kept over DF-0 noise
    assert not (set(padding.split()) & kept)  # no DF-0 padding survives


def test_long_common_query_still_finds_the_distinctive_note(note_repository):
    """End-to-end: a long query whose rare terms are buried still surfaces them."""
    repo = note_repository
    distinctive_id = _seed_dense_corpus(repo)
    query = f"{_COMMON_CONTENT} {_DISTINCTIVE_TERMS}"
    notes, scores = repo.search_scored(text=query)
    ids = [n.id for n in notes]
    assert distinctive_id in ids
    assert scores.get(distinctive_id, 0.0) > 0.0
