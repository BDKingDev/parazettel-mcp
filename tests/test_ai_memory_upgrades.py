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
    """normalize_tag collapses case/separator variants and preserves the @ GTD prefix."""
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
    """Tags are normalized and de-duplicated on note create and update."""
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
    """An inline [[id]] prose ref is indexed as a graph edge in both directions."""
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


@pytest.mark.parametrize(
    "template, indexed",
    [
        ("~~~\nexample [[{id}]]\n~~~\n", False),
        ("````\n```\nexample [[{id}]]\n```\n````\n", False),
        ("Use ```x``` inline, then see [[{id}]].\n", True),
        ("## Links to read later\n- [[{id}]]\n", True),
    ],
    ids=["tilde-fence", "long-fence", "code-span", "longer-heading"],
)
def test_inline_refs_follow_fence_and_heading_rules(zettel_service, template, indexed):
    """Inline refs use the Links section's rules: fenced examples are not refs,
    while a code span or a heading that only starts with "Links" is prose."""
    target = zettel_service.create_note(title="Inline Target", content="T.")
    source = zettel_service.create_note(
        title="Inline Source", content="Body.\n\n" + template.format(id=target.id)
    )

    outgoing = zettel_service.get_linked_notes(source.id, "outgoing")
    assert (target.id in {n.id for n in outgoing}) is indexed
    zettel_service.rebuild_index()
    outgoing = zettel_service.get_linked_notes(source.id, "outgoing")
    assert (target.id in {n.id for n in outgoing}) is indexed


def test_inline_ref_scrubbed_on_delete(zettel_service):
    """Inline prose refs to a deleted note are scrubbed, leaving readable alias/title text."""
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
    """An aliased inline ref is rewritten to the target's new title on rename."""
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
    """An aliased inline ref with a #fragment is refreshed on rename, preserving the fragment."""
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
    """create_link rejects the derived INLINE link type."""
    a = zettel_service.create_note(title="A", content="a")
    b = zettel_service.create_note(title="B", content="b")
    with pytest.raises(ValueError, match="inline"):
        zettel_service.create_link(a.id, b.id, link_type=LinkType.INLINE)


def test_consistency_reports_dangling_refs(zettel_service):
    """check_consistency reports wiki refs whose target no longer exists."""
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
    """A link's created_at persists in markdown and survives a full rebuild."""
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
    """A hand-edited ## Links section in updated content is reconciled into the graph."""
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

    # Hand-edit the ## Links section: keep B (now with an edited description),
    # drop C, add D (supports).
    new_content = (
        "# Reconcile Me\n\nUpdated body.\n\n## Links\n"
        f"- reference [[{b.id}]] edited description\n"
        f"- supports [[{d.id}]] hand-written\n"
    )
    zettel_service.update_note(a.id, content=new_content)

    parsed = zettel_service.get_note(a.id)
    by_target = {link.target_id: link for link in parsed.links}
    assert set(by_target) == {b.id, d.id}
    assert by_target[d.id].link_type == LinkType.SUPPORTS
    assert by_target[d.id].description == "hand-written"
    # The surviving link honors the edited description...
    assert by_target[b.id].description == "edited description"
    # ...while keeping its original creation time.
    assert by_target[b.id].created_at.replace(microsecond=0) == (
        kept_created_at.replace(microsecond=0)
    )
    # The graph agrees with the file.
    outgoing_ids = {
        n.id for n in zettel_service.get_linked_notes(a.id, "outgoing")
    }
    assert outgoing_ids == {b.id, d.id}


def test_content_without_links_section_keeps_links(zettel_service):
    """Updating content with no ## Links heading leaves the note's links untouched."""
    a = zettel_service.create_note(title="Body Edit", content="a")
    b = zettel_service.create_note(title="Stable Target", content="b")
    zettel_service.create_link(a.id, b.id, LinkType.REFERENCE)

    zettel_service.update_note(a.id, content="A brand new body, no links section.")

    parsed = zettel_service.get_note(a.id)
    assert b.id in {link.target_id for link in parsed.links}


# Content that once erased a note's links in a real vault: it mentions the
# literal "## Links" mid-sentence but has no Links heading. Verbatim except the
# private report paths, which are replaced with <shared-docs>.
_PROSE_MENTION_CONTENT = """# Parazettel area and project reads can hide generated links while preserving their maps

At Parazettel candidate 1c4b5295fb4addbe670f3b507d5789bc07cb9c22, pzk_get_area and pzk_get_project use the existing Links-section stripper to suppress the generated ## Links section from displayed content. They retain the curated body and inline map references, show a hidden-link count when applicable, and point to pzk_get_note(include_links=true) or linked-note tools for access to links. Project/member and area/project summary lists remain separate and can still be large; this changes response presentation, not stored Markdown or graph links.

The archivist inspected the exact candidate and independently reran the focused integration test on Linux/Python 3.13: 1 passed. It uses the real service and repository with mocked MCP registration, not a real transport. The review reports 125 related passes and a negative control failing on the parent; clean merge-tree checks against exact cache and security candidates are static, not evidence that a combined SDK 2 build ran. Native Windows, a full suite, merged runtime and real transport were not independently tested for this change. Behavior for malformed handwritten Links sections remains the existing stripper's contract.

A later inspection found that the original stripper also suppressed handwritten prose under a Links heading, while the notice depended on counting dash-prefixed lines: a prose-only section could disappear without notice. Amendment 0a042491ce32fce9dc1995b95c9f54a57c569532 announces any suppressed Links section in area and project reads, points first to linked notes for direction and stored descriptions, and adds a notice for prose-only sections in default pzk_get_note reads. Full note reads retain the section; the original list-only and no-section note output is unchanged in the tested cases. The amended behavior does not change the underlying stripper's handling of malformed or fenced headings.

The archivist inspected the amendment diff and ran three focused Linux/Python 3.13 tests (3 passed), including real-service container coverage with mocked MCP registration. The independent reviewer reports 128 related tests passed and a previous-candidate control in which the two new prose-only tests failed but the exact-output compatibility test passed. Static merge-tree checks are not merged-runtime evidence. The reviewer did not independently run the full suite, Windows, real transport, bulk-output comparison or merged SDK 2 runtime; builder suite claims remain builder-attributed. These findings are for the named candidate, not a claim of deployment.

Sources: <shared-docs>/readable-containers-review.md; <shared-docs>/readable-amend-review.md; src/parazettel_mcp/server/mcp_server.py and tests at the stated commits. Intakes: parazettel-readable-containers-review-1c4b529; parazettel-readable-amend-review-0a04249.
"""

_EXAMPLE = "## Links\n- related [[000]]\n"
_FENCED_CONTENTS = {
    "fenced-backticks": f"# Fenced\n\nEdit links like this:\n\n```\n{_EXAMPLE}```\n\n",
    "fenced-tildes": f"# Fenced\n\nExample:\n~~~markdown\n{_EXAMPLE}~~~\n",
    # A longer fence can show a shorter one; the inner ``` doesn't close it.
    "fenced-nested": f"# Fenced\n\n````\n```\n{_EXAMPLE}```\n````\n",
    # Indented four spaces after a blank line, it's an indented code block.
    "indented-code": "# Fenced\n\nExample:\n\n"
    + "".join(f"    {line}\n" for line in _EXAMPLE.splitlines())
    + "\n",
}
_FENCED_CONTENTS = {k: v + "After the fence.\n" for k, v in _FENCED_CONTENTS.items()}


@pytest.mark.parametrize(
    "content",
    [
        "The generated ## Links section is now hidden in reads.",
        "Intro.\n\n## Links to read later\n\nSome prose.",
        "Use `## Links` to edit links by hand.",
        _PROSE_MENTION_CONTENT,
        *_FENCED_CONTENTS.values(),
    ],
    ids=[
        "prose-mention",
        "longer-heading",
        "code-span",
        "archivist-verbatim",
        *_FENCED_CONTENTS,
    ],
)
def test_links_text_that_is_not_a_heading_keeps_links(zettel_service, content):
    """Only a real ## Links heading line edits links; any other "## Links" text is
    prose, and every written link keeps its type and description."""
    area = zettel_service.create_area_note(title="Area", content="a")
    a = zettel_service.create_note(title="Source", content="a", area_id=area.id)
    b = zettel_service.create_note(title="Related", content="b")
    c = zettel_service.create_note(title="Supported", content="c")
    zettel_service.create_link(a.id, b.id, LinkType.RELATED, "why A relates to B")
    zettel_service.create_link(a.id, c.id, LinkType.SUPPORTS)

    zettel_service.update_note(a.id, content=content)

    note = zettel_service.get_note(a.id)
    written = {
        (link.target_id, link.link_type, link.description)
        for link in note.links
        if link.target_id != area.id  # the area's routing links
    }
    assert written == {
        (b.id, LinkType.RELATED, "why A relates to B"),
        (c.id, LinkType.SUPPORTS, None),
    }
    assert {n.id for n in zettel_service.get_linked_notes(a.id, "outgoing")} >= {
        b.id,
        c.id,
    }
    if content in _FENCED_CONTENTS.values():
        # The fenced example stays in the body and is not parsed as a link.
        assert "After the fence." in note.content
        assert "- related [[000]]" in note.content
        assert "000" not in {link.target_id for link in note.links}


def test_inline_refs_to_some_linked_notes_keep_all_links(zettel_service):
    a = zettel_service.create_note(title="Inline Source", content="a")
    b = zettel_service.create_note(title="Cited Inline", content="b")
    c = zettel_service.create_note(title="Not Cited", content="c")
    zettel_service.create_link(a.id, b.id, LinkType.RELATED, "kept")
    zettel_service.create_link(a.id, c.id, LinkType.SUPPORTS)

    zettel_service.update_note(a.id, content=f"New body citing [[{b.id}]] only.")

    targets = {link.target_id for link in zettel_service.get_note(a.id).links}
    assert {b.id, c.id} <= targets


@pytest.mark.parametrize("heading", ["## Links", "## links", "  ##  Links  "])
def test_real_links_heading_still_reconciles(zettel_service, heading):
    """A real heading (any case or spacing) is the Links section: its entries are
    the note's links, and an empty one clears them (routing links stay)."""
    area = zettel_service.create_area_note(title="Area", content="a")
    a = zettel_service.create_note(title="Edited", content="a", area_id=area.id)
    b = zettel_service.create_note(title="Old Target", content="b")
    d = zettel_service.create_note(title="New Target", content="d")
    zettel_service.create_link(a.id, b.id, LinkType.RELATED)

    zettel_service.update_note(
        a.id, content=f"Body.\n\n{heading}\n- supports [[{d.id}]] added\n"
    )
    note = zettel_service.get_note(a.id)
    targets = {link.target_id for link in note.links}
    assert d.id in targets and b.id not in targets
    assert note.content.count("## Links") == 1  # one rendered section, no duplicate

    zettel_service.update_note(a.id, content=f"Body.\n\n{heading}\n")
    targets = {link.target_id for link in zettel_service.get_note(a.id).links}
    assert targets == {area.id}  # only the routing links remain


def test_links_heading_detection_follows_fence_rules():
    from parazettel_mcp.storage.note_repository import has_links_heading

    assert has_links_heading("Body.\n\n## Links\n- related [[x]]")
    # A one-line ```x``` is a code span, not an opening fence.
    assert has_links_heading("Use ```x``` inline.\n\n## Links\n- related [[x]]")
    # An opener that never closes is plain text, not a fence.
    assert has_links_heading("Stray:\n~~~\n\n## Links\n- related [[x]]")
    # Inside a closed fence of either kind, the heading is an example.
    assert not has_links_heading("~~~\n## Links\n~~~\n")
    assert not has_links_heading("````\n```\n## Links\n```\n````\n")
    # Up to three spaces of indent is still a heading; four is indented code.
    assert has_links_heading("Body.\n\n   ## Links\n- related [[x]]")
    assert not has_links_heading("Example:\n\n    ## Links\n    - related [[x]]")
    assert not has_links_heading("Example:\n\n\t## Links\n")


def _link_pairs(zettel_service, note_id):
    note = zettel_service.get_note(note_id)
    return {(link.link_type, link.target_id) for link in note.links}


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_fenced_example_inside_links_section_is_not_a_link(zettel_service, fence):
    """A fenced block inside a real Links section is an example, not entries."""
    a = zettel_service.create_note(title="Edited", content="a")
    b = zettel_service.create_note(title="Real Target", content="b")
    x = zettel_service.create_note(title="Example Target", content="x")

    zettel_service.update_note(
        a.id,
        content=(
            f"Body.\n\n## Links\n- related [[{b.id}]] real\n\n"
            f"{fence}\n## Example\n- supports [[{x.id}]] example\n{fence}\n"
        ),
    )
    assert _link_pairs(zettel_service, a.id) == {(LinkType.RELATED, b.id)}

    zettel_service.rebuild_index()
    assert _link_pairs(zettel_service, a.id) == {(LinkType.RELATED, b.id)}


def test_unclosed_fence_does_not_hide_the_links_section(zettel_service):
    a = zettel_service.create_note(title="Stray Fence", content="a")
    b = zettel_service.create_note(title="Target", content="b")
    zettel_service.create_link(a.id, b.id, LinkType.RELATED, "survives")

    zettel_service.update_note(
        a.id, content="Body with a stray fence:\n```\nnot closed"
    )
    zettel_service.rebuild_index()

    note = zettel_service.get_note(a.id)
    assert b.id in {link.target_id for link in note.links}
    assert note.content.count("## Links") == 1


# ---------------------------------------------------------------------------
# Provenance + verification fields
# ---------------------------------------------------------------------------


def test_origin_and_last_verified_roundtrip(zettel_service):
    """The origin and last_verified fields round-trip through frontmatter and the graph."""
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


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_derived_text_keeps_body_after_fenced_links_example(zettel_service, fence):
    """Embedding text, dedup tokens and the read-side view all use the shared
    Links rule, so a fenced ``## Links`` example doesn't cut the body."""
    from parazettel_mcp.server.mcp_server import _strip_links_section

    content = (
        f"Intro.\n\n{fence}\n## Links\n- related [[x]] example\n{fence}\n\n"
        "After the example, zebracorn.\n"
    )
    note = zettel_service.create_note(title="Fence Example", content=content)
    note = zettel_service.get_note(note.id)

    assert "zebracorn" in zettel_service.repository._embedding_text(note)
    assert "zebracorn" in zettel_service._content_tokens(note)
    shown, hidden = _strip_links_section(note.content)
    assert "zebracorn" in shown and hidden == 0


def test_embedding_text_unchanged_for_a_written_note(zettel_service):
    """Notes the writer produces embed exactly as before the shared rule, so
    the change doesn't invalidate stored embeddings."""
    import re

    area = zettel_service.create_area_note(title="Embed Area", content="About X.")
    other = zettel_service.create_note(title="Other", content="o")
    note = zettel_service.create_note(
        title="Written", content="Line one.\n\n## Notes\nMore.", area_id=area.id
    )
    zettel_service.create_link(note.id, other.id, LinkType.SUPPORTS, "because")
    for current in (zettel_service.get_note(note.id), zettel_service.get_note(area.id)):
        body = re.split(r"(?m)^\s*##\s+Links\s*$", current.content, maxsplit=1)[0]
        expected = f"{current.title}\n\n{body}".strip()
        assert zettel_service.repository._embedding_text(current) == expected


def test_consistency_ignores_fenced_dangling_refs(zettel_service):
    """A wiki ref inside fenced code is an example, not a broken reference."""
    shown_id = "20990101T000000000000000"
    fenced_id = "20990101T000000000000001"
    note = zettel_service.create_note(
        title="Dangler",
        content=f"Mentions [[{shown_id}]].\n\n~~~\nexample [[{fenced_id}]]\n~~~\n",
    )
    report = zettel_service.check_consistency()
    assert f"{note.id} -> {shown_id}" in report["dangling_refs"]
    assert f"{note.id} -> {fenced_id}" not in report["dangling_refs"]


def test_to_markdown_excludes_inline_links():
    """Note.to_markdown (used by export) must not render derived INLINE links
    into the ## Links section."""
    from parazettel_mcp.models.schema import LinkType as _LT
    from parazettel_mcp.models.schema import Note

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
    """Retrieval signals are recorded, survive a rebuild, and never rewrite the note file."""
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
