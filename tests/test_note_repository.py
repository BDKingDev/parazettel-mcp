"""Tests for the NoteRepository class."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.models.graph_db import GraphDatabaseReadOnlyError
from parazettel_mcp.models.schema import LinkType, Note, NoteStatus, NoteType, Tag
from parazettel_mcp.storage.note_repository import (
    NoteRepository,
    _coerce_datetime,
    _normalize_wiki_target,
)


def test_create_note(note_repository):
    """Test creating a new note."""
    # Create a test note
    note = Note(
        title="Test Note",
        content="This is a test note.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="example")],
    )
    # Save to repository
    saved_note = note_repository.create(note)
    # Verify note was saved with an ID
    assert saved_note.id is not None
    assert saved_note.title == "Test Note"
    assert saved_note.content == "This is a test note."
    assert saved_note.note_type == NoteType.PERMANENT
    assert len(saved_note.tags) == 2
    assert {tag.name for tag in saved_note.tags} == {"test", "example"}


def test_get_note(note_repository):
    """Test retrieving a note."""
    # Create a test note
    note = Note(
        title="Get Test Note",
        content="This is a test note for retrieval.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="get")],
    )
    # Save to repository
    saved_note = note_repository.create(note)
    # Retrieve the note
    retrieved_note = note_repository.get(saved_note.id)
    # Verify note was retrieved correctly
    assert retrieved_note is not None
    assert retrieved_note.id == saved_note.id
    assert retrieved_note.title == "Get Test Note"
    # Note content includes the title as a markdown header - account for this in our test
    expected_content = f"# {note.title}\n\n{note.content}"
    assert retrieved_note.content.strip() == expected_content.strip()
    assert retrieved_note.note_type == NoteType.PERMANENT
    assert len(retrieved_note.tags) == 2
    assert {tag.name for tag in retrieved_note.tags} == {"test", "get"}


def test_update_note(note_repository):
    """Test updating a note."""
    # Create a test note
    note = Note(
        title="Update Test Note",
        content="This is a test note for updating.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="update")],
        status=NoteStatus.INBOX,
    )
    # Save to repository
    saved_note = note_repository.create(note)
    # Update the note
    saved_note.title = "Updated Test Note"
    saved_note.content = "This note has been updated."
    saved_note.tags = [Tag(name="test"), Tag(name="updated")]
    saved_note.status = NoteStatus.EVERGREEN
    # Save the update
    updated_note = note_repository.update(saved_note)
    # Retrieve the note again
    retrieved_note = note_repository.get(saved_note.id)
    # Verify note was updated
    assert retrieved_note is not None
    assert retrieved_note.id == saved_note.id
    assert retrieved_note.title == "Updated Test Note"
    # Note content includes the title as a markdown header - account for this
    expected_content = f"# {updated_note.title}\n\n{updated_note.content}"
    assert retrieved_note.content.strip() == expected_content.strip()
    assert retrieved_note.status == NoteStatus.EVERGREEN
    assert {tag.name for tag in retrieved_note.tags} == {"test", "updated"}


def test_update_note_title_only_rewrites_leading_h1(note_repository):
    """Title-only updates should replace the system H1 instead of prepending another one."""
    note = Note(
        title="Original Title",
        content="This note keeps the same body.",
        note_type=NoteType.PERMANENT,
    )
    saved_note = note_repository.create(note)

    saved_note.title = "Renamed Title"
    updated_note = note_repository.update(saved_note)
    stored_markdown = (
        note_repository.notes_dir / f"{saved_note.id}.md"
    ).read_text(encoding="utf-8")
    retrieved_note = note_repository.get(saved_note.id)

    assert updated_note.title == "Renamed Title"
    assert stored_markdown.count("# Renamed Title") == 1
    assert "# Original Title" not in stored_markdown
    assert retrieved_note is not None
    assert retrieved_note.content.startswith("# Renamed Title\n\n")
    assert "# Original Title" not in retrieved_note.content


def test_create_note_persists_status(note_repository):
    """Regular notes should persist workflow status through storage."""
    note = Note(
        title="Status Test Note",
        content="This note starts in inbox.",
        note_type=NoteType.PERMANENT,
        status=NoteStatus.INBOX,
        tags=[Tag(name="status")],
    )

    saved_note = note_repository.create(note)
    retrieved_note = note_repository.get(saved_note.id)

    assert retrieved_note is not None
    assert retrieved_note.status == NoteStatus.INBOX


def test_delete_note(note_repository):
    """Test deleting a note."""
    # Create a test note
    note = Note(
        title="Delete Test Note",
        content="This is a test note for deletion.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="delete")],
    )
    # Save to repository
    saved_note = note_repository.create(note)
    # Verify note exists
    retrieved_note = note_repository.get(saved_note.id)
    assert retrieved_note is not None
    # Delete the note
    note_repository.delete(saved_note.id)
    # Verify note no longer exists
    deleted_note = note_repository.get(saved_note.id)
    assert deleted_note is None


def test_search_notes(note_repository):
    """Test searching for notes."""
    # Create test notes
    note1 = Note(
        title="Python Programming",
        content="Python is a versatile programming language.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="python"), Tag(name="programming")],
    )
    note2 = Note(
        title="JavaScript Basics",
        content="JavaScript is used for web development.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="javascript"), Tag(name="programming")],
    )
    note3 = Note(
        title="Data Science Overview",
        content="Data science uses Python for data analysis.",
        note_type=NoteType.STRUCTURE,
        tags=[Tag(name="data science"), Tag(name="python")],
    )
    # Save notes
    saved_note1 = note_repository.create(note1)
    saved_note2 = note_repository.create(note2)
    saved_note3 = note_repository.create(note3)

    # Search by content with title included (since content has the title prepended)
    python_notes = note_repository.search(content="Python")
    # We should find both the Python notes even with title prepended
    assert len(python_notes) >= 1  # At least one match
    python_ids = {note.id for note in python_notes}
    assert saved_note1.id in python_ids or saved_note3.id in python_ids

    # Search by title
    javascript_notes = note_repository.search(title="JavaScript")
    assert len(javascript_notes) == 1
    assert javascript_notes[0].id == saved_note2.id

    # Search by broad text across title/content
    text_notes = note_repository.search(text="javascript")
    assert len(text_notes) == 1
    assert text_notes[0].id == saved_note2.id

    # Search by note_type
    structure_notes = note_repository.search(note_type=NoteType.STRUCTURE)
    assert len(structure_notes) == 1
    assert structure_notes[0].id == saved_note3.id

    # Search by tag
    programming_notes = note_repository.find_by_tag("programming")
    assert len(programming_notes) == 2
    assert {note.id for note in programming_notes} == {saved_note1.id, saved_note2.id}


def test_search_uses_fts_for_stemming_and_field_specific_queries(note_repository):
    """FTS-backed search should improve recall without blurring title/content filters."""
    note = Note(
        title="Tooling Overview",
        content="Policies libraries decisions and planning support analysis.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="tooling")],
    )
    saved_note = note_repository.create(note)

    text_results = note_repository.search(text="library")
    assert [result.id for result in text_results] == [saved_note.id]

    content_results = note_repository.search(content="library")
    assert [result.id for result in content_results] == [saved_note.id]

    assert note_repository.search(title="library") == []


def test_note_linking(note_repository):
    """Test creating links between notes."""
    # Create test notes
    note1 = Note(
        title="Source Note",
        content="This is the source note.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="source")],
    )
    note2 = Note(
        title="Target Note",
        content="This is the target note.",
        note_type=NoteType.PERMANENT,
        tags=[Tag(name="test"), Tag(name="target")],
    )
    # Save notes
    source_note = note_repository.create(note1)
    target_note = note_repository.create(note2)
    # Add a link from source to target
    source_note.add_link(
        target_id=target_note.id,
        link_type=LinkType.REFERENCE,
        description="A test link",
    )
    # Update the source note
    updated_source = note_repository.update(source_note)
    # Verify link was created
    assert len(updated_source.links) == 1
    assert updated_source.links[0].target_id == target_note.id
    assert updated_source.links[0].link_type == LinkType.REFERENCE
    assert updated_source.links[0].description == "A test link"
    # Find linked notes
    linked_notes = note_repository.find_linked_notes(source_note.id, "outgoing")
    assert len(linked_notes) == 1
    assert linked_notes[0].id == target_note.id


def test_delete_removes_dangling_links_from_source_files(note_repository):
    """Deleting a note must remove its reference from source notes' markdown files."""
    source = Note(title="Source Note", content="Links to the target.")
    target = Note(title="Target Note", content="Will be deleted.")
    saved_source = note_repository.create(source)
    saved_target = note_repository.create(target)

    saved_source.add_link(saved_target.id, LinkType.REFERENCE)
    note_repository.update(saved_source)

    # Delete the target — source's markdown should be cleaned up
    note_repository.delete(saved_target.id)

    # File-backed read should not show the dangling link
    refreshed = note_repository.get(saved_source.id)
    assert refreshed is not None
    assert all(lnk.target_id != saved_target.id for lnk in refreshed.links)


def test_invalid_frontmatter_status_is_ignored_on_read_and_rebuild(note_repository):
    """Malformed status frontmatter should not break reads or index rebuilds."""
    bad_note_path = note_repository.notes_dir / "bad-status.md"
    bad_note_path.write_text(
        "---\n"
        "id: bad-status\n"
        "title: Bad Status\n"
        "type: task\n"
        "status: flying\n"
        "project_id: project123\n"
        "---\n"
        "# Bad Status\n\n"
        "This task has an invalid stored status.\n",
        encoding="utf-8",
    )

    note_repository.rebuild_index()
    note = note_repository.get("bad-status")

    assert note is not None
    assert note.title == "Bad Status"
    assert note.note_type == NoteType.TASK
    assert note.status is None


def test_normalize_wiki_target_handles_aliases_and_fragments():
    """Wiki-link target parsing should keep only the note id."""
    assert _normalize_wiki_target("target-note|Target Note") == "target-note"
    assert _normalize_wiki_target("target-note#Section|Target Note") == "target-note"
    assert _normalize_wiki_target("target-note.md|Target Note") == "target-note"
    assert _normalize_wiki_target("target-note") == "target-note"


def test_get_normalizes_piped_wiki_link_targets(note_repository):
    """Direct file-backed reads should normalize Obsidian aliases without a rebuild."""
    target_path = note_repository.notes_dir / "aliased-target.md"
    source_path = note_repository.notes_dir / "aliased-source.md"
    target_path.write_text(
        "---\n"
        "id: aliased-target\n"
        "title: Aliased Target\n"
        "type: permanent\n"
        "---\n"
        "# Aliased Target\n\n"
        "Target content.\n",
        encoding="utf-8",
    )
    source_path.write_text(
        "---\n"
        "id: aliased-source\n"
        "title: Aliased Source\n"
        "type: permanent\n"
        "---\n"
        "# Aliased Source\n\n"
        "Source content.\n\n"
        "## Links\n"
        "- reference [[aliased-target|Aliased Target]]\n",
        encoding="utf-8",
    )

    source = note_repository.get("aliased-source")

    assert source is not None
    assert [link.target_id for link in source.links] == ["aliased-target"]
    assert all("|" not in link.target_id for link in source.links)


def test_update_adds_title_aliases_to_non_aliased_links(note_repository):
    """Touched notes should rewrite wiki-links with the target note title as alias."""
    target = Note(title="Alias Target", content="Target content.")
    source = Note(title="Alias Source", content="Source content.")
    saved_target = note_repository.create(target)
    saved_source = note_repository.create(source)

    saved_source.add_link(saved_target.id, LinkType.REFERENCE, "Existing description")
    note_repository.update(saved_source)

    stored_markdown = (
        note_repository.notes_dir / f"{saved_source.id}.md"
    ).read_text(encoding="utf-8")

    assert (
        f"- reference [[{saved_target.id}|Alias Target]] Existing description"
        in stored_markdown
    )


def test_coerce_datetime_handles_yaml_parsed_dates():
    """YAML may parse unquoted timestamp frontmatter before repository parsing."""
    import datetime

    fallback = datetime.datetime(2026, 1, 1, 0, 0, 0)
    parsed = datetime.datetime(2026, 4, 3, 14, 47, 1, 126472)
    parsed_date = datetime.date(2026, 4, 3)

    assert _coerce_datetime(parsed, fallback) == parsed
    assert _coerce_datetime(parsed_date, fallback) == datetime.datetime(2026, 4, 3)
    assert _coerce_datetime("2026-04-03T14:47:01.126472", fallback) == parsed
    assert _coerce_datetime(None, fallback) == fallback


def test_rebuild_index_accepts_yaml_parsed_timestamp_frontmatter(note_repository):
    """Unquoted YAML timestamps should not cause rebuild to skip the note."""
    note_path = note_repository.notes_dir / "timestamp-note.md"
    note_path.write_text(
        "---\n"
        "id: timestamp-note\n"
        "title: Timestamp Note\n"
        "type: permanent\n"
        "created: 2026-04-03T14:47:01.126472\n"
        "updated: 2026-04-03T14:48:49.332913\n"
        "---\n"
        "# Timestamp Note\n\n"
        "Timestamp content.\n",
        encoding="utf-8",
    )

    note_repository.rebuild_index()
    note = note_repository.get("timestamp-note")

    assert note is not None
    assert note.created_at.year == 2026
    assert note.updated_at.minute == 48


def test_rebuild_index_normalizes_piped_wiki_link_targets(note_repository):
    """Rebuild should index the note id, not the display alias, for piped links."""
    source_path = note_repository.notes_dir / "source-note.md"
    target_path = note_repository.notes_dir / "target-note.md"
    target_path.write_text(
        "---\n"
        "id: target-note\n"
        "title: Target Note\n"
        "type: permanent\n"
        "---\n"
        "# Target Note\n\n"
        "Target content.\n",
        encoding="utf-8",
    )
    source_path.write_text(
        "---\n"
        "id: source-note\n"
        "title: Source Note\n"
        "type: permanent\n"
        "---\n"
        "# Source Note\n\n"
        "Source content.\n\n"
        "## Links\n"
        "- reference [[target-note|Target Note]] Alias should not become target_id.\n",
        encoding="utf-8",
    )

    note_repository.rebuild_index()

    source = note_repository.get("source-note")
    linked = note_repository.find_linked_notes("source-note", "outgoing")

    assert source is not None
    assert [link.target_id for link in source.links] == ["target-note"]
    assert [note.id for note in linked] == ["target-note"]


def test_delete_cleans_aliased_source_links(note_repository):
    """Deleting a linked note should remove aliased wiki-links from the source file."""
    target_path = note_repository.notes_dir / "delete-target.md"
    source_path = note_repository.notes_dir / "delete-source.md"
    target_path.write_text(
        "---\n"
        "id: delete-target\n"
        "title: Delete Target\n"
        "type: permanent\n"
        "---\n"
        "# Delete Target\n\n"
        "Target content.\n",
        encoding="utf-8",
    )
    source_path.write_text(
        "---\n"
        "id: delete-source\n"
        "title: Delete Source\n"
        "type: permanent\n"
        "---\n"
        "# Delete Source\n\n"
        "Source content.\n\n"
        "## Links\n"
        "- reference [[delete-target|Delete Target]]\n",
        encoding="utf-8",
    )

    note_repository.rebuild_index()
    note_repository.delete("delete-target")
    refreshed = note_repository.get("delete-source")
    stored_markdown = source_path.read_text(encoding="utf-8")

    assert refreshed is not None
    assert refreshed.links == []
    assert "[[delete-target|Delete Target]]" not in stored_markdown


def test_note_to_markdown_uses_bulk_title_lookup_for_aliases(
    note_repository, monkeypatch
):
    """Alias rendering should not re-read each linked note from disk."""
    source = note_repository.create(
        Note(title="Bulk Lookup Source", content="Source content.")
    )
    target = note_repository.create(
        Note(title="Bulk Lookup Target", content="Target content.")
    )

    source.add_link(target.id, LinkType.REFERENCE)
    source = note_repository.update(source)

    def unexpected_get(_note_id):
        raise AssertionError("alias rendering should not call NoteRepository.get()")

    monkeypatch.setattr(note_repository, "get", unexpected_get)

    markdown = note_repository._note_to_markdown(source)

    assert f"[[{target.id}|Bulk Lookup Target]]" in markdown


def test_update_falls_back_to_plain_link_for_unsafe_alias_titles(note_repository):
    """Unsafe titles should fall back to plain wiki-links instead of invalid aliases."""
    source = note_repository.create(
        Note(title="Unsafe Alias Source", content="Source content.")
    )
    target = note_repository.create(
        Note(title="Unsafe | Alias", content="Target content.")
    )

    source.add_link(target.id, LinkType.REFERENCE)
    note_repository.update(source)
    stored_markdown = (
        note_repository.notes_dir / f"{source.id}.md"
    ).read_text(encoding="utf-8")

    assert f"[[{target.id}]]" in stored_markdown
    assert f"[[{target.id}|Unsafe | Alias]]" not in stored_markdown


def test_delete_preserves_aliases_for_remaining_links(note_repository):
    """Deleting one target should not strip aliases from other remaining links."""
    first_target = note_repository.notes_dir / "delete-first-target.md"
    second_target = note_repository.notes_dir / "delete-second-target.md"
    source_path = note_repository.notes_dir / "delete-keep-source.md"
    first_target.write_text(
        "---\n"
        "id: delete-first-target\n"
        "title: Delete First Target\n"
        "type: permanent\n"
        "---\n"
        "# Delete First Target\n\n"
        "Target content.\n",
        encoding="utf-8",
    )
    second_target.write_text(
        "---\n"
        "id: delete-second-target\n"
        "title: Delete Second Target\n"
        "type: permanent\n"
        "---\n"
        "# Delete Second Target\n\n"
        "Target content.\n",
        encoding="utf-8",
    )
    source_path.write_text(
        "---\n"
        "id: delete-keep-source\n"
        "title: Delete Keep Source\n"
        "type: permanent\n"
        "---\n"
        "# Delete Keep Source\n\n"
        "Source content.\n\n"
        "## Links\n"
        "- reference [[delete-first-target|Delete First Target]]\n"
        "- reference [[delete-second-target|Delete Second Target]]\n",
        encoding="utf-8",
    )

    note_repository.rebuild_index()
    original = note_repository.get("delete-keep-source")
    assert original is not None
    original_updated_at = original.updated_at
    note_repository.delete("delete-first-target")
    refreshed = note_repository.get("delete-keep-source")
    stored_markdown = source_path.read_text(encoding="utf-8")

    assert refreshed is not None
    assert refreshed.updated_at == original_updated_at
    assert [link.target_id for link in refreshed.links] == ["delete-second-target"]
    assert "[[delete-first-target|Delete First Target]]" not in stored_markdown
    assert "[[delete-second-target|Delete Second Target]]" in stored_markdown


def test_delete_reuses_file_backed_source_note_for_preserved_timestamp_update(
    note_repository, monkeypatch
):
    """Delete should not re-read a source note after loading it for cleanup."""
    source = note_repository.create(
        Note(title="Delete Read Source", content="Source content.")
    )
    target = note_repository.create(
        Note(title="Delete Read Target", content="Target content.")
    )
    source.add_link(target.id, LinkType.REFERENCE)
    note_repository.update(source)

    original_get = note_repository.get
    get_counts = {}

    def tracking_get(note_id):
        get_counts[note_id] = get_counts.get(note_id, 0) + 1
        return original_get(note_id)

    monkeypatch.setattr(note_repository, "get", tracking_get)

    note_repository.delete(target.id)

    assert get_counts.get(source.id, 0) == 1


def test_delete_preserves_remaining_link_created_at(note_repository):
    """Deleting one target should not reset created_at on remaining links."""
    source = note_repository.create(
        Note(title="Delete CreatedAt Source", content="Source content.")
    )
    first_target = note_repository.create(
        Note(title="Delete CreatedAt First", content="First target.")
    )
    second_target = note_repository.create(
        Note(title="Delete CreatedAt Second", content="Second target.")
    )

    source.add_link(first_target.id, LinkType.REFERENCE)
    source.add_link(second_target.id, LinkType.REFERENCE)
    note_repository.update(source)

    original_link = note_repository.get_link(
        source.id, second_target.id, LinkType.REFERENCE.value
    )
    assert original_link is not None
    original_created_at = original_link["created_at"]

    note_repository.delete(first_target.id)

    refreshed_link = note_repository.get_link(
        source.id, second_target.id, LinkType.REFERENCE.value
    )
    assert refreshed_link is not None
    assert refreshed_link["created_at"] == original_created_at


def test_rebuild_index_repopulates_graph(note_repository):
    """rebuild_index() should clear and re-import all notes from markdown files."""
    saved = note_repository.create(Note(title="Rebuild Test", content="Rebuild content."))

    backup_path = note_repository.rebuild_index()

    result = note_repository.get(saved.id)
    assert result is not None
    assert result.title == "Rebuild Test"
    assert backup_path is not None
    assert backup_path.exists()


def test_create_graph_backup_copies_directory_snapshot(note_repository, tmp_path, monkeypatch):
    """Directory-backed graph paths should be snapshotted with copytree."""
    graph_dir = tmp_path / "graph.kuzu"
    graph_dir.mkdir()
    nested_file = graph_dir / "data.bin"
    nested_file.write_text("graph-bytes", encoding="utf-8")

    note_repository.graph_db_path = graph_dir
    monkeypatch.setattr(note_repository, "close", lambda: None)
    monkeypatch.setattr(
        note_repository, "_open_graph_db", lambda allow_rebuild_if_needed=False: None
    )

    backup_path = note_repository._create_graph_backup()

    assert backup_path is not None
    assert backup_path.is_dir()
    assert (backup_path / "data.bin").read_text(encoding="utf-8") == "graph-bytes"


# ---------------------------------------------------------------------------
# Phase 1 data layer tests
# ---------------------------------------------------------------------------


def test_graph_db_initialized(note_repository):
    """Graph database should be initialised after repository construction."""
    conn = note_repository._get_conn()
    try:
        result = conn.execute("MATCH (n:Note) RETURN count(n) AS cnt")
        count = result.get_next()[0]
    finally:
        conn.close()
    assert count == 0  # empty repository has no notes


def test_fetch_notes_by_ids_preserves_requested_order(note_repository):
    """Low-level graph reconstruction should preserve the caller's ID order."""
    first = note_repository.create(Note(title="First Ordered", content="First body"))
    second = note_repository.create(Note(title="Second Ordered", content="Second body"))

    with note_repository._connection() as conn:
        ordered = note_repository._fetch_notes_by_ids(conn, [second.id, first.id])

    assert [note.id for note in ordered] == [second.id, first.id]


def test_connection_helper_closes_graph_connection(note_repository):
    """_connection() should close the underlying Kuzu connection."""
    fake_conn = MagicMock()

    with patch.object(note_repository, "_get_conn", return_value=fake_conn):
        with note_repository._connection() as conn:
            assert conn is fake_conn

    fake_conn.close.assert_called_once()


def test_closed_repository_rejects_new_connections(note_repository):
    """Closed repositories should fail fast instead of creating new Kuzu handles."""
    note_repository.close()

    with pytest.raises(RuntimeError, match="closed"):
        note_repository._get_conn()


def test_repository_close_allows_reopen_same_graph_path():
    """Closing a repository should release the graph path for a fresh reopen."""
    test_root = Path(".tmp") / "test-note-repository" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "test_graph.kuzu"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    first_repo = None
    reopened_repo = None

    try:
        config.notes_dir = notes_dir
        config.graph_db_path = graph_db_path

        first_repo = NoteRepository(notes_dir=notes_dir)
        saved = first_repo.create(Note(title="Reopen Test", content="Reopen body"))
        first_repo.close()
        first_repo = None

        reopened_repo = NoteRepository(notes_dir=notes_dir)
        reopened = reopened_repo.search(title="Reopen Test")
        assert len(reopened) == 1
        assert reopened[0].id == saved.id
    finally:
        if reopened_repo is not None:
            reopened_repo.close()
        if first_repo is not None:
            first_repo.close()
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path
        shutil.rmtree(test_root, ignore_errors=True)


def test_multiple_repositories_share_same_graph_path():
    """Repositories for the same graph path should share the DB handle safely."""
    test_root = Path(".tmp") / "test-note-repository" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "test_graph.kuzu"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    first_repo = None
    second_repo = None

    try:
        config.notes_dir = notes_dir
        config.graph_db_path = graph_db_path

        first_repo = NoteRepository(notes_dir=notes_dir)
        second_repo = NoteRepository(notes_dir=notes_dir)

        saved = first_repo.create(
            Note(title="Shared Graph Note", content="Shared body")
        )
        results = second_repo.search(title="Shared Graph Note")
        assert len(results) == 1
        assert results[0].id == saved.id

        first_repo.close()
        first_repo = None

        still_available = second_repo.search(title="Shared Graph Note")
        assert len(still_available) == 1
    finally:
        if second_repo is not None:
            second_repo.close()
        if first_repo is not None:
            first_repo.close()
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path
        shutil.rmtree(test_root, ignore_errors=True)


def test_repository_falls_back_to_read_only_when_graph_db_locked(monkeypatch):
    """A second process lock should degrade to read-only mode instead of failing init."""
    test_root = Path(".tmp") / "test-note-repository" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "test_graph.kuzu"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    init_calls = []
    close_calls = []
    repository = None

    def fake_init_graph_db(db_path: Path, read_only: bool = False):
        init_calls.append((Path(db_path), read_only))
        if not read_only:
            raise RuntimeError(f"Could not set lock on file {db_path}")
        return MagicMock(name="readonly_graph_db")

    def fake_close_graph_db(db_path: Path, read_only: bool = False):
        close_calls.append((Path(db_path), read_only))

    try:
        config.notes_dir = notes_dir
        config.graph_db_path = graph_db_path
        monkeypatch.setattr(
            "parazettel_mcp.storage.note_repository.init_graph_db",
            fake_init_graph_db,
        )
        monkeypatch.setattr(
            "parazettel_mcp.storage.note_repository.close_graph_db",
            fake_close_graph_db,
        )
        monkeypatch.setattr(
            NoteRepository,
            "rebuild_index_if_needed",
            lambda self: pytest.fail("read-only fallback should skip rebuild checks"),
        )

        repository = NoteRepository(notes_dir=notes_dir)

        assert repository.read_only is True
        assert [read_only for _, read_only in init_calls] == [False, True]
        assert init_calls[0][0].name == graph_db_path.name
    finally:
        if repository is not None:
            repository.close()
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path
        shutil.rmtree(test_root, ignore_errors=True)

    assert close_calls
    assert close_calls[-1][1] is True


def test_create_uses_graph_db_without_creating_legacy_sqlite():
    """Repository writes should index into Kuzu without creating the legacy SQLite DB."""
    test_root = Path(".tmp") / "test-note-repository" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "test_graph.kuzu"
    legacy_db_path = test_root / "db" / "legacy-parazettel.db"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    original_database_path = config.database_path
    repository = None

    try:
        config.notes_dir = notes_dir
        config.graph_db_path = graph_db_path
        config.database_path = legacy_db_path

        repository = NoteRepository(notes_dir=notes_dir)
        saved = repository.create(Note(title="Graph Write Test", content="Uses Kuzu"))

        assert graph_db_path.exists()
        assert not legacy_db_path.exists()

        with repository._connection() as conn:
            result = conn.execute(
                "MATCH (n:Note {id: $id}) RETURN count(n) AS cnt",
                {"id": saved.id},
            )
            assert result.get_next()[0] == 1
    finally:
        if repository is not None:
            repository.close()
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path
        config.database_path = original_database_path
        shutil.rmtree(test_root, ignore_errors=True)


def test_read_only_repository_rejects_create(note_repository):
    """Mutating writes should fail fast in read-only graph mode."""
    note_repository.read_only = True

    with pytest.raises(GraphDatabaseReadOnlyError, match="read-only graph mode"):
        note_repository.create(Note(title="Blocked Write", content="No writes"))


def test_read_only_repository_rejects_rebuild(note_repository):
    """Index rebuild should fail fast in read-only graph mode."""
    note_repository.read_only = True

    with pytest.raises(GraphDatabaseReadOnlyError, match="read-only graph mode"):
        note_repository.rebuild_index()


def test_create_leaves_no_tmp_file(note_repository):
    """Atomic write: no .md.tmp file should remain after create()."""
    note = Note(title="Atomic Create", content="Test atomic write.")
    saved = note_repository.create(note)
    assert not list(note_repository.notes_dir.glob(f"{saved.id}.md*.tmp"))


def test_update_leaves_no_tmp_file(note_repository):
    """Atomic write: no .md.tmp file should remain after update()."""
    note = Note(title="Atomic Update", content="Original content.")
    saved = note_repository.create(note)
    saved.content = "Updated content."
    note_repository.update(saved)
    assert not list(note_repository.notes_dir.glob(f"{saved.id}.md*.tmp"))


def test_create_retries_transient_replace_failure(note_repository, monkeypatch):
    """Atomic create should retry once when Windows briefly locks the target file."""
    note = Note(title="Retry Create", content="Retry around replace().")
    original_replace = Path.replace
    calls = {"count": 0}

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.name.startswith(f"{note.id}.md") and calls["count"] == 0:
            calls["count"] += 1
            raise PermissionError(13, "Access is denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    saved = note_repository.create(note)

    assert calls["count"] == 1
    assert note_repository.get(saved.id) is not None
    assert not list(note_repository.notes_dir.glob(f"{saved.id}.md*.tmp"))


def test_cache_hit_after_get(note_repository):
    """Second get() for the same ID should return a result (cache hit path)."""
    note = Note(title="Cache Hit", content="Should be cached.")
    saved = note_repository.create(note)
    first = note_repository.get(saved.id)
    second = note_repository.get(saved.id)
    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.title == second.title


def test_cache_invalidated_on_update(note_repository):
    """After update(), get() should return the new content, not a stale cache entry."""
    note = Note(title="Cache Invalidation", content="Original.")
    saved = note_repository.create(note)
    # Prime the cache
    note_repository.get(saved.id)
    # Mutate and update
    saved.title = "Cache Invalidation Updated"
    note_repository.update(saved)
    fresh = note_repository.get(saved.id)
    assert fresh is not None
    assert fresh.title == "Cache Invalidation Updated"


def test_cache_invalidated_on_delete(note_repository):
    """After delete(), get() should return None, not a stale cache entry."""
    note = Note(title="Cache Delete", content="Will be deleted.")
    saved = note_repository.create(note)
    # Prime the cache
    note_repository.get(saved.id)
    # Delete and verify cache is gone
    note_repository.delete(saved.id)
    result = note_repository.get(saved.id)
    assert result is None


def test_search_returns_correct_content_and_tags(note_repository):
    """search() reconstructs notes from DB rows — content, tags, and type must match."""
    note = Note(
        title="Search DB Reconstruction",
        content="Verifying DB-backed search.",
        note_type=NoteType.LITERATURE,
        tags=[Tag(name="search"), Tag(name="db")],
    )
    note_repository.create(note)
    results = note_repository.search(title="Search DB Reconstruction")
    assert len(results) == 1
    result = results[0]
    assert result.note_type == NoteType.LITERATURE
    assert {t.name for t in result.tags} == {"search", "db"}
    assert "Verifying DB-backed search." in result.content


def test_search_returns_links(note_repository):
    """search() via _note_from_db() should include outgoing links."""
    source = Note(title="Search Link Source", content="Source note.")
    target = Note(title="Search Link Target", content="Target note.")
    saved_source = note_repository.create(source)
    saved_target = note_repository.create(target)
    saved_source.add_link(saved_target.id, LinkType.REFERENCE)
    note_repository.update(saved_source)

    results = note_repository.search(title="Search Link Source")
    assert len(results) == 1
    assert any(lnk.target_id == saved_target.id for lnk in results[0].links)


def test_rebuild_index_if_needed_detects_missing_file(note_repository):
    """rebuild_index_if_needed() should trigger rebuild when a file is gone but DB has it."""
    import os

    note = Note(title="Rebuild Test", content="Will be orphaned in DB.")
    saved = note_repository.create(note)
    # Remove the file but leave the DB entry
    file_path = note_repository.notes_dir / f"{saved.id}.md"
    os.remove(file_path)
    # Should detect mismatch and rebuild (DB entry removed)
    note_repository.rebuild_index_if_needed()
    # After rebuild the note should not be in the DB
    result = note_repository.get(saved.id)
    assert result is None


def test_rebuild_index_if_needed_detects_extra_file(note_repository):
    """rebuild_index_if_needed() should trigger rebuild when a file exists but DB lacks it."""
    note = Note(title="Extra File Test", content="Exists on disk only.")
    saved = note_repository.create(note)
    # Manually delete from graph DB but leave file
    with note_repository._connection() as conn:
        conn.execute("MATCH (n:Note {id: $id}) DETACH DELETE n", {"id": saved.id})
    # Should detect mismatch and rebuild (DB re-indexed from file)
    note_repository.rebuild_index_if_needed()
    result = note_repository.get(saved.id)
    assert result is not None
    assert result.title == "Extra File Test"


def test_parse_note_from_markdown_deduplicates_duplicate_tags(note_repository):
    """Duplicate tags in frontmatter should collapse to first-seen unique tags."""
    parsed = note_repository._parse_note_from_markdown(
        "---\n"
        "id: dup-tags\n"
        "title: Duplicate Tags\n"
        "type: permanent\n"
        "tags: alpha, beta, alpha\n"
        "created: 2026-01-01T00:00:00\n"
        "updated: 2026-01-01T00:00:00\n"
        "---\n"
        "# Duplicate Tags\n\n"
        "Body\n"
    )

    assert [tag.name for tag in parsed.tags] == ["alpha", "beta"]


def test_parse_note_from_markdown_deduplicates_duplicate_links(note_repository):
    """Duplicate link lines should not produce duplicate outgoing link records."""
    parsed = note_repository._parse_note_from_markdown(
        "---\n"
        "id: dup-links\n"
        "title: Duplicate Links\n"
        "type: permanent\n"
        "tags: test\n"
        "created: 2026-01-01T00:00:00\n"
        "updated: 2026-01-01T00:00:00\n"
        "---\n"
        "# Duplicate Links\n\n"
        "Body\n\n"
        "## Links\n"
        "- reference [[target-1]] First description\n"
        "- reference [[target-1]] Second description\n"
    )

    assert len(parsed.links) == 1
    assert parsed.links[0].target_id == "target-1"
    assert parsed.links[0].link_type == LinkType.REFERENCE


def test_metadata_round_trips_through_search(note_repository):
    """Metadata stored in a note should survive the DB path returned by search()."""
    note = Note(
        title="Metadata Search Test",
        content="Has custom metadata.",
        metadata={"source_url": "https://example.com", "author": "test"},
    )
    note_repository.create(note)
    results = note_repository.search(title="Metadata Search Test")
    assert len(results) == 1
    assert results[0].metadata == {
        "source_url": "https://example.com",
        "author": "test",
    }


def test_metadata_round_trips_through_get_all(note_repository):
    """Metadata stored in a note should survive the DB path returned by get_all()."""
    note = Note(
        title="Metadata GetAll Test",
        content="Has custom metadata.",
        metadata={"priority": 1, "project": "alpha"},
    )
    saved = note_repository.create(note)
    all_notes = note_repository.get_all()
    match = next((n for n in all_notes if n.id == saved.id), None)
    assert match is not None
    assert match.metadata == {"priority": 1, "project": "alpha"}


def test_metadata_round_trips_through_find_linked_notes(note_repository):
    """Metadata should be present on notes returned by find_linked_notes()."""
    source = Note(title="Metadata Link Source", content="Source.")
    target = Note(
        title="Metadata Link Target",
        content="Target with metadata.",
        metadata={"reviewed": True},
    )
    saved_source = note_repository.create(source)
    saved_target = note_repository.create(target)
    saved_source.add_link(saved_target.id, LinkType.REFERENCE)
    note_repository.update(saved_source)

    linked = note_repository.find_linked_notes(saved_source.id, "outgoing")
    assert len(linked) == 1
    assert linked[0].metadata == {"reviewed": True}


def test_note_without_metadata_returns_empty_dict(note_repository):
    """Notes created without metadata should return {} from all DB-backed paths."""
    note = Note(title="No Metadata", content="Plain note, no custom frontmatter.")
    saved = note_repository.create(note)

    search_result = note_repository.search(title="No Metadata")
    assert search_result[0].metadata == {}

    all_notes = note_repository.get_all()
    match = next((n for n in all_notes if n.id == saved.id), None)
    assert match is not None
    assert match.metadata == {}


def test_cache_returns_independent_copy(note_repository):
    """Mutating a note returned by get() should not corrupt the cache."""
    note = Note(title="Cache Copy Test", content="Original content.")
    saved = note_repository.create(note)
    # Prime cache
    first = note_repository.get(saved.id)
    assert first is not None
    # Mutate the returned object
    first.title = "MUTATED"
    first.tags.append(Tag(name="injected"))
    # Second get() should return the original, not the mutated version
    second = note_repository.get(saved.id)
    assert second is not None
    assert second.title == "Cache Copy Test"
    assert all(t.name != "injected" for t in second.tags)


def test_metadata_with_datetime_round_trips(note_repository):
    """Metadata containing datetime.date values should serialize to JSON safely."""
    import datetime as dt

    note = Note(
        title="Date Metadata Test",
        content="Has a date in metadata.",
        metadata={
            "published": dt.date(2026, 1, 15),
            "updated": dt.datetime(2026, 3, 1, 12, 0),
        },
    )
    note_repository.create(note)
    results = note_repository.search(title="Date Metadata Test")
    assert len(results) == 1
    # Dates are serialized as ISO strings by _json_default
    assert results[0].metadata["published"] == "2026-01-15"
    assert results[0].metadata["updated"] == "2026-03-01T12:00:00"


def test_search_content_matches_get_content(note_repository):
    """Content from search() (DB-backed) should match content from get() (file-backed)."""
    note = Note(
        title="Content Consistency",
        content="Body text here.",
        tags=[Tag(name="test")],
    )
    saved = note_repository.create(note)
    from_file = note_repository.get(saved.id)
    from_db = note_repository.search(title="Content Consistency")
    assert len(from_db) == 1
    assert from_file is not None
    assert from_db[0].content == from_file.content


def test_copy_file_with_retry_recovers_from_transient_lock(tmp_path):
    """The graph-backup copy retries WinError 33 (locked region) until it clears."""
    from parazettel_mcp.storage import note_repository as nr

    src = tmp_path / "graph.kuzu"
    src.write_bytes(b"db-bytes")
    dst = tmp_path / "graph.kuzu.bak"

    real_copy = shutil.copy2
    calls = {"n": 0}

    def flaky_copy(s, d, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            err = PermissionError("locked")
            err.winerror = 33  # ERROR_LOCK_VIOLATION
            raise err
        return real_copy(s, d, *args, **kwargs)

    with patch.object(nr.shutil, "copy2", side_effect=flaky_copy):
        with patch.object(nr.time, "sleep", lambda *_: None):
            nr._copy_file_with_retry(src, dst)

    assert calls["n"] == 3  # failed twice, succeeded on the third attempt
    assert dst.read_bytes() == b"db-bytes"


def test_copy_file_with_retry_reraises_non_retryable_error(tmp_path):
    """A non-lock OSError is not retried and propagates immediately."""
    from parazettel_mcp.storage import note_repository as nr

    src = tmp_path / "graph.kuzu"
    src.write_bytes(b"x")
    dst = tmp_path / "graph.kuzu.bak"

    calls = {"n": 0}

    def failing_copy(s, d, *args, **kwargs):
        calls["n"] += 1
        raise FileNotFoundError("missing")

    with patch.object(nr.shutil, "copy2", side_effect=failing_copy):
        with pytest.raises(FileNotFoundError):
            nr._copy_file_with_retry(src, dst)

    assert calls["n"] == 1  # not retried
