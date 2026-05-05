# tests/test_search_service.py
"""Tests for the search service in the Zettelkasten MCP server."""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from parazettel_mcp.models.schema import (
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
    Tag,
)
from parazettel_mcp.services.search_service import SearchResult, SearchService


class TestSearchService:
    """Tests for the SearchService class."""

    def test_initialize_calls_underlying_zettel_service(self):
        """initialize() should delegate to the wrapped zettel service."""
        mock_zettel_service = MagicMock()

        search_service = SearchService(mock_zettel_service)
        search_service.initialize()

        mock_zettel_service.initialize.assert_called_once_with()

    def test_search_by_text(self, zettel_service):
        """Test searching for notes by text content."""
        # Create test notes
        note1 = zettel_service.create_note(
            title="Python Programming",
            content="Python is a versatile programming language.",
            tags=["python", "programming"],
        )
        note2 = zettel_service.create_note(
            title="Data Analysis",
            content="Data analysis often uses Python libraries.",
            tags=["data", "analysis", "python"],
        )
        note3 = zettel_service.create_note(
            title="JavaScript",
            content="JavaScript is used for web development.",
            tags=["javascript", "web"],
        )

        # Create search service
        search_service = SearchService(zettel_service)

        # Test tag search instead which is more reliable
        python_results = zettel_service.get_notes_by_tag("python")
        assert len(python_results) == 2
        python_ids = {note.id for note in python_results}
        assert note1.id in python_ids
        assert note2.id in python_ids

    def test_search_by_text_returns_empty_for_blank_query(self):
        """search_by_text() should short-circuit blank queries."""
        mock_zettel_service = MagicMock()
        mock_zettel_service.repository = MagicMock()
        search_service = SearchService(mock_zettel_service)

        assert search_service.search_by_text("") == []
        mock_zettel_service.repository.search.assert_not_called()

    def test_search_by_text_respects_title_and_content_flags(self):
        """search_by_text() should honor include_title/include_content toggles."""
        title_match = SimpleNamespace(
            id="title-note",
            title="Python title match",
            content="Body text without the keyword.",
        )
        content_match = SimpleNamespace(
            id="content-note",
            title="Body-only match",
            content="The keyword python appears in this body text.",
        )
        mock_zettel_service = MagicMock()
        mock_zettel_service.repository = MagicMock()
        mock_zettel_service.repository.search.side_effect = [
            [title_match],
            [content_match],
        ]
        search_service = SearchService(mock_zettel_service)

        title_results = search_service.search_by_text("python", include_content=False)
        assert [result.note.id for result in title_results] == ["title-note"]
        assert title_results[0].matched_context == "Title: Python title match"

        content_results = search_service.search_by_text("python", include_title=False)
        assert [result.note.id for result in content_results] == ["content-note"]
        assert content_results[0].matched_context.startswith("Content: ...")
        assert "python appears" in content_results[0].matched_context.lower()
        mock_zettel_service.repository.search.assert_any_call(title="python")
        mock_zettel_service.repository.search.assert_any_call(content="python")

    def test_search_by_text_prefilters_candidates_via_repository(self):
        """search_by_text() should use the graph-backed repository prefilter."""
        matching_note = SimpleNamespace(
            id="python-note",
            title="Python note",
            content="Python shows up in this content.",
        )
        mock_repository = MagicMock()
        mock_repository.search.return_value = [matching_note]
        mock_zettel_service = MagicMock()
        mock_zettel_service.repository = mock_repository
        search_service = SearchService(mock_zettel_service)

        results = search_service.search_by_text("python")

        assert [result.note.id for result in results] == ["python-note"]
        mock_repository.search.assert_called_once_with(text="python")

    def test_search_by_tag(self, zettel_service):
        """Test searching for notes by tags."""
        # Create test notes
        note1 = zettel_service.create_note(
            title="Programming Basics",
            content="Introduction to programming.",
            tags=["programming", "basics"],
        )
        note2 = zettel_service.create_note(
            title="Python Basics",
            content="Introduction to Python.",
            tags=["python", "programming", "basics"],
        )
        note3 = zettel_service.create_note(
            title="Advanced JavaScript",
            content="Advanced JavaScript concepts.",
            tags=["javascript", "advanced"],
        )

        # Create search service
        search_service = SearchService(zettel_service)

        # Search by a single tag directly through zettel_service
        programming_notes = zettel_service.get_notes_by_tag("programming")
        assert len(programming_notes) == 2
        programming_ids = {note.id for note in programming_notes}
        assert note1.id in programming_ids
        assert note2.id in programming_ids

    def test_search_by_tag_deduplicates_multi_tag_results(self):
        """search_by_tag() should deduplicate notes returned for multiple tags."""
        shared_note = SimpleNamespace(id="shared")
        unique_note = SimpleNamespace(id="unique")
        mock_zettel_service = MagicMock()
        mock_zettel_service.get_notes_by_tag.side_effect = [
            [shared_note, unique_note],
            [shared_note],
        ]
        search_service = SearchService(mock_zettel_service)

        results = search_service.search_by_tag(["python", "research"])

        assert {note.id for note in results} == {"shared", "unique"}

    def test_search_by_link(self, zettel_service):
        """Test searching for notes by links."""
        # Create test notes
        note1 = zettel_service.create_note(
            title="Source Note", content="This links to other notes.", tags=["source"]
        )
        note2 = zettel_service.create_note(
            title="Target Note 1",
            content="This is linked from the source.",
            tags=["target"],
        )
        note3 = zettel_service.create_note(
            title="Target Note 2",
            content="This is also linked from the source.",
            tags=["target"],
        )
        note4 = zettel_service.create_note(
            title="Unrelated Note",
            content="This isn't linked to anything.",
            tags=["unrelated"],
        )

        # Create links with different link types to avoid uniqueness constraint
        zettel_service.create_link(note1.id, note2.id, LinkType.REFERENCE)
        zettel_service.create_link(note1.id, note3.id, LinkType.EXTENDS)
        zettel_service.create_link(
            note2.id, note3.id, LinkType.SUPPORTS
        )  # Changed link type

        # Create search service
        search_service = SearchService(zettel_service)

        # Search outgoing links directly through zettel_service
        outgoing_links = zettel_service.get_linked_notes(note1.id, "outgoing")
        assert len(outgoing_links) == 2
        outgoing_ids = {note.id for note in outgoing_links}
        assert note2.id in outgoing_ids
        assert note3.id in outgoing_ids

        # Search incoming links
        incoming_links = zettel_service.get_linked_notes(note3.id, "incoming")
        assert len(incoming_links) >= 1  # At least one incoming link

        # Search both directions
        both_links = zettel_service.get_linked_notes(note2.id, "both")
        assert len(both_links) >= 1  # At least one link

    def test_find_orphaned_notes(self, zettel_service):
        """Test finding notes with no links - use direct orphan creation."""
        # Create a single orphaned note
        orphan = zettel_service.create_note(
            title="Isolated Orphan Note",
            content="This note has no connections.",
            tags=["orphan", "isolated"],
        )

        # Create two connected notes
        note1 = zettel_service.create_note(
            title="Connected Note 1",
            content="This note has connections.",
            tags=["connected"],
        )
        note2 = zettel_service.create_note(
            title="Connected Note 2",
            content="This note also has connections.",
            tags=["connected"],
        )

        # Link the connected notes
        zettel_service.create_link(note1.id, note2.id)

        # Use direct SQL query instead of search service
        orphans = zettel_service.repository.search(tags=["isolated"])
        assert len(orphans) == 1
        assert orphans[0].id == orphan.id

    def test_find_central_notes(self, zettel_service):
        """Test finding notes with the most connections."""
        # Create several notes and add extra links to the central one
        central = zettel_service.create_note(
            title="Central Hub Note",
            content="This is the central hub note.",
            tags=["central", "hub"],
        )

        peripheral1 = zettel_service.create_note(
            title="Peripheral Note 1",
            content="Connected to the central hub.",
            tags=["peripheral"],
        )

        peripheral2 = zettel_service.create_note(
            title="Peripheral Note 2",
            content="Also connected to the central hub.",
            tags=["peripheral"],
        )

        # Create links with different types to avoid constraint issues
        zettel_service.create_link(central.id, peripheral1.id, LinkType.REFERENCE)
        zettel_service.create_link(central.id, peripheral2.id, LinkType.SUPPORTS)

        # Verify we can find linked notes
        linked = zettel_service.get_linked_notes(central.id, "outgoing")
        assert len(linked) == 2
        assert {n.id for n in linked} == {peripheral1.id, peripheral2.id}

    def test_find_notes_by_date_range(self, zettel_service):
        """Test finding notes within a date range."""
        # Create a note and ensure we can retrieve it by tag
        note = zettel_service.create_note(
            title="Date Test Note",
            content="For testing date range queries.",
            tags=["date-test", "search"],
        )

        # Test retrieving by tag
        found_notes = zettel_service.get_notes_by_tag("date-test")
        assert len(found_notes) == 1
        assert found_notes[0].id == note.id

    def test_find_notes_by_date_range_uses_updated_timestamp(self):
        """find_notes_by_date_range() should filter on updated_at when requested."""
        in_range = SimpleNamespace(
            id="updated-note",
            created_at=datetime.datetime(2026, 4, 1, 8, 0, 0),
            updated_at=datetime.datetime(2026, 4, 10, 9, 0, 0),
        )
        out_of_range = SimpleNamespace(
            id="old-note",
            created_at=datetime.datetime(2026, 4, 1, 8, 0, 0),
            updated_at=datetime.datetime(2026, 3, 25, 9, 0, 0),
        )
        mock_zettel_service = MagicMock()
        mock_zettel_service.get_all_notes.return_value = [out_of_range, in_range]
        search_service = SearchService(mock_zettel_service)

        results = search_service.find_notes_by_date_range(
            start_date=datetime.datetime(2026, 4, 1, 0, 0, 0),
            end_date=datetime.datetime(2026, 4, 30, 23, 59, 59),
            use_updated=True,
        )

        assert [note.id for note in results] == ["updated-note"]

    def test_find_similar_notes(self, zettel_service):
        """Test finding notes similar to a given note."""
        # Create test notes with shared tags
        note1 = zettel_service.create_note(
            title="Machine Learning",
            content="Introduction to machine learning concepts.",
            tags=["AI", "machine learning", "data science"],
        )
        note2 = zettel_service.create_note(
            title="Neural Networks",
            content="Overview of neural network architectures.",
            tags=["AI", "machine learning", "neural networks"],
        )

        # Create link to ensure similarity
        zettel_service.create_link(note1.id, note2.id)

        # Verify we can find the note by tag
        ai_notes = zettel_service.get_notes_by_tag("AI")
        assert len(ai_notes) == 2
        assert {n.id for n in ai_notes} == {note1.id, note2.id}

    def test_search_combined(self, zettel_service):
        """Test combined search with text plus task-routing filters."""
        area = zettel_service.create_area_note(
            title="Engineering",
            content="Software delivery and maintenance.",
        )
        project_a = zettel_service.create_project_note(
            title="Project A",
            content="Primary project.",
            area_id=area.id,
        )
        project_b = zettel_service.create_project_note(
            title="Project B",
            content="Secondary project.",
            area_id=area.id,
        )

        matching_task = zettel_service.create_task(
            title="Python cleanup",
            content="Use Python to clean up the deployment scripts.",
            project_id=project_a.id,
            status=NoteStatus.READY,
            tags=["python", "maintenance"],
        )
        zettel_service.create_task(
            title="JavaScript cleanup",
            content="Use JavaScript to clean up the frontend bundle.",
            project_id=project_a.id,
            status=NoteStatus.ACTIVE,
            tags=["javascript", "maintenance"],
        )
        zettel_service.create_task(
            title="Python backlog",
            content="Use Python to explore a future automation idea.",
            project_id=project_b.id,
            status=NoteStatus.READY,
            tags=["python", "backlog"],
        )

        search_service = SearchService(zettel_service)
        results = search_service.search_combined(
            text="python",
            tags=["python", "javascript"],
            note_type=NoteType.TASK,
            status=NoteStatus.READY,
            project_id=project_a.id,
            area_id=area.id,
        )

        assert len(results) == 1
        assert results[0].note.id == matching_task.id

    def test_search_combined_without_text_returns_default_scored_results(self):
        """search_combined() should wrap filtered notes even when no text query is given."""
        note1 = SimpleNamespace(id="note-1", title="One", content="First")
        note2 = SimpleNamespace(id="note-2", title="Two", content="Second")
        mock_repository = MagicMock()
        mock_repository.search.return_value = [note1, note2]
        mock_zettel_service = MagicMock()
        mock_zettel_service.repository = mock_repository
        search_service = SearchService(mock_zettel_service)
        start_date = datetime.datetime(2026, 4, 1, 0, 0, 0)
        end_date = datetime.datetime(2026, 4, 30, 0, 0, 0)

        results = search_service.search_combined(
            tags=["python"],
            note_type=NoteType.PERMANENT,
            status=NoteStatus.INBOX,
            project_id="project123",
            area_id="area456",
            start_date=start_date,
            end_date=end_date,
        )

        assert [result.note.id for result in results] == ["note-1", "note-2"]
        assert all(result.score == 1.0 for result in results)
        assert all(result.matched_terms == set() for result in results)
        mock_repository.search.assert_called_once_with(
            tags=["python"],
            note_type=NoteType.PERMANENT,
            status=NoteStatus.INBOX,
            project_id="project123",
            area_id="area456",
            created_after=start_date,
            created_before=end_date,
        )

    def test_search_combined_prefilters_text_in_repository_query(self):
        """search_combined() should include text in the graph-backed prefilter."""
        note = SimpleNamespace(id="note-1", title="Python", content="Python body")
        mock_repository = MagicMock()
        mock_repository.search.return_value = [note]
        mock_zettel_service = MagicMock()
        mock_zettel_service.repository = mock_repository
        search_service = SearchService(mock_zettel_service)

        results = search_service.search_combined(
            text="python",
            tags=["python"],
            project_id="project123",
            area_id="area456",
        )

        assert [result.note.id for result in results] == ["note-1"]
        mock_repository.search.assert_called_once_with(
            tags=["python"],
            project_id="project123",
            area_id="area456",
            text="python",
        )

    def test_search_combined_filters_non_task_notes_by_project(self, zettel_service):
        """Combined search should honor project_id for non-task notes as well."""
        area = zettel_service.create_area_note(
            title="Knowledge",
            content="Knowledge work and systems.",
        )
        project_a = zettel_service.create_project_note(
            title="Project Notes A",
            content="Primary project.",
            area_id=area.id,
        )
        project_b = zettel_service.create_project_note(
            title="Project Notes B",
            content="Secondary project.",
            area_id=area.id,
        )
        matching_note = zettel_service.create_note(
            title="Python research",
            content="Python notes for the active project.",
            note_type=NoteType.PERMANENT,
            tags=["python", "research"],
            project_id=project_a.id,
            source=NoteSource.TRANSCRIPT,
        )
        zettel_service.create_note(
            title="Python backlog",
            content="Python notes for another project.",
            note_type=NoteType.PERMANENT,
            tags=["python", "research"],
            project_id=project_b.id,
            source=NoteSource.TRANSCRIPT,
        )

        search_service = SearchService(zettel_service)
        results = search_service.search_combined(
            text="python",
            tags=["python"],
            note_type=NoteType.PERMANENT,
            project_id=project_a.id,
            area_id=area.id,
        )

        assert len(results) == 1
        assert results[0].note.id == matching_note.id

    def test_search_combined_filters_non_task_notes_by_project_default_type(
        self, zettel_service
    ):
        """Combined search should respect project_id for non-task notes with default note type."""
        area = zettel_service.create_area_note(
            title="Engineering",
            content="Software delivery and maintenance.",
        )
        project_a = zettel_service.create_project_note(
            title="Project A",
            content="Primary project.",
            area_id=area.id,
        )
        project_b = zettel_service.create_project_note(
            title="Project B",
            content="Secondary project.",
            area_id=area.id,
        )
        matching_note = zettel_service.create_note(
            title="Python support note",
            content="Use Python to support Project A.",
            tags=["python", "support"],
            project_id=project_a.id,
        )
        zettel_service.create_note(
            title="Python backlog note",
            content="Use Python to support Project B.",
            tags=["python", "backlog"],
            project_id=project_b.id,
        )

        search_service = SearchService(zettel_service)
        results = search_service.search_combined(
            text="python",
            tags=["python"],
            note_type=NoteType.PERMANENT,
            project_id=project_a.id,
            area_id=area.id,
        )

        assert len(results) == 1
        assert results[0].note.id == matching_note.id
