"""Tests for the AI-memory upgrade set: inline wiki-links, durable link
timestamps, ## Links reconciliation, tag normalization, provenance fields,
and retrieval signals."""

import datetime

import pytest

from parazettel_mcp.daemon.server import ALLOWED_SERVICE_METHODS
from parazettel_mcp.models.schema import LinkType, normalize_tag
from parazettel_mcp.services.zettel_service import ZettelService


# ---------------------------------------------------------------------------
# Tag normalization
# ---------------------------------------------------------------------------


def test_normalize_tag_collapses_common_variants():
    assert normalize_tag("ADD-ADHD") == "add-adhd"
    assert normalize_tag("emotional regulation") == "emotional-regulation"
    assert normalize_tag("structure_note") == "structure-note"
    assert normalize_tag("  Hubs  ") == "hubs"
    assert normalize_tag("a--b") == "a-b"
    # GTD context tags keep their @ prefix.
    assert normalize_tag("@Home") == "@home"
    assert normalize_tag("") == ""
    assert normalize_tag("   ") == ""


def test_tags_normalized_on_create_and_update(zettel_service):
    note = zettel_service.create_note(
        title="Tag normalization probe",
        content="Body.",
        tags=["Emotion Regulation", "emotion-regulation", "VA", "@Computer"],
    )
    names = [tag.name for tag in note.tags]
    # Case/separator variants converge and de-duplicate.
    assert names == ["emotion-regulation", "va", "@computer"]

    updated = zettel_service.update_note(note.id, tags=["New_Tag", "new tag"])
    assert [tag.name for tag in updated.tags] == ["new-tag"]


def test_get_notes_by_tag_unions_raw_and_normalized(zettel_service):
    """A mixed vault — a legacy note under a raw 'AI' tag plus a current note
    under the normalized 'ai' — must surface BOTH when queried by the raw
    spelling, not just whichever set matches first."""
    from parazettel_mcp.models.schema import Note, NoteType, Tag

    # Current note: normalized to 'ai' on write.
    norm = zettel_service.create_note(title="Norm", content="n", tags=["AI"])
    assert "ai" in {t.name for t in zettel_service.get_note(norm.id).tags}
    # Legacy note seeded directly with the raw 'AI' tag (pre-normalization).
    legacy = Note(
        title="Legacy", content="l", note_type=NoteType.FLEETING,
        tags=[Tag(name="AI")],
    )
    zettel_service.repository.create(legacy)

    ids = {n.id for n in zettel_service.get_notes_by_tag("AI")}
    assert {norm.id, legacy.id} <= ids


# ---------------------------------------------------------------------------
# Inline prose wiki-links
# ---------------------------------------------------------------------------


def test_inline_ref_is_indexed_as_graph_edge(zettel_service):
    target = zettel_service.create_note(title="Inline Target", content="T.")
    source = zettel_service.create_note(
        title="Inline Source",
        content=f"This prose mentions [[{target.id}]] in passing.",
    )

    # The prose-only reference is visible to graph traversal in both directions.
    incoming = zettel_service.get_linked_notes(target.id, "incoming")
    assert source.id in {n.id for n in incoming}
    outgoing = zettel_service.get_linked_notes(source.id, "outgoing")
    assert target.id in {n.id for n in outgoing}

    # The file-parsed note exposes it as inline_refs, not as a ## Links entry.
    parsed = zettel_service.get_note(source.id)
    assert target.id in parsed.inline_refs
    assert target.id not in {link.target_id for link in parsed.links}
    assert "## Links" not in parsed.content or target.id not in parsed.content.split(
        "## Links", 1
    )[1]


def test_inline_ref_scrubbed_on_delete(zettel_service):
    target = zettel_service.create_note(title="Doomed Note", content="T.")
    source = zettel_service.create_note(
        title="Referencing Note",
        content=(
            f"Bare ref [[{target.id}]] and aliased ref "
            f"[[{target.id}|a readable alias]] here."
        ),
    )

    zettel_service.delete_note(target.id)

    parsed = zettel_service.get_note(source.id)
    # No dangling reference remains anywhere in the body...
    assert target.id not in parsed.content
    assert parsed.inline_refs == []
    # ...but the prose stays readable: alias text survives, bare refs become
    # the deleted note's title.
    assert "a readable alias" in parsed.content
    assert "Doomed Note" in parsed.content


def test_inline_alias_refreshed_on_rename(zettel_service):
    target = zettel_service.create_note(title="Old Title", content="T.")
    source = zettel_service.create_note(
        title="Aliased Referencer",
        content=f"See [[{target.id}|Old Title]] for details.",
    )

    zettel_service.update_note(target.id, title="New Title")

    parsed = zettel_service.get_note(source.id)
    assert f"[[{target.id}|New Title]]" in parsed.content
    assert "Old Title" not in parsed.content


def test_inline_ref_with_fragment_or_md_suffix_scrubbed_on_delete(zettel_service):
    """An inline ref normalizes to the target id even with a #fragment or .md
    suffix, so delete must scrub all those forms — not just bare/aliased ones."""
    target = zettel_service.create_note(title="Suffixed Target", content="T.")
    source = zettel_service.create_note(
        title="Suffix Referencer",
        content=(
            f"Frag [[{target.id}#a-heading]], md [[{target.id}.md]], and "
            f"frag-alias [[{target.id}#sec|see here]] all point here."
        ),
    )
    # All three forms were indexed as an inline edge to the target.
    assert target.id in zettel_service.get_note(source.id).inline_refs

    zettel_service.delete_note(target.id)

    parsed = zettel_service.get_note(source.id)
    # No form of the reference survives in prose...
    assert target.id not in parsed.content
    assert parsed.inline_refs == []
    # ...and the aliased form leaves its readable alias behind.
    assert "see here" in parsed.content


def test_inline_alias_with_fragment_refreshed_on_rename(zettel_service):
    target = zettel_service.create_note(title="Old Title", content="T.")
    source = zettel_service.create_note(
        title="Fragment Referencer",
        content=f"See [[{target.id}#intro|Old Title]] for details.",
    )

    zettel_service.update_note(target.id, title="New Title")

    parsed = zettel_service.get_note(source.id)
    # The alias is refreshed AND the #fragment is preserved.
    assert f"[[{target.id}#intro|New Title]]" in parsed.content
    assert "Old Title" not in parsed.content


def test_create_link_rejects_inline_type(zettel_service):
    a = zettel_service.create_note(title="A", content="a")
    b = zettel_service.create_note(title="B", content="b")
    with pytest.raises(ValueError, match="inline"):
        zettel_service.create_link(a.id, b.id, link_type=LinkType.INLINE)


def test_consistency_reports_dangling_refs(zettel_service):
    ghost_id = "20990101T000000000000000"
    note = zettel_service.create_note(
        title="Dangler", content=f"Mentions [[{ghost_id}]] which never existed."
    )
    report = zettel_service.check_consistency()
    assert f"{note.id} -> {ghost_id}" in report["dangling_refs"]
    # Dangling refs are informational; they do not flip file/index consistency.
    assert report["consistent"]


# ---------------------------------------------------------------------------
# Durable link timestamps + ## Links reconciliation
# ---------------------------------------------------------------------------


def test_link_created_at_survives_file_roundtrip_and_rebuild(zettel_service):
    a = zettel_service.create_note(title="Link Source", content="a")
    b = zettel_service.create_note(title="Link Target", content="b")
    linked, _ = zettel_service.create_link(a.id, b.id, LinkType.EXTENDS)
    original = next(
        link for link in linked.links if link.target_id == b.id
    ).created_at.replace(microsecond=0)

    # The timestamp is persisted in the markdown itself...
    parsed = zettel_service.get_note(a.id)
    file_link = next(link for link in parsed.links if link.target_id == b.id)
    assert file_link.created_at.replace(microsecond=0) == original
    # ...as an HTML comment, which is stripped from the description.
    assert file_link.description is None

    # And it survives a full index rebuild (the old provenance-reset bug).
    zettel_service.rebuild_index()
    rebuilt = zettel_service.get_note(a.id)
    rebuilt_link = next(link for link in rebuilt.links if link.target_id == b.id)
    assert rebuilt_link.created_at.replace(microsecond=0) == original


def test_links_section_edit_is_reconciled_on_update(zettel_service):
    a = zettel_service.create_note(title="Reconcile Me", content="a")
    b = zettel_service.create_note(title="Keep Link", content="b")
    c = zettel_service.create_note(title="Drop Link", content="c")
    d = zettel_service.create_note(title="Add Link", content="d")
    zettel_service.create_link(a.id, b.id, LinkType.REFERENCE)
    zettel_service.create_link(a.id, c.id, LinkType.REFERENCE)
    kept_created_at = next(
        link
        for link in zettel_service.get_note(a.id).links
        if link.target_id == b.id
    ).created_at

    # Hand-edit the ## Links section: keep B, drop C, add D (supports).
    new_content = (
        "# Reconcile Me\n\nUpdated body.\n\n## Links\n"
        f"- reference [[{b.id}]]\n"
        f"- supports [[{d.id}]] hand-written\n"
    )
    zettel_service.update_note(a.id, content=new_content)

    parsed = zettel_service.get_note(a.id)
    by_target = {link.target_id: link for link in parsed.links}
    assert set(by_target) == {b.id, d.id}
    assert by_target[d.id].link_type == LinkType.SUPPORTS
    assert by_target[d.id].description == "hand-written"
    # The surviving link keeps its original creation time.
    assert by_target[b.id].created_at.replace(microsecond=0) == (
        kept_created_at.replace(microsecond=0)
    )
    # The graph agrees with the file.
    outgoing_ids = {
        n.id for n in zettel_service.get_linked_notes(a.id, "outgoing")
    }
    assert outgoing_ids == {b.id, d.id}


def test_content_without_links_section_keeps_links(zettel_service):
    a = zettel_service.create_note(title="Body Edit", content="a")
    b = zettel_service.create_note(title="Stable Target", content="b")
    zettel_service.create_link(a.id, b.id, LinkType.REFERENCE)

    zettel_service.update_note(a.id, content="A brand new body, no links section.")

    parsed = zettel_service.get_note(a.id)
    assert b.id in {link.target_id for link in parsed.links}


# ---------------------------------------------------------------------------
# Provenance + verification fields
# ---------------------------------------------------------------------------


def test_origin_and_last_verified_roundtrip(zettel_service):
    note = zettel_service.create_note(
        title="Provenance Note",
        content="Body.",
        origin="chat:session-abc123",
    )
    assert zettel_service.get_note(note.id).origin == "chat:session-abc123"

    today = datetime.date.today()
    zettel_service.update_note(note.id, last_verified=today)
    parsed = zettel_service.get_note(note.id)
    assert parsed.last_verified == today
    assert parsed.origin == "chat:session-abc123"

    # Searchable via the graph too (round-trips through the index).
    indexed = [n for n in zettel_service.get_all_notes() if n.id == note.id][0]
    assert indexed.origin == "chat:session-abc123"
    assert indexed.last_verified == today


# ---------------------------------------------------------------------------
# Embedding text hygiene
# ---------------------------------------------------------------------------


def test_embedding_text_strips_links_section(zettel_service):
    """The rendered ## Links section must not pollute a note's semantic vector
    — a container note (area/hub) would otherwise embed as a soup of member
    titles instead of its own meaning."""
    area = zettel_service.create_area_note(title="Embed Area", content="About X.")
    member = zettel_service.create_note(
        title="A Very Distinctive Member Title", content="m", area_id=area.id
    )
    area_note = zettel_service.get_note(area.id)
    # The file content contains the membership line...
    assert f"has_part [[{member.id}" in area_note.content
    # ...but the embedded text does not.
    embed_text = zettel_service.repository._embedding_text(area_note)
    assert "About X." in embed_text
    assert member.id not in embed_text
    assert "Distinctive Member Title" not in embed_text


def test_embedding_text_keeps_prose_mention_of_links_heading(zettel_service):
    """Only an actual ## Links heading line is stripped — the literal text in
    prose must not truncate the embedded body."""
    note = zettel_service.create_note(
        title="Meta Note",
        content=(
            "The phrase ## Links in running prose is not a heading and the "
            "important keyword zebracorn must survive embedding."
        ),
    )
    embed_text = zettel_service.repository._embedding_text(
        zettel_service.get_note(note.id)
    )
    assert "zebracorn" in embed_text


def test_to_markdown_excludes_inline_links(zettel_service):
    """Note.to_markdown (used by export) must not render derived INLINE links
    into the ## Links section."""
    from parazettel_mcp.models.schema import LinkType as _LT
    from parazettel_mcp.models.schema import Note, Tag

    note = Note(title="Exportable", content="# Exportable\n\nBody.")
    note.add_link("20260101T000000000000009", _LT.INLINE)
    note.add_link("20260101T000000000000010", _LT.REFERENCE)
    md = note.to_markdown()
    assert "20260101T000000000000010" in md  # reference rendered
    assert "20260101T000000000000009" not in md  # inline NOT rendered
    assert "inline" not in md


# ---------------------------------------------------------------------------
# Retrieval signals
# ---------------------------------------------------------------------------


def test_retrieval_signals_recorded_and_survive_rebuild(zettel_service):
    note = zettel_service.create_note(title="Hot Note", content="Body.")

    zettel_service.record_retrieval([note.id])
    zettel_service.record_retrieval([note.id])
    signals = zettel_service.get_retrieval_signals([note.id])
    assert signals[note.id]["hit_count"] == 2
    assert signals[note.id]["last_retrieved_at"] is not None

    # Signals live only in the graph, so the rebuild must carry them over.
    zettel_service.rebuild_index()
    carried = zettel_service.get_retrieval_signals([note.id])
    assert carried[note.id]["hit_count"] == 2

    # And reading a note never rewrites its markdown file.
    before = (zettel_service.repository.notes_dir / f"{note.id}.md").read_text(
        encoding="utf-8"
    )
    zettel_service.record_retrieval([note.id])
    after = (zettel_service.repository.notes_dir / f"{note.id}.md").read_text(
        encoding="utf-8"
    )
    assert before == after


# ---------------------------------------------------------------------------
# Daemon RPC allowlist
# ---------------------------------------------------------------------------


def test_daemon_allowlist_includes_new_service_methods():
    """Direct-mode tests don't exercise the allowlist; assert membership so a
    daemon-mode 'Unsupported RPC method' can't surface only at runtime."""
    zettel_methods = ALLOWED_SERVICE_METHODS["zettel_service"]
    assert "record_retrieval" in zettel_methods
    assert "get_retrieval_signals" in zettel_methods
    # Every public ZettelService method an MCP tool calls must be allowlisted.
    for method in ("find_similar_to_text", "check_consistency", "create_link"):
        assert method in zettel_methods
        assert callable(getattr(ZettelService, method))
