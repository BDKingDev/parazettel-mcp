"""Regression tests for link <-> markdown synchronization.

Covers two integrity guarantees that are easy to silently break:

1. Deleting a note scrubs the structured ``## Links`` reference from any note that
   linked to it (both in the graph and in the persisted markdown body).
2. Re-routing a knowledge note to a different area updates its ``reference`` link
   to the area in the markdown ``## Links`` section, not just the frontmatter.
"""

from pathlib import Path

from parazettel_mcp.models.schema import LinkType, NoteType


def _links_section(notes_dir: Path, note_id: str) -> list[str]:
    """Return the stripped lines under the markdown ``## Links`` heading."""
    text = (notes_dir / f"{note_id}.md").read_text(encoding="utf-8")
    lines, inside = [], False
    for line in text.splitlines():
        if line.strip() == "## Links":
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside and line.strip():
            lines.append(line.strip())
    return lines


def test_delete_scrubs_links_section_reference(zettel_service):
    """Deleting a target removes the ## Links entry from its source note.

    Runs many iterations back-to-back (no sleeps) to guard against any
    cache/timing regression in the delete read-modify-write path.
    """
    notes_dir = zettel_service.repository.notes_dir
    for _ in range(25):
        area = zettel_service.create_note(
            "Area", "area body", note_type=NoteType.AREA
        )
        src = zettel_service.create_note(
            "Source", "source body", note_type=NoteType.PERMANENT, area_id=area.id
        )
        tgt = zettel_service.create_note(
            "Target", "target body", note_type=NoteType.PERMANENT, area_id=area.id
        )
        zettel_service.create_link(src.id, tgt.id, LinkType.SUPPORTS)

        # Precondition: the link is present in the source's ## Links section.
        assert any(tgt.id in line for line in _links_section(notes_dir, src.id))

        zettel_service.delete_note(tgt.id)

        # The graph edge is gone...
        outgoing = zettel_service.repository.find_linked_notes(src.id, "outgoing")
        assert all(n.id != tgt.id for n in outgoing)
        # ...and so is the markdown ## Links entry (no dangling reference).
        assert all(
            tgt.id not in line for line in _links_section(notes_dir, src.id)
        )


def test_update_note_area_change_syncs_reference_link(zettel_service):
    """Changing a note's area_id rewrites the area reference link in markdown."""
    notes_dir = zettel_service.repository.notes_dir
    area1 = zettel_service.create_note("Area One", "a1", note_type=NoteType.AREA)
    area2 = zettel_service.create_note("Area Two", "a2", note_type=NoteType.AREA)
    note = zettel_service.create_note(
        "Knowledge", "body", note_type=NoteType.PERMANENT, area_id=area1.id
    )

    # On create, the note references area1.
    created_links = _links_section(notes_dir, note.id)
    assert any(area1.id in line for line in created_links)

    # Re-route to area2.
    zettel_service.update_note(note.id, area_id=area2.id)

    updated_links = _links_section(notes_dir, note.id)
    # The stale area1 reference is gone and area2 is now referenced.
    assert all(area1.id not in line for line in updated_links), updated_links
    assert any(area2.id in line for line in updated_links), updated_links

    # The graph agrees with the markdown.
    outgoing = {
        n.id for n in zettel_service.repository.find_linked_notes(note.id, "outgoing")
    }
    assert area2.id in outgoing
    assert area1.id not in outgoing


def test_update_note_area_change_via_project(zettel_service):
    """Moving a note to a project in a different area updates its area reference."""
    notes_dir = zettel_service.repository.notes_dir
    area1 = zettel_service.create_note("PArea One", "a1", note_type=NoteType.AREA)
    area2 = zettel_service.create_note("PArea Two", "a2", note_type=NoteType.AREA)
    proj2 = zettel_service.create_note(
        "Project Two", "p2", note_type=NoteType.PROJECT, area_id=area2.id
    )
    note = zettel_service.create_note(
        "Routed", "body", note_type=NoteType.PERMANENT, area_id=area1.id
    )
    assert any(area1.id in line for line in _links_section(notes_dir, note.id))

    # Route under a project whose area is area2; area_id should follow to area2.
    updated = zettel_service.update_note(note.id, project_id=proj2.id)
    assert updated.area_id == area2.id

    links = _links_section(notes_dir, note.id)
    assert all(area1.id not in line for line in links), links
    assert any(area2.id in line for line in links), links
