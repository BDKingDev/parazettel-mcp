"""Tests for content-aware find_similar_notes (lexical signal, not tags/links only)."""

from parazettel_mcp.models.schema import LinkType, NoteType


def test_similarity_matches_on_content_without_shared_tags_or_links(zettel_service):
    """Two notes about the same topic match on content even with no shared tags/links.

    This is the gap the old tag+link-only scorer missed: a brand-new note with
    generic/disjoint tags and no links scored 0 against everything.
    """
    a = zettel_service.create_note(
        title="Kubernetes rolling deployments",
        content=(
            "A rolling deployment in Kubernetes updates pods incrementally so the "
            "cluster stays available during a release."
        ),
        note_type=NoteType.PERMANENT,
        tags=["alpha"],  # deliberately disjoint tags
    )
    b = zettel_service.create_note(
        title="Canary release strategy",
        content=(
            "A canary deployment shifts a small fraction of traffic to new pods in "
            "the Kubernetes cluster before a full rolling release."
        ),
        note_type=NoteType.PERMANENT,
        tags=["beta"],  # no tag overlap with a
    )
    unrelated = zettel_service.create_note(
        title="Sourdough starter care",
        content="Feed the starter with flour and water on a daily schedule.",
        note_type=NoteType.PERMANENT,
        tags=["gamma"],
    )

    results = zettel_service.find_similar_notes(a.id, threshold=0.1)
    ids = [n.id for n, _score in results]

    assert b.id in ids, "content-similar note should be found despite no shared tags/links"
    assert unrelated.id not in ids
    # The matched note carries a positive score.
    score = dict((n.id, s) for n, s in results)[b.id]
    assert score > 0.0


def test_similarity_still_finds_structural_matches(zettel_service):
    """A shared-tag/linked note is still returned (structural signal preserved)."""
    a = zettel_service.create_note(
        title="Topic A",
        content="Body about widgets.",
        note_type=NoteType.PERMANENT,
        tags=["shared-tag", "x"],
    )
    b = zettel_service.create_note(
        title="Topic B",
        content="Entirely different subject matter here.",
        note_type=NoteType.PERMANENT,
        tags=["shared-tag", "y"],
    )
    zettel_service.create_link(a.id, b.id, LinkType.RELATED)

    results = zettel_service.find_similar_notes(a.id, threshold=0.1)
    ids = [n.id for n, _score in results]

    assert b.id in ids


def test_similarity_ignores_link_section_ids_in_content(zettel_service):
    """The generated ## Links section must not drive lexical overlap.

    Two notes that link to the same target share a [[id]] in their rendered
    ## Links, but that is structural, not topical — content tokens should exclude
    it so unrelated bodies don't look similar just because they cite the same note.
    """
    target = zettel_service.create_note(
        title="Shared target",
        content="Target body.",
        note_type=NoteType.PERMANENT,
        tags=["t"],
    )
    a = zettel_service.create_note(
        title="Photosynthesis overview",
        content="Plants convert sunlight into chemical energy in chloroplasts.",
        note_type=NoteType.PERMANENT,
        tags=["bio"],
    )
    b = zettel_service.create_note(
        title="Tax filing deadlines",
        content="Quarterly estimated payments are due in April, June, September, January.",
        note_type=NoteType.PERMANENT,
        tags=["finance"],
    )
    zettel_service.create_link(a.id, target.id, LinkType.REFERENCE)
    zettel_service.create_link(b.id, target.id, LinkType.REFERENCE)

    # a and b share an outgoing link to `target` (structural), but their bodies are
    # unrelated. The link target id must not leak into the lexical token set, so a
    # high content-similarity match between a and b should not occur.
    tokens_a = zettel_service._content_tokens(zettel_service.get_note(a.id))
    assert target.id not in tokens_a
    assert "photosynthesis" in tokens_a
