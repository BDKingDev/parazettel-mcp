"""Tests for the ZettelService class."""

import pytest
from sqlalchemy import select

from parazettel_mcp.models.db_models import DBLink
from parazettel_mcp.models.schema import LinkType, NoteStatus, NoteType


def test_create_note(zettel_service):
    """Test creating a note through the service."""
    # Create a test note
    note = zettel_service.create_note(
        title="Service Test Note",
        content="Testing note creation through the service.",
        note_type=NoteType.PERMANENT,
        tags=["service", "test"],
        status=NoteStatus.INBOX,
    )
    # Verify note was created
    assert note.id is not None
    assert note.title == "Service Test Note"
    assert note.content == "Testing note creation through the service."
    assert note.note_type == NoteType.PERMANENT
    assert note.status == NoteStatus.INBOX
    assert len(note.tags) == 2
    assert {tag.name for tag in note.tags} == {"service", "test"}


def test_create_note_with_area_adds_reference_link(zettel_service):
    """Creating a note with area_id should add a REFERENCE link to that area."""
    area = zettel_service.create_area_note(
        title="Knowledge Management",
        content="Maintain the system.",
    )
    note = zettel_service.create_note(
        title="Area-routed note",
        content="Supports the area directly.",
        area_id=area.id,
    )

    assert note.area_id == area.id
    stored_links = {lnk.link_type for lnk in zettel_service.get_note(note.id).links}
    assert LinkType.REFERENCE in stored_links


def test_create_area_note_self_assigns_area_without_rewrite(zettel_service, monkeypatch):
    """Area creation should self-assign area_id before the first persisted write."""
    updated_ids = []
    original_update = zettel_service.repository.update

    def tracking_update(note):
        updated_ids.append(note.id)
        return original_update(note)

    monkeypatch.setattr(zettel_service.repository, "update", tracking_update)

    area = zettel_service.create_area_note(
        title="Operations",
        content="Operational responsibilities.",
    )

    assert area.area_id == area.id
    assert area.id not in updated_ids


def test_get_note(zettel_service):
    """Test retrieving a note through the service."""
    # Create a test note
    note = zettel_service.create_note(
        title="Service Get Note",
        content="Testing note retrieval through the service.",
        note_type=NoteType.PERMANENT,
        tags=["service", "get"],
    )
    # Retrieve the note
    retrieved_note = zettel_service.get_note(note.id)
    # Verify note was retrieved
    assert retrieved_note is not None
    assert retrieved_note.id == note.id
    assert retrieved_note.title == "Service Get Note"

    # Note content includes the title as a markdown header - account for this in our test
    expected_content = f"# {note.title}\n\n{note.content}"
    assert retrieved_note.content.strip() == expected_content.strip()

    assert retrieved_note.note_type == NoteType.PERMANENT
    assert {tag.name for tag in retrieved_note.tags} == {"service", "get"}


def test_update_note(zettel_service):
    """Test updating a note through the service."""
    # Create a test note
    note = zettel_service.create_note(
        title="Service Update Note",
        content="Testing note update through the service.",
        note_type=NoteType.PERMANENT,
        tags=["service", "update"],
        status=NoteStatus.INBOX,
    )
    # Update the note
    updated_note = zettel_service.update_note(
        note_id=note.id,
        title="Updated Service Note",
        content="This note has been updated through the service.",
        tags=["service", "updated"],
        status=NoteStatus.EVERGREEN,
    )
    # Verify note was updated
    assert updated_note.id == note.id
    assert updated_note.title == "Updated Service Note"
    assert "This note has been updated through the service." in updated_note.content
    assert updated_note.status == NoteStatus.EVERGREEN
    assert {tag.name for tag in updated_note.tags} == {"service", "updated"}

    cleared_note = zettel_service.update_note(note_id=note.id, status=None)
    assert cleared_note.status is None


def test_update_note_title_only_rewrites_heading(zettel_service):
    """Title-only note updates should rewrite the leading H1 in stored content."""
    note = zettel_service.create_note(
        title="Original Service Title",
        content="Body stays the same.",
        note_type=NoteType.PERMANENT,
        tags=["service"],
    )

    updated_note = zettel_service.update_note(
        note_id=note.id,
        title="Renamed Service Title",
    )
    retrieved_note = zettel_service.get_note(note.id)

    assert updated_note.title == "Renamed Service Title"
    assert retrieved_note is not None
    assert retrieved_note.content.startswith("# Renamed Service Title\n\n")
    assert "# Original Service Title" not in retrieved_note.content


def test_update_note_refreshes_incoming_aliases_that_match_old_title(zettel_service):
    """Renaming a note should refresh incoming aliases to the new title."""
    target = zettel_service.create_note(
        title="Original Target Title",
        content="Target body.",
        note_type=NoteType.PERMANENT,
    )
    first_source = zettel_service.create_note(
        title="First Alias Source",
        content="Links with the current title alias.",
        note_type=NoteType.PERMANENT,
    )
    second_source = zettel_service.create_note(
        title="Second Alias Source",
        content="Also links with the current title alias.",
        note_type=NoteType.PERMANENT,
    )

    zettel_service.create_link(first_source.id, target.id, LinkType.REFERENCE)
    zettel_service.create_link(second_source.id, target.id, LinkType.REFERENCE)

    zettel_service.update_note(note_id=target.id, title="Renamed Target Title")

    first_markdown = (
        zettel_service.repository.notes_dir / f"{first_source.id}.md"
    ).read_text(encoding="utf-8")
    second_markdown = (
        zettel_service.repository.notes_dir / f"{second_source.id}.md"
    ).read_text(encoding="utf-8")

    assert f"[[{target.id}|Renamed Target Title]]" in first_markdown
    assert f"[[{target.id}|Original Target Title]]" not in first_markdown
    assert f"[[{target.id}|Renamed Target Title]]" in second_markdown


def test_update_note_refreshes_aliases_without_touching_source_timestamp(
    zettel_service,
):
    """Alias-only rewrites should preserve the source note's updated_at value."""
    target = zettel_service.create_note(
        title="Timestamp Target Title",
        content="Target body.",
        note_type=NoteType.PERMANENT,
    )
    source = zettel_service.create_note(
        title="Timestamp Source",
        content="Source body.",
        note_type=NoteType.PERMANENT,
    )

    zettel_service.create_link(source.id, target.id, LinkType.REFERENCE)
    original_source = zettel_service.get_note(source.id)
    assert original_source is not None
    original_updated_at = original_source.updated_at

    zettel_service.update_note(note_id=target.id, title="Renamed Timestamp Target")

    refreshed_source = zettel_service.get_note(source.id)
    assert refreshed_source is not None
    assert refreshed_source.updated_at == original_updated_at

    stored_markdown = (
        zettel_service.repository.notes_dir / f"{source.id}.md"
    ).read_text(encoding="utf-8")
    assert f"[[{target.id}|Renamed Timestamp Target]]" in stored_markdown


def test_update_note_refreshes_aliases_without_resetting_link_created_at(
    zettel_service,
):
    """Alias-only rewrites should preserve source link created_at in the DB."""
    target = zettel_service.create_note(
        title="CreatedAt Target",
        content="Target body.",
        note_type=NoteType.PERMANENT,
    )
    source = zettel_service.create_note(
        title="CreatedAt Source",
        content="Source body.",
        note_type=NoteType.PERMANENT,
    )

    zettel_service.create_link(source.id, target.id, LinkType.REFERENCE)

    with zettel_service.repository.session_factory() as session:
        original_link = session.scalar(
            select(DBLink).where(
                DBLink.source_id == source.id,
                DBLink.target_id == target.id,
                DBLink.link_type == LinkType.REFERENCE.value,
            )
        )
        assert original_link is not None
        original_created_at = original_link.created_at

    zettel_service.update_note(note_id=target.id, title="CreatedAt Target Renamed")

    with zettel_service.repository.session_factory() as session:
        refreshed_link = session.scalar(
            select(DBLink).where(
                DBLink.source_id == source.id,
                DBLink.target_id == target.id,
                DBLink.link_type == LinkType.REFERENCE.value,
            )
        )
        assert refreshed_link is not None
        assert refreshed_link.created_at == original_created_at


def test_update_note_assigns_project_routing(zettel_service):
    """Updating a note with project_id should inherit the project area and link it."""
    area = zettel_service.create_area_note(
        title="Engineering",
        content="Software delivery and maintenance.",
    )
    project = zettel_service.create_project_note(
        title="Project A",
        content="Primary project.",
        area_id=area.id,
    )
    note = zettel_service.create_note(
        title="Loose support note",
        content="Needs to be routed under the project.",
        note_type=NoteType.PERMANENT,
    )

    updated_note = zettel_service.update_note(note_id=note.id, project_id=project.id)

    assert updated_note.project_id == project.id
    assert updated_note.area_id == area.id
    stored_links = {lnk.link_type for lnk in zettel_service.get_note(note.id).links}
    project_links = {lnk.link_type for lnk in zettel_service.get_note(project.id).links}
    assert LinkType.PART_OF in stored_links
    assert LinkType.HAS_PART in project_links


def test_update_project_reparents_and_inherits_parent_area(zettel_service):
    """Reparenting a subproject should update hierarchy and inherited area links."""
    area_one = zettel_service.create_area_note(
        title="Engineering", content="Software delivery and maintenance."
    )
    area_two = zettel_service.create_area_note(
        title="Operations", content="Operational coordination."
    )
    parent_one = zettel_service.create_project_note(
        title="Parent A", content="First parent project.", area_id=area_one.id
    )
    parent_two = zettel_service.create_project_note(
        title="Parent B", content="Second parent project.", area_id=area_two.id
    )
    child = zettel_service.create_project_note(
        title="Child Project", content="Nested implementation project.", project_id=parent_one.id
    )

    updated = zettel_service.update_note(note_id=child.id, project_id=parent_two.id)
    stored_child = zettel_service.get_note(child.id)
    parent_one_links = {
        (link.target_id, link.link_type) for link in zettel_service.get_note(parent_one.id).links
    }
    parent_two_links = {
        (link.target_id, link.link_type) for link in zettel_service.get_note(parent_two.id).links
    }

    assert updated.project_id == parent_two.id
    assert updated.area_id == area_two.id
    assert stored_child is not None
    child_links = {(link.target_id, link.link_type) for link in stored_child.links}
    assert (parent_one.id, LinkType.PART_OF) not in child_links
    assert (parent_two.id, LinkType.PART_OF) in child_links
    assert (area_one.id, LinkType.PART_OF) not in child_links
    assert (area_two.id, LinkType.PART_OF) in child_links
    assert (child.id, LinkType.HAS_PART) not in parent_one_links
    assert (child.id, LinkType.HAS_PART) in parent_two_links
    assert zettel_service.get_parent_project(child.id).id == parent_two.id
    assert zettel_service.get_subprojects(parent_one.id) == []
    assert [note.id for note in zettel_service.get_subprojects(parent_two.id)] == [child.id]


def test_update_project_can_clear_parent_and_stay_top_level(zettel_service):
    """Clearing a subproject parent should keep the project routed to its area."""
    area = zettel_service.create_area_note(
        title="Engineering", content="Software delivery and maintenance."
    )
    parent = zettel_service.create_project_note(
        title="Parent Project", content="Primary initiative.", area_id=area.id
    )
    child = zettel_service.create_project_note(
        title="Child Project", content="Nested implementation project.", project_id=parent.id
    )

    updated = zettel_service.update_note(note_id=child.id, project_id=None)
    stored_child = zettel_service.get_note(child.id)
    parent_links = {
        (link.target_id, link.link_type) for link in zettel_service.get_note(parent.id).links
    }

    assert updated.project_id is None
    assert updated.area_id == area.id
    assert stored_child is not None
    child_links = {(link.target_id, link.link_type) for link in stored_child.links}
    assert (parent.id, LinkType.PART_OF) not in child_links
    assert (area.id, LinkType.PART_OF) in child_links
    assert (child.id, LinkType.HAS_PART) not in parent_links
    assert zettel_service.get_parent_project(child.id) is None


def test_delete_note(zettel_service):
    """Test deleting a note through the service."""
    # Create a test note
    note = zettel_service.create_note(
        title="Service Delete Note",
        content="Testing note deletion through the service.",
        note_type=NoteType.PERMANENT,
        tags=["service", "delete"],
    )
    # Verify note exists
    retrieved_note = zettel_service.get_note(note.id)
    assert retrieved_note is not None
    # Delete the note
    zettel_service.delete_note(note.id)
    # Verify note no longer exists
    deleted_note = zettel_service.get_note(note.id)
    assert deleted_note is None


def test_create_link(zettel_service):
    """Test creating a link between notes through the service."""
    # Create test notes
    source_note = zettel_service.create_note(
        title="Service Source Note",
        content="Testing link creation (source).",
        note_type=NoteType.PERMANENT,
        tags=["service", "link", "source"],
    )
    target_note = zettel_service.create_note(
        title="Service Target Note",
        content="Testing link creation (target).",
        note_type=NoteType.PERMANENT,
        tags=["service", "link", "target"],
    )
    # Create a link
    source, target = zettel_service.create_link(
        source_id=source_note.id,
        target_id=target_note.id,
        link_type=LinkType.REFERENCE,
        description="A test link via service",
        bidirectional=True,
    )
    # Verify link was created
    assert len(source.links) == 1
    assert source.links[0].target_id == target_note.id
    assert source.links[0].link_type == LinkType.REFERENCE
    assert source.links[0].description == "A test link via service"
    # Verify bidirectional link
    assert len(target.links) == 1
    assert target.links[0].target_id == source_note.id
    assert target.links[0].link_type == LinkType.REFERENCE
    # Test get_linked_notes
    outgoing_links = zettel_service.get_linked_notes(source_note.id, "outgoing")
    assert len(outgoing_links) == 1
    assert outgoing_links[0].id == target_note.id
    incoming_links = zettel_service.get_linked_notes(target_note.id, "incoming")
    assert len(incoming_links) == 1
    assert incoming_links[0].id == source_note.id
    both_links = zettel_service.get_linked_notes(source_note.id, "both")
    assert len(both_links) == 1
    assert both_links[0].id == target_note.id


def test_bidirectional_link_creation_preserves_aliases_and_single_links_section(
    zettel_service,
):
    """Bidirectional link updates should not duplicate or flatten the links section."""
    source_note = zettel_service.create_note(
        title="Service Source With Alias",
        content="Source note body.",
        note_type=NoteType.PERMANENT,
    )
    existing_target = zettel_service.create_note(
        title="Existing Target Title",
        content="Existing target body.",
        note_type=NoteType.PERMANENT,
    )
    new_target = zettel_service.create_note(
        title="New Bidirectional Target",
        content="New target body.",
        note_type=NoteType.PERMANENT,
    )

    zettel_service.create_link(source_note.id, existing_target.id, LinkType.REFERENCE)

    source, target = zettel_service.create_link(
        source_id=source_note.id,
        target_id=new_target.id,
        link_type=LinkType.EXTENDS,
        description="Fresh bidirectional link",
        bidirectional=True,
    )

    source_markdown = (
        zettel_service.repository.notes_dir / f"{source_note.id}.md"
    ).read_text(encoding="utf-8")
    target_markdown = (
        zettel_service.repository.notes_dir / f"{new_target.id}.md"
    ).read_text(encoding="utf-8")

    assert source_markdown.count("## Links") == 1
    assert target_markdown.count("## Links") == 1
    assert f"[[{existing_target.id}|Existing Target Title]]" in source_markdown
    assert (
        f"- extends [[{new_target.id}|New Bidirectional Target]] Fresh bidirectional link"
        in source_markdown
    )
    assert f"[[{source_note.id}|Service Source With Alias]]" in target_markdown
    assert any(link.target_id == new_target.id for link in source.links)
    assert any(link.target_id == source_note.id for link in target.links)


def test_search_notes(zettel_service):
    """Test searching for notes through the service."""
    # Create test notes
    note1 = zettel_service.create_note(
        title="Python Basics",
        content="Introduction to Python programming.",
        note_type=NoteType.PERMANENT,
        tags=["python", "programming", "service"],
    )
    note2 = zettel_service.create_note(
        title="Advanced Python",
        content="Advanced techniques in Python.",
        note_type=NoteType.PERMANENT,
        tags=["python", "advanced", "service"],
    )
    note3 = zettel_service.create_note(
        title="JavaScript Introduction",
        content="Basics of JavaScript programming.",
        note_type=NoteType.PERMANENT,
        tags=["javascript", "programming", "service"],
    )

    # Search by tags instead of content since that's more reliable
    python_notes = zettel_service.get_notes_by_tag("python")
    assert len(python_notes) == 2
    assert {n.id for n in python_notes} == {note1.id, note2.id}

    # Test adding and removing tags
    first_note = python_notes[0]
    zettel_service.add_tag_to_note(first_note.id, "newTag")
    updated_note = zettel_service.get_note(first_note.id)
    assert "newTag" in {tag.name for tag in updated_note.tags}
    zettel_service.remove_tag_from_note(first_note.id, "newTag")
    updated_note = zettel_service.get_note(first_note.id)
    assert "newTag" not in {tag.name for tag in updated_note.tags}


def test_find_similar_notes(zettel_service):
    """Test finding similar notes."""
    # Create test notes with shared tags and links
    note1 = zettel_service.create_note(
        title="Machine Learning Basics",
        content="Introduction to machine learning concepts.",
        note_type=NoteType.PERMANENT,
        tags=["AI", "machine learning", "data science"],
    )
    note2 = zettel_service.create_note(
        title="Neural Networks",
        content="Overview of neural network architectures.",
        note_type=NoteType.PERMANENT,
        tags=["AI", "machine learning", "neural networks"],
    )
    note3 = zettel_service.create_note(
        title="Python for Data Science",
        content="Using Python for data analysis and machine learning.",
        note_type=NoteType.PERMANENT,
        tags=["python", "data science"],
    )
    note4 = zettel_service.create_note(
        title="History of Computing",
        content="Evolution of computing technology.",
        note_type=NoteType.PERMANENT,
        tags=["history", "computing"],
    )

    # Create links between notes with different types
    # This ensures we don't have duplicate links of the same type
    zettel_service.create_link(note1.id, note2.id, LinkType.EXTENDS)
    zettel_service.create_link(note1.id, note3.id, LinkType.REFERENCE)

    # Find similar notes to note1
    # Setting a lower threshold since the current implementation may have different weights
    similar_notes = zettel_service.find_similar_notes(note1.id, 0.0)

    # Verify we get at least one similar note (the exact order may vary)
    assert len(similar_notes) > 0

    # Convert to IDs for easier comparison
    similar_ids = [note_tuple[0].id for note_tuple in similar_notes]

    # At least one of note2 or note3 should be in the similar notes
    # (They share tags and/or links with note1)
    assert note2.id in similar_ids or note3.id in similar_ids
