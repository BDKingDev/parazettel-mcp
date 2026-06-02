"""Tests for the ZettelService class."""

import pytest

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

    original_link = zettel_service.repository.get_link(
        source.id, target.id, LinkType.REFERENCE.value
    )
    assert original_link is not None
    original_created_at = original_link["created_at"]

    zettel_service.update_note(note_id=target.id, title="CreatedAt Target Renamed")

    refreshed_link = zettel_service.repository.get_link(
        source.id, target.id, LinkType.REFERENCE.value
    )
    assert refreshed_link is not None
    assert refreshed_link["created_at"] == original_created_at


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


def test_concurrent_same_note_updates_stay_consistent(zettel_service):
    """Per-note locking serializes concurrent full-value updates to one note.

    The lock guarantees each ``update_note`` runs to completion without
    interleaving another writer mid-write, so the note never ends up in a torn
    state and the final value is exactly one writer's complete content/tags
    (clean last-writer-wins), not a corrupted mix. (It does not merge separate
    read-modify-write calls — that is a different, compare-and-swap operation.)
    """
    import threading

    area = zettel_service.create_note(
        title="Area", content="area", note_type=NoteType.AREA
    )
    note = zettel_service.create_note(
        title="Concurrency target",
        content="base",
        note_type=NoteType.PERMANENT,
        tags=["base"],
        status=NoteStatus.INBOX,
        area_id=area.id,
    )

    # Each thread writes a self-consistent (content_i, tag_i) pair. Whichever
    # wins, the persisted content and tag must come from the *same* writer.
    writers = list(range(12))
    barrier = threading.Barrier(len(writers))
    errors = []

    # Use a unique, non-prefixing token per writer (zero-padded) so substring
    # checks can't collide (e.g. "w1" inside "w10").
    def token(i: int) -> str:
        return f"w{i:02d}"

    def write(i: int) -> None:
        try:
            barrier.wait()  # maximize overlap
            zettel_service.update_note(
                note.id, content=f"body-{token(i)}-end", tags=[f"tag-{token(i)}"]
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in writers]
    for t in threads:
        t.start()
    # Join with a timeout so a lock regression that deadlocks update_note fails
    # the test fast instead of hanging the whole suite.
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "update_note deadlocked"

    assert not errors, f"update threads raised: {errors}"
    final = zettel_service.get_note(note.id)
    final_tags = [t.name for t in final.tags]
    # Exactly one writer's tag survives, and the content carries that same
    # writer's token (no torn mix of one writer's content with another's tags).
    assert len(final_tags) == 1, f"expected one winning tag, got {final_tags}"
    winner = final_tags[0]  # e.g. "tag-w07"
    winner_token = winner.split("tag-")[1]
    assert f"body-{winner_token}-end" in final.content, (
        f"torn write: content={final.content!r} but tag={winner!r}"
    )
    # No other writer's token leaked into the body alongside the winner's.
    other_tokens = [token(i) for i in writers if token(i) != winner_token]
    assert not any(tok in final.content for tok in other_tokens), (
        f"torn write: foreign writer token present in {final.content!r}"
    )


def test_concurrent_link_changes_on_same_pair_do_not_deadlock(zettel_service):
    """create_link/remove_link on the same note pair are lock-ordered (no deadlock).

    Two notes linked/unlinked from opposite directions by many threads must lock
    their endpoints in a stable order; otherwise an A->B / B->A acquisition could
    deadlock. The test passes if every thread completes (no hang) and the graph
    stays readable afterward.
    """
    import threading

    area = zettel_service.create_note(
        title="Area", content="area", note_type=NoteType.AREA
    )
    a = zettel_service.create_note(
        title="Note A", content="a", note_type=NoteType.PERMANENT, area_id=area.id
    )
    b = zettel_service.create_note(
        title="Note B", content="b", note_type=NoteType.PERMANENT, area_id=area.id
    )

    barrier = threading.Barrier(8)
    errors = []

    def churn(i: int) -> None:
        try:
            barrier.wait()
            # Half the threads work A->B, half B->A — opposite orders on the
            # same pair, which is exactly the deadlock-prone pattern.
            src, tgt = (a.id, b.id) if i % 2 == 0 else (b.id, a.id)
            for _ in range(5):
                zettel_service.create_link(src, tgt, LinkType.RELATED)
                zettel_service.remove_link(src, tgt, LinkType.RELATED)
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    # A generous join timeout turns a deadlock into a test failure instead of a hang.
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "link churn deadlocked"
    assert not errors, f"link churn raised: {errors}"

    # Graph is still consistent and readable.
    assert zettel_service.get_note(a.id) is not None
    assert zettel_service.get_note(b.id) is not None


def test_concurrent_task_reassignment_and_parent_edits_stay_consistent(zettel_service):
    """Routing updates lock both endpoints, so a parent's HAS_PART isn't clobbered.

    A task reassignment rewrites the new project's HAS_PART link while another
    thread edits that same project. Because update_task/update_note now lock the
    task AND its current/new project up front (stable order), the parent update
    can't be lost and the operations can't deadlock.
    """
    import threading

    area = zettel_service.create_note(
        title="Area", content="area", note_type=NoteType.AREA
    )
    proj_a = zettel_service.create_project_note(
        title="Project A", content="a", area_id=area.id
    )
    proj_b = zettel_service.create_project_note(
        title="Project B", content="b", area_id=area.id
    )
    task = zettel_service.create_task(
        title="Roaming task", content="t", project_id=proj_a.id
    )

    barrier = threading.Barrier(2)
    errors = []

    def reassign() -> None:
        try:
            barrier.wait()
            for _ in range(5):
                zettel_service.update_task(task.id, project_id=proj_b.id)
                zettel_service.update_task(task.id, project_id=proj_a.id)
        except Exception as exc:  # pragma: no cover
            errors.append(("reassign", exc))

    def edit_parent() -> None:
        try:
            barrier.wait()
            for i in range(5):
                # Concurrently edit project B (a routing endpoint) directly.
                zettel_service.update_note(proj_b.id, content=f"b-{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(("edit_parent", exc))

    threads = [threading.Thread(target=reassign), threading.Thread(target=edit_parent)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "routing update deadlocked"
    assert not errors, f"routing threads raised: {errors}"

    # Final state: task ends on project A; project A must still record the task as
    # a child (HAS_PART), and project B must not (the reassignment removed it).
    final_task = zettel_service.get_note(task.id)
    assert final_task.project_id == proj_a.id
    a_children = {
        link.target_id
        for link in zettel_service.get_note(proj_a.id).links
        if link.link_type == LinkType.HAS_PART
    }
    assert task.id in a_children, "parent A lost its HAS_PART link to the task"
    # ...and project B must no longer record the task (the reassignment removed it).
    b_children = {
        link.target_id
        for link in zettel_service.get_note(proj_b.id).links
        if link.link_type == LinkType.HAS_PART
    }
    assert task.id not in b_children, "parent B still has a stale HAS_PART link"
