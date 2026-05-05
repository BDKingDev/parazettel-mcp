# tests/test_mcp_server.py
"""Tests for the MCP server implementation."""
import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from parazettel_mcp.models.schema import LinkType, NoteSource, NoteStatus, NoteType
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer


class TestMcpServer:
    """Tests for the ZettelkastenMcpServer class."""

    def setup_method(self):
        """Set up test environment before each test."""
        # Capture the tool decorator functions when registering
        self.registered_tools = {}

        # Create a mock for FastMCP
        self.mock_mcp = MagicMock()

        # Mock the tool decorator to capture registered functions BEFORE server creation
        def mock_tool_decorator(*args, **kwargs):
            def tool_wrapper(func):
                # Store the function with its name
                name = kwargs.get("name")
                self.registered_tools[name] = func
                return func

            return tool_wrapper

        self.mock_mcp.tool = mock_tool_decorator

        # Mock the ZettelService and SearchService
        self.mock_zettel_service = MagicMock()
        self.mock_search_service = MagicMock()

        # Create patchers for FastMCP, ZettelService, and SearchService
        self.mcp_patcher = patch(
            "parazettel_mcp.server.mcp_server.FastMCP", return_value=self.mock_mcp
        )
        self.zettel_patcher = patch(
            "parazettel_mcp.server.mcp_server.ZettelService",
            return_value=self.mock_zettel_service,
        )
        self.search_patcher = patch(
            "parazettel_mcp.server.mcp_server.SearchService",
            return_value=self.mock_search_service,
        )

        # Start the patchers
        self.mcp_patcher.start()
        self.zettel_patcher.start()
        self.search_patcher.start()

        # Create a server instance AFTER setting up the mocks
        self.server = ZettelkastenMcpServer()

    def teardown_method(self):
        """Clean up after each test."""
        self.mcp_patcher.stop()
        self.zettel_patcher.stop()
        self.search_patcher.stop()

    def test_server_initialization(self):
        """Test server initialization."""
        # Check services are initialized
        assert self.mock_zettel_service.initialize.called
        assert self.mock_search_service.initialize.called

    def test_create_note_tool(self):
        """Test the pzk_create_note tool."""
        # Check the tool is registered
        assert "pzk_create_note" in self.registered_tools
        # Set up return value for create_note
        mock_note = MagicMock()
        mock_note.id = "test123"
        mock_area = MagicMock()
        mock_area.note_type = NoteType.AREA
        self.mock_zettel_service.create_note.return_value = mock_note
        self.mock_zettel_service.get_note.return_value = mock_area
        # Call the tool function directly
        create_note_func = self.registered_tools["pzk_create_note"]
        result = create_note_func(
            title="Test Note",
            content="Test content",
            note_type="permanent",
            tags="tag1, tag2",
            source="transcript",
            area_id="area123",
        )
        # Verify result
        assert "successfully" in result
        assert mock_note.id in result
        # Verify service call
        self.mock_zettel_service.create_note.assert_called_with(
            title="Test Note",
            content="Test content",
            note_type=NoteType.PERMANENT,
            tags=["tag1", "tag2"],
            source=NoteSource.TRANSCRIPT,
            status=None,
            project_id=None,
            area_id="area123",
        )

    def test_create_note_tool_passes_status(self):
        """pzk_create_note forwards note status for knowledge-note triage."""
        mock_note = MagicMock()
        mock_note.id = "test123"
        mock_area = MagicMock()
        mock_area.note_type = NoteType.AREA
        self.mock_zettel_service.create_note.return_value = mock_note
        self.mock_zettel_service.get_note.return_value = mock_area

        create_note_func = self.registered_tools["pzk_create_note"]
        result = create_note_func(
            title="Inbox Note",
            content="Captured for later processing.",
            note_type="permanent",
            source="transcript",
            status="inbox",
            area_id="area123",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_note.assert_called_with(
            title="Inbox Note",
            content="Captured for later processing.",
            note_type=NoteType.PERMANENT,
            tags=[],
            source=NoteSource.TRANSCRIPT,
            status=NoteStatus.INBOX,
            project_id=None,
            area_id="area123",
        )

    def test_create_note_tool_rejects_invalid_status(self):
        """pzk_create_note returns the valid note-status list for bad values."""
        create_note_func = self.registered_tools["pzk_create_note"]

        result = create_note_func(
            title="Inbox Note",
            content="Captured for later processing.",
            note_type="permanent",
            source="transcript",
            status="flying",
        )

        assert "Invalid status" in result
        assert "inbox" in result
        self.mock_zettel_service.create_note.assert_not_called()

    def test_create_note_tool_requires_source_for_non_area(self):
        """pzk_create_note requires source for all note types except area."""
        create_note_func = self.registered_tools["pzk_create_note"]

        result = create_note_func(
            title="Test Note",
            content="Test content",
            note_type="permanent",
            tags="tag1, tag2",
        )

        assert "source is required" in result
        self.mock_zettel_service.create_note.assert_not_called()

    def test_create_note_tool_requires_area_or_project_for_non_area(self):
        """pzk_create_note requires routing for non-area note types."""
        create_note_func = self.registered_tools["pzk_create_note"]

        result = create_note_func(
            title="Test Note",
            content="Test content",
            note_type="permanent",
            source="transcript",
        )

        assert "area_id or project_id is required" in result
        self.mock_zettel_service.create_note.assert_not_called()

    def test_create_note_tool_allows_area_without_source(self):
        """pzk_create_note allows area notes to omit source."""
        mock_note = MagicMock()
        mock_note.id = "area123"
        self.mock_zettel_service.create_note.return_value = mock_note

        create_note_func = self.registered_tools["pzk_create_note"]
        result = create_note_func(
            title="Household Systems",
            content="Ongoing responsibilities.",
            note_type="area",
            tags="home",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_note.assert_called_with(
            title="Household Systems",
            content="Ongoing responsibilities.",
            note_type=NoteType.AREA,
            tags=["home"],
            source=NoteSource.MANUAL,
            status=None,
            project_id=None,
            area_id=None,
        )

    def test_create_note_tool_inherits_area_from_project(self):
        """pzk_create_note inherits area_id from the linked project."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        mock_project = MagicMock()
        mock_project.note_type = NoteType.PROJECT
        mock_project.area_id = "area123"
        mock_area = MagicMock()
        mock_area.note_type = NoteType.AREA
        self.mock_zettel_service.create_note.return_value = mock_note
        self.mock_zettel_service.get_note.side_effect = [mock_project, mock_area]

        create_note_func = self.registered_tools["pzk_create_note"]
        result = create_note_func(
            title="Project Note",
            content="Inherited routing.",
            note_type="permanent",
            source="transcript",
            project_id="project123",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_note.assert_called_with(
            title="Project Note",
            content="Inherited routing.",
            note_type=NoteType.PERMANENT,
            tags=[],
            source=NoteSource.TRANSCRIPT,
            status=None,
            project_id="project123",
            area_id="area123",
        )

    def test_create_note_tool_rejects_project_area_mismatch(self):
        """pzk_create_note rejects area_id values that conflict with project routing."""
        mock_project = MagicMock()
        mock_project.note_type = NoteType.PROJECT
        mock_project.area_id = "area123"
        self.mock_zettel_service.get_note.return_value = mock_project

        create_note_func = self.registered_tools["pzk_create_note"]
        result = create_note_func(
            title="Project Note",
            content="Bad routing.",
            note_type="permanent",
            source="transcript",
            project_id="project123",
            area_id="area999",
        )

        assert "does not match project" in result
        self.mock_zettel_service.create_note.assert_not_called()

    def test_create_note_tool_rejects_invalid_source(self):
        """pzk_create_note rejects unknown source values."""
        create_note_func = self.registered_tools["pzk_create_note"]

        result = create_note_func(
            title="Test Note",
            content="Test content",
            note_type="permanent",
            source="invalid",
            area_id="area123",
        )

        assert "Invalid source" in result
        self.mock_zettel_service.create_note.assert_not_called()

    def test_create_project_tool_requires_and_passes_source(self):
        """pzk_create_project validates and forwards source."""
        assert "pzk_create_project" in self.registered_tools
        mock_project = MagicMock()
        mock_project.id = "project123"
        mock_area = MagicMock()
        mock_area.note_type = NoteType.AREA
        self.mock_zettel_service.create_project_note.return_value = mock_project
        self.mock_zettel_service.get_note.return_value = mock_area

        create_project_func = self.registered_tools["pzk_create_project"]
        result = create_project_func(
            title="Launch feature",
            content="Ship by end of quarter.",
            source="transcript",
            area_id="area123",
            tags="product, launch",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_project_note.assert_called_with(
            title="Launch feature",
            content="Ship by end of quarter.",
            outcome=None,
            deadline=None,
            area_id="area123",
            project_id=None,
            tags=["product", "launch"],
            source=NoteSource.TRANSCRIPT,
        )

    def test_create_project_tool_can_create_subproject(self):
        """pzk_create_project should accept parent_project_id for advanced subproject creation."""
        mock_project = MagicMock()
        mock_project.id = "project123"
        mock_parent = MagicMock()
        mock_parent.note_type = NoteType.PROJECT
        mock_parent.area_id = "area123"
        mock_area = MagicMock()
        mock_area.note_type = NoteType.AREA
        self.mock_zettel_service.create_project_note.return_value = mock_project
        self.mock_zettel_service.get_note.side_effect = (
            lambda note_id: {
                "project999": mock_parent,
                "area123": mock_area,
            }.get(note_id)
        )

        create_project_func = self.registered_tools["pzk_create_project"]
        result = create_project_func(
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            source="transcript",
            parent_project_id="project999",
            tags="child",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_project_note.assert_called_with(
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            outcome=None,
            deadline=None,
            area_id="area123",
            project_id="project999",
            tags=["child"],
            source=NoteSource.TRANSCRIPT,
        )

    def test_create_subproject_tool_requires_parent_and_passes_source(self):
        """pzk_create_subproject should inherit the parent project area."""
        assert "pzk_create_subproject" in self.registered_tools
        mock_project = MagicMock()
        mock_project.id = "project123"
        mock_parent = MagicMock()
        mock_parent.note_type = NoteType.PROJECT
        mock_parent.area_id = "area123"
        self.mock_zettel_service.create_project_note.return_value = mock_project
        self.mock_zettel_service.get_note.return_value = mock_parent

        create_subproject_func = self.registered_tools["pzk_create_subproject"]
        result = create_subproject_func(
            parent_project_id="project999",
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            source="transcript",
            tags="child, launch",
        )

        assert "successfully" in result
        self.mock_zettel_service.create_project_note.assert_called_with(
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            outcome=None,
            deadline=None,
            area_id="area123",
            project_id="project999",
            tags=["child", "launch"],
            source=NoteSource.TRANSCRIPT,
        )

    def test_create_subproject_tool_rejects_invalid_parent(self):
        """pzk_create_subproject should validate the parent project note."""
        create_subproject_func = self.registered_tools["pzk_create_subproject"]
        self.mock_zettel_service.get_note.return_value = None

        result = create_subproject_func(
            parent_project_id="missing-project",
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            source="transcript",
        )

        assert "is not a valid project note" in result
        self.mock_zettel_service.create_project_note.assert_not_called()

    def test_create_subproject_tool_strips_parent_project_id(self):
        """pzk_create_subproject should normalize parent_project_id before lookup."""
        mock_project = MagicMock()
        mock_project.id = "project123"
        mock_parent = MagicMock()
        mock_parent.note_type = NoteType.PROJECT
        mock_parent.area_id = "area123"
        self.mock_zettel_service.create_project_note.return_value = mock_project
        self.mock_zettel_service.get_note.return_value = mock_parent

        create_subproject_func = self.registered_tools["pzk_create_subproject"]
        result = create_subproject_func(
            parent_project_id="  project999  ",
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            source="transcript",
        )

        assert "successfully" in result
        self.mock_zettel_service.get_note.assert_called_with("project999")
        self.mock_zettel_service.create_project_note.assert_called_with(
            title="Launch slice",
            content="Ship one slice of the larger effort.",
            outcome=None,
            deadline=None,
            area_id="area123",
            project_id="project999",
            tags=[],
            source=NoteSource.TRANSCRIPT,
        )

    def test_create_project_tool_requires_area(self):
        """pzk_create_project requires a valid area_id."""
        create_project_func = self.registered_tools["pzk_create_project"]

        result = create_project_func(
            title="Launch feature",
            content="Ship by end of quarter.",
            source="transcript",
            area_id="missing-area",
        )

        assert "is not a valid area note" in result
        self.mock_zettel_service.create_project_note.assert_not_called()

    def test_create_project_tool_rejects_invalid_source(self):
        """pzk_create_project rejects unknown source values."""
        create_project_func = self.registered_tools["pzk_create_project"]

        result = create_project_func(
            title="Launch feature",
            content="Ship by end of quarter.",
            source="invalid",
            area_id="area123",
        )

        assert "Invalid source" in result
        self.mock_zettel_service.create_project_note.assert_not_called()

    def test_update_note_tool_passes_status(self):
        """pzk_update_note forwards note status updates."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(note_id="note123", status="inbox")

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            status=NoteStatus.INBOX,
        )

    def test_update_note_tool_passes_project_and_area_routing(self):
        """pzk_update_note forwards project_id/area_id routing changes."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(
            note_id="note123", project_id="project123", area_id="area456"
        )

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            project_id="project123",
            area_id="area456",
        )

    def test_update_note_tool_uses_parent_project_id_alias(self):
        """pzk_update_note should map parent_project_id to project_id broadly."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        mock_note.note_type = NoteType.PERMANENT
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(
            note_id="note123",
            parent_project_id="project123",
        )

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            project_id="project123",
        )

    def test_update_note_tool_rejects_conflicting_parent_and_project_ids(self):
        """pzk_update_note should reject conflicting routing aliases."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        mock_note.note_type = NoteType.PERMANENT
        self.mock_zettel_service.get_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(
            note_id="note123",
            project_id="project123",
            parent_project_id="project456",
        )

        assert "must match" in result
        self.mock_zettel_service.update_note.assert_not_called()

    def test_update_note_tool_clears_project_id_with_empty_string(self):
        """pzk_update_note clears project routing when given an empty string."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(note_id="note123", project_id="")

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            project_id=None,
        )

    def test_update_note_tool_clears_parent_project_id_with_empty_string(self):
        """pzk_update_note clears a project parent when given an empty parent_project_id."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        mock_note.note_type = NoteType.PROJECT
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(note_id="note123", parent_project_id="")

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            project_id=None,
        )

    def test_update_note_tool_clears_status_with_empty_string(self):
        """pzk_update_note clears status when given an empty string."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        self.mock_zettel_service.get_note.return_value = mock_note
        self.mock_zettel_service.update_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(note_id="note123", status="")

        assert "updated successfully" in result
        self.mock_zettel_service.update_note.assert_called_with(
            note_id="note123",
            title=None,
            content=None,
            note_type=None,
            tags=None,
            status=None,
        )

    def test_update_note_tool_rejects_invalid_status(self):
        """pzk_update_note returns the valid note-status list for bad values."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        self.mock_zettel_service.get_note.return_value = mock_note

        update_note_func = self.registered_tools["pzk_update_note"]
        result = update_note_func(note_id="note123", status="flying")

        assert "Invalid status" in result
        assert "inbox" in result
        self.mock_zettel_service.update_note.assert_not_called()

    def test_get_note_tool(self):
        """Test the pzk_get_note tool."""
        # Check the tool is registered
        assert "pzk_get_note" in self.registered_tools

        # Set up mock note
        mock_note = MagicMock()
        mock_note.id = "test123"
        mock_note.title = "Test Note"
        mock_note.content = "# Test Note\n\nTest content"
        mock_note.note_type = NoteType.PERMANENT
        mock_note.project_id = "project123"
        mock_note.area_id = "area456"
        mock_note.created_at.isoformat.return_value = "2023-01-01T12:00:00"
        mock_note.updated_at.isoformat.return_value = "2023-01-01T12:30:00"
        mock_tag1 = MagicMock()
        mock_tag1.name = "tag1"
        mock_tag2 = MagicMock()
        mock_tag2.name = "tag2"
        mock_note.tags = [mock_tag1, mock_tag2]
        mock_note.links = []

        # Set up return value for get_note
        self.mock_zettel_service.get_note.return_value = mock_note

        # Call the tool function directly
        get_note_func = self.registered_tools["pzk_get_note"]
        result = get_note_func(identifier="test123")

        # Verify result — title appears in content body, not duplicated in header
        assert "ID: test123" in result
        assert "Project ID: project123" in result
        assert "Area ID: area456" in result
        assert "Test content" in result

        # Verify service call
        self.mock_zettel_service.get_note.assert_called_with("test123")

    def test_get_note_tool_renders_current_heading_once(self):
        """pzk_get_note should reflect the current heading without stale duplicates."""
        mock_note = MagicMock()
        mock_note.id = "note123"
        mock_note.title = "Renamed Note"
        mock_note.content = "# Renamed Note\n\nBody content"
        mock_note.note_type = NoteType.PERMANENT
        mock_note.project_id = None
        mock_note.area_id = None
        mock_note.created_at.isoformat.return_value = "2023-01-01T12:00:00"
        mock_note.updated_at.isoformat.return_value = "2023-01-01T12:30:00"
        mock_note.tags = []
        self.mock_zettel_service.get_note.return_value = mock_note

        get_note_func = self.registered_tools["pzk_get_note"]
        result = get_note_func(identifier="note123")

        assert result.count("# Renamed Note") == 1
        assert "# Old Note" not in result

    def test_get_notes_tool_formats_multiple_notes_and_missing_identifiers(self):
        """pzk_get_notes should batch note retrieval and report missing identifiers."""
        first_note = MagicMock()
        first_note.id = "note123"
        first_note.title = "First Note"
        first_note.content = "# First Note\n\nFirst content"
        first_note.note_type = NoteType.PERMANENT
        first_note.project_id = "project123"
        first_note.area_id = "area123"
        first_note.created_at.isoformat.return_value = "2026-04-23T09:00:00"
        first_note.updated_at.isoformat.return_value = "2026-04-23T09:15:00"
        first_note.tags = []

        second_note = MagicMock()
        second_note.id = "note456"
        second_note.title = "Working Notes"
        second_note.content = "# Working Notes\n\nSecond content"
        second_note.note_type = NoteType.LITERATURE
        second_note.project_id = "project123"
        second_note.area_id = "area123"
        second_note.created_at.isoformat.return_value = "2026-04-23T10:00:00"
        second_note.updated_at.isoformat.return_value = "2026-04-23T10:30:00"
        second_note.tags = []

        self.mock_zettel_service.get_note.side_effect = (
            lambda identifier: {"note123": first_note}.get(identifier)
        )
        self.mock_zettel_service.get_note_by_title.side_effect = (
            lambda identifier: {"Working Notes": second_note}.get(identifier)
        )

        get_notes_func = self.registered_tools["pzk_get_notes"]
        result = get_notes_func(
            identifiers=["note123", "Working Notes", "note123", "missing999", "   "]
        )

        assert "Notes retrieved (2/3):" in result
        assert "ID: note123" in result
        assert "ID: note456" in result
        assert "# First Note" in result
        assert "# Working Notes" in result
        assert "Missing identifiers:" in result
        assert "- missing999" in result
        self.mock_zettel_service.get_note.assert_has_calls(
            [call("note123"), call("Working Notes"), call("missing999")]
        )
        self.mock_zettel_service.get_note_by_title.assert_has_calls(
            [call("Working Notes"), call("missing999")]
        )

    def test_get_notes_tool_rejects_empty_identifier_list(self):
        """pzk_get_notes should require at least one non-empty identifier."""
        get_notes_func = self.registered_tools["pzk_get_notes"]

        result = get_notes_func(identifiers=["", "   "])

        assert result == "Provide at least one note identifier."
        self.mock_zettel_service.get_note.assert_not_called()
        self.mock_zettel_service.get_note_by_title.assert_not_called()

    def test_get_notes_tool_deduplicates_same_note_resolved_multiple_ways(self):
        """pzk_get_notes should not repeat a note resolved by both ID and title."""
        note = MagicMock()
        note.id = "note123"
        note.title = "First Note"
        note.content = "# First Note\n\nUseful context."
        note.note_type = NoteType.PERMANENT
        note.project_id = "project123"
        note.area_id = "area123"
        note.created_at.isoformat.return_value = "2026-04-23T09:00:00"
        note.updated_at.isoformat.return_value = "2026-04-23T09:15:00"
        note.tags = []

        self.mock_zettel_service.get_note.side_effect = (
            lambda identifier: note if identifier == "note123" else None
        )
        self.mock_zettel_service.get_note_by_title.side_effect = (
            lambda title: note if title == "First Note" else None
        )

        get_notes_func = self.registered_tools["pzk_get_notes"]
        result = get_notes_func(identifiers=["note123", "First Note"])

        assert "Notes retrieved (1/2):" in result
        assert result.count("# First Note") == 1
        assert "Missing identifiers" not in result

    def test_get_notes_by_tag_tool_formats_matching_notes_and_respects_limit(self):
        """pzk_get_notes_by_tag should render full note context for exact-tag matches."""
        first_note = MagicMock()
        first_note.id = "note123"
        first_note.note_type = NoteType.PERMANENT
        first_note.created_at.isoformat.return_value = "2026-04-23T09:00:00"
        first_note.updated_at.isoformat.return_value = "2026-04-23T09:15:00"
        first_note.project_id = "project123"
        first_note.area_id = "area123"
        first_note.tags = []
        first_note.content = "# First Note\n\nUseful context."

        second_note = MagicMock()
        second_note.id = "note456"
        second_note.note_type = NoteType.LITERATURE
        second_note.created_at.isoformat.return_value = "2026-04-23T10:00:00"
        second_note.updated_at.isoformat.return_value = "2026-04-23T10:30:00"
        second_note.project_id = "project123"
        second_note.area_id = "area123"
        second_note.tags = []
        second_note.content = "# Second Note\n\nBackground material."

        self.mock_zettel_service.get_notes_by_tag.return_value = [first_note, second_note]

        get_notes_by_tag_func = self.registered_tools["pzk_get_notes_by_tag"]
        result = get_notes_by_tag_func(tag=" research ", limit=1)

        assert "Notes tagged 'research' (1):" in result
        assert "ID: note123" in result
        assert "# First Note" in result
        assert "note456" not in result
        self.mock_zettel_service.get_notes_by_tag.assert_called_once_with("research")

    def test_get_notes_by_tag_tool_rejects_non_positive_limits(self):
        """pzk_get_notes_by_tag should require a positive limit."""
        get_notes_by_tag_func = self.registered_tools["pzk_get_notes_by_tag"]

        zero_result = get_notes_by_tag_func(tag="research", limit=0)
        negative_result = get_notes_by_tag_func(tag="research", limit=-1)

        assert zero_result == "Limit must be greater than 0."
        assert negative_result == "Limit must be greater than 0."
        self.mock_zettel_service.get_notes_by_tag.assert_not_called()

    def test_get_notes_by_tag_tool_handles_empty_and_missing_results(self):
        """pzk_get_notes_by_tag should reject blank tags and handle empty matches."""
        get_notes_by_tag_func = self.registered_tools["pzk_get_notes_by_tag"]

        empty_result = get_notes_by_tag_func(tag="   ")
        self.mock_zettel_service.get_notes_by_tag.return_value = []
        missing_result = get_notes_by_tag_func(tag="research")

        assert empty_result == "Provide a tag name."
        assert missing_result == "No notes found with tag 'research'."
        self.mock_zettel_service.get_notes_by_tag.assert_called_once_with("research")

    def test_create_link_tool(self):
        """Test the pzk_create_link tool."""
        # Check the tool is registered
        assert "pzk_create_link" in self.registered_tools

        # Set up mock notes
        source_note = MagicMock()
        source_note.id = "source123"
        target_note = MagicMock()
        target_note.id = "target456"

        # Set up return value for create_link
        self.mock_zettel_service.create_link.return_value = (source_note, target_note)

        # Call the tool function directly
        create_link_func = self.registered_tools["pzk_create_link"]
        result = create_link_func(
            source_id="source123",
            target_id="target456",
            link_type="extends",
            description="Test link",
            bidirectional=True,
        )

        # Verify result
        assert "Bidirectional link created" in result
        assert "source123" in result
        assert "target456" in result

        # Verify service call
        self.mock_zettel_service.create_link.assert_called_with(
            source_id="source123",
            target_id="target456",
            link_type=LinkType.EXTENDS,
            description="Test link",
            bidirectional=True,
        )

    def test_search_notes_tool(self):
        """Test the pzk_search_notes tool."""
        # Check the tool is registered
        assert "pzk_search_notes" in self.registered_tools

        # Set up mock notes
        mock_note1 = MagicMock()
        mock_note1.id = "note1"
        mock_note1.title = "Note 1"
        mock_note1.content = "This is note 1 content"
        mock_tag1 = MagicMock()
        mock_tag1.name = "tag1"
        mock_tag2 = MagicMock()
        mock_tag2.name = "tag2"
        mock_note1.tags = [mock_tag1, mock_tag2]
        mock_note1.created_at.strftime.return_value = "2023-01-01"

        mock_note2 = MagicMock()
        mock_note2.id = "note2"
        mock_note2.title = "Note 2"
        mock_note2.content = "This is note 2 content"
        # mock_note2.tags = [MagicMock(name="tag1")]
        mock_tag1 = MagicMock()
        mock_tag1.name = "tag1"
        mock_note2.tags = [mock_tag1]
        mock_note2.created_at.strftime.return_value = "2023-01-02"

        # Set up mock search results
        mock_result1 = MagicMock()
        mock_result1.note = mock_note1
        mock_result2 = MagicMock()
        mock_result2.note = mock_note2

        self.mock_search_service.search_combined.return_value = [
            mock_result1,
            mock_result2,
        ]

        # Call the tool function directly
        search_notes_func = self.registered_tools["pzk_search_notes"]
        result = search_notes_func(
            query="test query",
            tags="tag1, tag2",
            note_type="permanent",
            status="ready",
            project_id="project123",
            area_id="area456",
            limit=10,
        )

        # Verify result
        assert "Found 2 matching notes" in result
        assert "Note 1" in result
        assert "Note 2" in result

        # Verify service call
        self.mock_search_service.search_combined.assert_called_with(
            text="test query",
            tags=["tag1", "tag2"],
            note_type=NoteType.PERMANENT,
            status=NoteStatus.READY,
            project_id="project123",
            area_id="area456",
        )

    def test_search_notes_rejects_invalid_status(self):
        """pzk_search_notes returns the valid status list for invalid status values."""
        search_notes_func = self.registered_tools["pzk_search_notes"]
        result = search_notes_func(status="flying")

        assert "Invalid status" in result
        assert "inbox" in result
        self.mock_search_service.search_combined.assert_not_called()

    def test_create_task_tool_response_includes_title_and_id(self):
        """pzk_create_task returns a human-readable success message with title and ID."""
        mock_task = MagicMock()
        mock_task.id = "task123"
        mock_task.title = "Weekly review"
        self.mock_zettel_service.create_task.return_value = mock_task

        create_task_func = self.registered_tools["pzk_create_task"]
        result = create_task_func(
            title="Weekly review",
            content="Review projects and notes.",
            project_id="project123",
        )

        assert "Weekly review" in result
        assert "task123" in result
        self.mock_zettel_service.create_task.assert_called_with(
            title="Weekly review",
            content="Review projects and notes.",
            status=NoteStatus.INBOX,
            tags=[],
            project_id="project123",
            area_id=None,
            due_date=None,
            remind_at=None,
            priority=None,
            recurrence_rule=None,
            estimated_minutes=None,
            source=NoteSource.MANUAL,
        )

    def test_update_task_tool_registered(self):
        """pzk_update_task tool should be registered."""
        assert "pzk_update_task" in self.registered_tools

    def test_update_task_updates_due_date(self):
        """pzk_update_task updates due_date on a task note."""
        mock_task = MagicMock()
        mock_task.id = "task001"
        mock_task.note_type = NoteType.TASK
        mock_task.tags = []
        self.mock_zettel_service.get_note.return_value = mock_task
        self.mock_zettel_service.update_task.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", due_date="2026-04-01")

        assert "updated successfully" in result
        self.mock_zettel_service.update_task.assert_called_once_with(
            "task001", due_date=datetime.date(2026, 4, 1)
        )

    def test_update_task_rejects_non_task_note(self):
        """pzk_update_task returns an error if the note is not a task."""
        mock_note = MagicMock()
        mock_note.note_type = NoteType.PERMANENT
        self.mock_zettel_service.get_note.return_value = mock_note

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="note001", priority=3)

        assert "not a task" in result
        self.mock_zettel_service.update_task.assert_not_called()

    def test_update_task_rejects_invalid_due_date(self):
        """pzk_update_task returns an error for a malformed due_date."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        self.mock_zettel_service.get_note.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", due_date="not-a-date")

        assert "Invalid due_date" in result
        self.mock_zettel_service.update_task.assert_not_called()

    def test_update_task_rejects_invalid_status(self):
        """pzk_update_task returns an error for an unrecognised status value."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        self.mock_zettel_service.get_note.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", status="flying")

        assert "Invalid status" in result
        self.mock_zettel_service.update_task.assert_not_called()

    def test_update_task_routes_status_through_service(self):
        """pzk_update_task routes status changes through service.update_task."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        mock_task.recurrence_rule = None
        self.mock_zettel_service.get_note.return_value = mock_task
        self.mock_zettel_service.update_task.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", status="done")

        assert "done" in result
        self.mock_zettel_service.update_task.assert_called_once_with(
            "task001", status=NoteStatus.DONE
        )

    def test_update_task_announces_recurring_spawn(self):
        """pzk_update_task includes 'recurring instance created' when a recurring task is completed."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        mock_task.recurrence_rule = "weekly"
        self.mock_zettel_service.get_note.return_value = mock_task
        self.mock_zettel_service.update_task.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", status="done")

        assert "recurring instance created" in result

    def test_update_task_routes_project_reassignment_through_service(self):
        """pzk_update_task should forward project reassignment through service.update_task."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        mock_task.recurrence_rule = None
        self.mock_zettel_service.get_note.return_value = mock_task
        self.mock_zettel_service.update_task.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", project_id="project999", priority=3)

        assert "updated successfully" in result
        self.mock_zettel_service.update_task.assert_called_once_with(
            "task001", project_id="project999", priority=3
        )

    def test_update_task_accepts_parent_project_id_alias(self):
        """pzk_update_task should accept parent_project_id as a clearer alias."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        mock_task.recurrence_rule = None
        self.mock_zettel_service.get_note.return_value = mock_task
        self.mock_zettel_service.update_task.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(task_id="task001", parent_project_id="project999", priority=3)

        assert "updated successfully" in result
        self.mock_zettel_service.update_task.assert_called_once_with(
            "task001", project_id="project999", priority=3
        )

    def test_update_task_rejects_conflicting_parent_and_project_ids(self):
        """pzk_update_task should reject conflicting routing aliases."""
        mock_task = MagicMock()
        mock_task.note_type = NoteType.TASK
        mock_task.recurrence_rule = None
        self.mock_zettel_service.get_note.return_value = mock_task

        fn = self.registered_tools["pzk_update_task"]
        result = fn(
            task_id="task001",
            project_id="project123",
            parent_project_id="project999",
        )

        assert "must match" in result
        self.mock_zettel_service.update_task.assert_not_called()

    def test_get_tasks_tool_formats_results_and_parses_filters(self):
        """pzk_get_tasks should parse filters and render matching tasks."""
        ready_task = MagicMock()
        ready_task.id = "task123"
        ready_task.title = "Ready task"
        ready_task.status = NoteStatus.READY
        ready_task.due_date = datetime.date(2026, 4, 5)
        ready_task.priority = 3
        low_task = MagicMock()
        low_task.id = "task456"
        low_task.title = "Lower priority task"
        low_task.status = NoteStatus.READY
        low_task.due_date = None
        low_task.priority = None
        self.mock_zettel_service.get_tasks.return_value = [ready_task, low_task]

        fn = self.registered_tools["pzk_get_tasks"]
        result = fn(
            status="ready",
            project_id="project123",
            due_date="2026-04-05",
            priority=3,
            limit=5,
        )

        assert "Found 2 task(s)" in result
        assert "Ready task (ID: task123)" in result
        assert "Due: 2026-04-05" in result
        assert "Priority: 3" in result
        self.mock_zettel_service.get_tasks.assert_called_once_with(
            status=NoteStatus.READY,
            project_id="project123",
            due_date_before=datetime.date(2026, 4, 5),
            priority=3,
            limit=5,
        )

    def test_get_tasks_tool_rejects_invalid_due_date(self):
        """pzk_get_tasks should reject malformed due_date filters."""
        fn = self.registered_tools["pzk_get_tasks"]

        result = fn(due_date="not-a-date")

        assert "Invalid due_date" in result
        self.mock_zettel_service.get_tasks.assert_not_called()

    def test_get_todays_tasks_tool_formats_priorities_and_due_dates(self):
        """pzk_get_todays_tasks should render task priorities and due dates."""
        task = MagicMock()
        task.id = "task123"
        task.title = "Review inbox"
        task.status = NoteStatus.ACTIVE
        task.priority = 4
        task.due_date = datetime.date(2026, 4, 22)
        self.mock_zettel_service.get_todays_tasks.return_value = [task]

        fn = self.registered_tools["pzk_get_todays_tasks"]
        result = fn(include_overdue=False)

        assert "Today's tasks (1)" in result
        assert "1. [P4] Review inbox — due 2026-04-22 (ID: task123)" in result
        assert "Status: active" in result
        self.mock_zettel_service.get_todays_tasks.assert_called_once_with(False)

    def test_get_project_tasks_tool_filters_status_and_limits_results(self):
        """pzk_get_project_tasks should filter by status and respect the limit."""
        first_task = MagicMock()
        first_task.id = "task123"
        first_task.title = "First task"
        first_task.status = NoteStatus.READY
        first_task.due_date = datetime.date(2026, 4, 25)
        second_task = MagicMock()
        second_task.id = "task456"
        second_task.title = "Second task"
        second_task.status = NoteStatus.READY
        second_task.due_date = None
        third_task = MagicMock()
        third_task.id = "task789"
        third_task.title = "Third task"
        third_task.status = NoteStatus.READY
        third_task.due_date = None
        self.mock_zettel_service.get_project_tasks.return_value = [
            first_task,
            second_task,
            third_task,
        ]

        fn = self.registered_tools["pzk_get_project_tasks"]
        result = fn(project_id="project123", status="ready", limit=2)

        assert "Tasks for project project123 (2):" in result
        assert "First task (ID: task123)" in result
        assert "Second task (ID: task456)" in result
        assert "Third task" not in result
        self.mock_zettel_service.get_project_tasks.assert_called_once_with(
            "project123", NoteStatus.READY
        )

    def test_get_reminders_tool_formats_results(self):
        """pzk_get_reminders should render note/task reminders with type and date."""
        reminder = MagicMock()
        reminder.id = "task123"
        reminder.title = "Renew domain"
        reminder.note_type = NoteType.TASK
        reminder.remind_at = datetime.date(2026, 4, 22)
        self.mock_zettel_service.get_reminders.return_value = [reminder]

        fn = self.registered_tools["pzk_get_reminders"]
        result = fn(limit=10)

        assert "Reminders due (1):" in result
        assert "Renew domain (ID: task123)" in result
        assert "Type: task  Remind: 2026-04-22" in result
        self.mock_zettel_service.get_reminders.assert_called_once_with(10)

    def test_get_reminders_tool_handles_empty_results(self):
        """pzk_get_reminders should return a friendly empty-state message."""
        self.mock_zettel_service.get_reminders.return_value = []

        fn = self.registered_tools["pzk_get_reminders"]
        result = fn()

        assert result == "No reminders due today."

    def test_get_project_tool_lists_parent_subprojects_and_notes(self):
        """pzk_get_project should separate hierarchy context from routed notes."""
        mock_project = MagicMock()
        mock_project.id = "project123"
        mock_project.note_type = NoteType.PROJECT
        mock_project.area_id = "area123"
        mock_project.metadata = {"outcome": "Ship it"}
        mock_project.content = "# Project\n\nBody"

        active_task = MagicMock()
        active_task.id = "task111"
        active_task.title = "Ship beta"
        active_task.status = NoteStatus.ACTIVE
        active_task.priority = 4
        active_task.due_date = datetime.date(2026, 4, 24)
        ready_task = MagicMock()
        ready_task.id = "task222"
        ready_task.title = "Write release notes"
        ready_task.status = NoteStatus.READY
        ready_task.priority = 2
        ready_task.due_date = None
        done_task = MagicMock()
        done_task.id = "task333"
        done_task.title = "Archive spec"
        done_task.status = NoteStatus.DONE
        done_task.priority = None
        done_task.due_date = None
        linked_note = MagicMock()
        linked_note.id = "note123"
        linked_note.title = "Working Notes"
        linked_note.note_type = NoteType.PERMANENT
        parent_project = MagicMock()
        parent_project.id = "project999"
        parent_project.title = "Parent Project"
        parent_project.note_type = NoteType.PROJECT
        subproject = MagicMock()
        subproject.id = "project777"
        subproject.title = "Beta rollout"
        subproject.note_type = NoteType.PROJECT

        self.mock_zettel_service.get_note.return_value = mock_project
        self.mock_zettel_service.get_project_tasks.return_value = [
            ready_task,
            done_task,
            active_task,
        ]
        self.mock_zettel_service.get_project_notes.return_value = [linked_note]
        self.mock_zettel_service.get_parent_project.return_value = parent_project
        self.mock_zettel_service.get_subprojects.return_value = [subproject]

        fn = self.registered_tools["pzk_get_project"]
        result = fn(project_id="project123")

        assert "Area ID: area123" in result
        assert "Next Tasks:" in result
        assert "[active] Ship beta (ID: task111) - P4, due 2026-04-24" in result
        assert "[ready] Write release notes (ID: task222) - P2" in result
        assert "Archive spec" not in result
        assert "Parent Project:" in result
        assert "Parent Project (ID: project999)" in result
        assert "Subprojects:" in result
        assert "Beta rollout (ID: project777)" in result
        assert "Notes:" in result
        assert "Working Notes (ID: note123, type: permanent)" in result
        assert "Linked Projects:" not in result

    def test_get_project_notes_tool_formats_note_context_and_respects_limit(self):
        """pzk_get_project_notes should render full note context for project-scoped notes."""
        first_note = MagicMock()
        first_note.id = "note123"
        first_note.note_type = NoteType.PERMANENT
        first_note.created_at = datetime.datetime(2026, 4, 20, 9, 0, 0)
        first_note.updated_at = datetime.datetime(2026, 4, 22, 10, 30, 0)
        first_note.project_id = "project123"
        first_note.area_id = "area123"
        first_note.tags = []
        first_note.content = "# Working Notes\n\nUseful context."

        second_note = MagicMock()
        second_note.id = "note456"
        second_note.note_type = NoteType.LITERATURE
        second_note.created_at = datetime.datetime(2026, 4, 21, 8, 15, 0)
        second_note.updated_at = datetime.datetime(2026, 4, 22, 11, 45, 0)
        second_note.project_id = "project123"
        second_note.area_id = "area123"
        second_note.tags = []
        second_note.content = "# Research\n\nBackground material."

        self.mock_zettel_service.get_project_notes.return_value = [first_note, second_note]

        fn = self.registered_tools["pzk_get_project_notes"]
        result = fn(project_id="project123", limit=1)

        assert "Project notes for project123 (1):" in result
        assert "ID: note123" in result
        assert "Type: permanent" in result
        assert "# Working Notes" in result
        assert "note456" not in result
        self.mock_zettel_service.get_project_notes.assert_called_once_with("project123")

    def test_get_project_notes_tool_handles_empty_results(self):
        """pzk_get_project_notes should return a friendly empty-state message."""
        self.mock_zettel_service.get_project_notes.return_value = []

        fn = self.registered_tools["pzk_get_project_notes"]
        result = fn(project_id="project123")

        assert result == "No project notes found for project project123."

    def test_delete_note_tool_deletes_existing_note(self):
        """pzk_delete_note removes a note after confirming it exists."""
        self.mock_zettel_service.get_note.return_value = MagicMock()

        fn = self.registered_tools["pzk_delete_note"]
        result = fn(note_id="note123")

        assert result == "Note deleted successfully: note123"
        self.mock_zettel_service.get_note.assert_called_once_with("note123")
        self.mock_zettel_service.delete_note.assert_called_once_with("note123")

    def test_remove_link_tool_passes_bidirectional_flag(self):
        """pzk_remove_link forwards the source, target, and bidirectional flag."""
        self.mock_zettel_service.remove_link.return_value = (MagicMock(), MagicMock())

        fn = self.registered_tools["pzk_remove_link"]
        result = fn(source_id="source123", target_id="target456", bidirectional=True)

        assert result == "Bidirectional link removed between source123 and target456"
        self.mock_zettel_service.remove_link.assert_called_once_with(
            source_id="source123",
            target_id="target456",
            bidirectional=True,
        )

    def test_get_linked_notes_tool_formats_link_details(self):
        """pzk_get_linked_notes includes outgoing and incoming link metadata."""
        source_note = MagicMock()
        outgoing_link = MagicMock()
        outgoing_link.target_id = "target456"
        outgoing_link.link_type = LinkType.EXTENDS
        outgoing_link.description = "Builds on the concept"
        source_note.links = [outgoing_link]

        linked_note = MagicMock()
        linked_note.id = "target456"
        linked_note.title = "Target Note"
        linked_note.content = "Target note body"
        linked_note.tags = []
        incoming_link = MagicMock()
        incoming_link.target_id = "source123"
        incoming_link.link_type = LinkType.SUPPORTS
        incoming_link.description = "Back-reference"
        linked_note.links = [incoming_link]

        self.mock_zettel_service.get_linked_notes.return_value = [linked_note]
        self.mock_zettel_service.get_note.return_value = source_note

        fn = self.registered_tools["pzk_get_linked_notes"]
        result = fn(note_id="source123", direction="both")

        assert "Found 1 both linked notes for source123" in result
        assert "Target Note (ID: target456)" in result
        assert "Link type: extends" in result
        assert "Incoming link type: supports" in result
        assert "Builds on the concept" in result
        assert "Back-reference" in result

    def test_get_all_tags_tool_sorts_results_alphabetically(self):
        """pzk_get_all_tags sorts tags case-insensitively before formatting."""
        zeta = MagicMock()
        zeta.name = "zeta"
        alpha = MagicMock()
        alpha.name = "Alpha"
        self.mock_zettel_service.get_all_tags.return_value = [zeta, alpha]

        fn = self.registered_tools["pzk_get_all_tags"]
        result = fn()

        assert "Found 2 tags" in result
        assert result.index("1. Alpha") < result.index("2. zeta")

    def test_find_similar_notes_tool_formats_similarity_and_preview(self):
        """pzk_find_similar_notes renders similarity scores and snippets."""
        similar = MagicMock()
        similar.id = "note456"
        similar.title = "Related Note"
        similar.content = "This note expands the original idea with more detail."
        tag = MagicMock()
        tag.name = "analysis"
        similar.tags = [tag]
        self.mock_zettel_service.find_similar_notes.return_value = [(similar, 0.82)]

        fn = self.registered_tools["pzk_find_similar_notes"]
        result = fn(note_id="note123", threshold=0.5, limit=3)

        assert "Found 1 similar notes for note123" in result
        assert "Similarity: 0.82" in result
        assert "Related Note (ID: note456)" in result
        assert "Tags: analysis" in result
        self.mock_zettel_service.find_similar_notes.assert_called_once_with(
            "note123", 0.5
        )

    def test_find_central_notes_tool_formats_connection_counts(self):
        """pzk_find_central_notes renders the ranked connection counts."""
        central = MagicMock()
        central.id = "note789"
        central.title = "Hub Note"
        central.content = "Central note body"
        tag = MagicMock()
        tag.name = "hub"
        central.tags = [tag]
        self.mock_search_service.find_central_notes.return_value = [(central, 7)]

        fn = self.registered_tools["pzk_find_central_notes"]
        result = fn(limit=5)

        assert "Central notes in the Zettelkasten" in result
        assert "Hub Note (ID: note789)" in result
        assert "Connections: 7" in result
        self.mock_search_service.find_central_notes.assert_called_once_with(5)

    def test_find_orphaned_notes_tool_formats_preview(self):
        """pzk_find_orphaned_notes renders each orphan with a content preview."""
        orphan = MagicMock()
        orphan.id = "note321"
        orphan.title = "Isolated Note"
        orphan.content = "A standalone note with no links to anything else."
        tag = MagicMock()
        tag.name = "orphan"
        orphan.tags = [tag]
        self.mock_search_service.find_orphaned_notes.return_value = [orphan]

        fn = self.registered_tools["pzk_find_orphaned_notes"]
        result = fn()

        assert "Found 1 orphaned notes" in result
        assert "Isolated Note (ID: note321)" in result
        assert "Tags: orphan" in result
        assert "A standalone note with no links" in result

    def test_list_notes_by_date_tool_formats_updated_range(self):
        """pzk_list_notes_by_date parses date bounds and renders updated notes."""
        note = MagicMock()
        note.id = "note111"
        note.title = "Fresh Note"
        note.content = "Recently updated content."
        note.updated_at = datetime.datetime(2026, 4, 2, 14, 30)
        tag = MagicMock()
        tag.name = "recent"
        note.tags = [tag]
        self.mock_search_service.find_notes_by_date_range.return_value = [note]

        fn = self.registered_tools["pzk_list_notes_by_date"]
        result = fn(
            start_date="2026-04-01",
            end_date="2026-04-02",
            use_updated=True,
            limit=5,
        )

        assert "Notes updated between 2026-04-01 and 2026-04-02" in result
        assert "Fresh Note (ID: note111)" in result
        assert "Updated: 2026-04-02 14:30" in result
        self.mock_search_service.find_notes_by_date_range.assert_called_once_with(
            start_date=datetime.datetime(2026, 4, 1, 0, 0),
            end_date=datetime.datetime(2026, 4, 2, 23, 59, 59),
            use_updated=True,
        )

    def test_rebuild_index_tool_reports_backup_and_count_delta(self):
        """pzk_rebuild_index includes backup information and note-count changes."""
        self.mock_zettel_service.get_all_notes.side_effect = [
            [MagicMock(), MagicMock(), MagicMock()],
            [MagicMock(), MagicMock()],
        ]
        self.mock_zettel_service.rebuild_index.return_value = "backup-2026-04-23.db"

        fn = self.registered_tools["pzk_rebuild_index"]
        result = fn()

        assert "Database index rebuilt successfully." in result
        assert "Backup created: backup-2026-04-23.db" in result
        assert "Notes processed: 2" in result
        assert "Change in note count: -1" in result

    def test_create_area_tool_passes_cadence_and_tags(self):
        """pzk_create_area forwards cadence and parsed tag values."""
        mock_area = MagicMock()
        mock_area.id = "area123"
        self.mock_zettel_service.create_area_note.return_value = mock_area

        fn = self.registered_tools["pzk_create_area"]
        result = fn(
            title="Household Systems",
            content="Ongoing home responsibilities.",
            cadence="weekly review",
            tags="home, chores",
        )

        assert result == "Area created successfully with ID: area123"
        self.mock_zettel_service.create_area_note.assert_called_once_with(
            title="Household Systems",
            content="Ongoing home responsibilities.",
            cadence="weekly review",
            tags=["home", "chores"],
        )

    def test_list_projects_tool_filters_done_and_sorts_by_deadline(self):
        """pzk_list_projects omits done projects by default and sorts by deadline."""
        later_project = MagicMock()
        later_project.id = "project-later"
        later_project.title = "Later Project"
        later_project.due_date = datetime.date(2026, 5, 1)
        later_project.status = NoteStatus.ACTIVE
        later_project.metadata = {"outcome": "Ship later"}

        done_project = MagicMock()
        done_project.id = "project-done"
        done_project.title = "Done Project"
        done_project.due_date = datetime.date(2026, 4, 1)
        done_project.status = NoteStatus.DONE
        done_project.metadata = {"outcome": "Already complete"}

        earlier_project = MagicMock()
        earlier_project.id = "project-early"
        earlier_project.title = "Earlier Project"
        earlier_project.due_date = datetime.date(2026, 4, 15)
        earlier_project.status = NoteStatus.READY
        earlier_project.metadata = {"outcome": "Ship first"}

        self.mock_zettel_service.search_notes.return_value = [
            later_project,
            done_project,
            earlier_project,
        ]

        fn = self.registered_tools["pzk_list_projects"]
        result = fn(include_done=False, limit=5)

        assert "Projects (2)" in result
        assert "Done Project" not in result
        assert result.index("Earlier Project") < result.index("Later Project")
        self.mock_zettel_service.search_notes.assert_called_once_with(
            note_type=NoteType.PROJECT
        )

    def test_list_areas_tool_formats_cadence(self):
        """pzk_list_areas renders each area's cadence metadata."""
        area = MagicMock()
        area.id = "area123"
        area.title = "Family"
        area.metadata = {"cadence": "monthly check-in"}
        self.mock_zettel_service.search_notes.return_value = [area]

        fn = self.registered_tools["pzk_list_areas"]
        result = fn(limit=10)

        assert "Areas (1)" in result
        assert "Family (ID: area123)" in result
        assert "Cadence: monthly check-in" in result
        self.mock_zettel_service.search_notes.assert_called_once_with(
            note_type=NoteType.AREA
        )

    def test_get_area_tool_lists_projects_and_open_task_counts(self):
        """pzk_get_area summarizes linked projects and their task counts."""
        area = MagicMock()
        area.id = "area123"
        area.note_type = NoteType.AREA
        area.metadata = {"cadence": "weekly review"}
        area.content = "# Area\n\nArea body"

        project_one = MagicMock()
        project_one.id = "project1"
        project_one.title = "Budgeting"

        project_two = MagicMock()
        project_two.id = "project2"
        project_two.title = "Planning"

        self.mock_zettel_service.get_note.return_value = area
        self.mock_zettel_service.search_notes.return_value = [project_one, project_two]
        self.mock_zettel_service.get_project_tasks.side_effect = [
            [MagicMock(), MagicMock()],
            [MagicMock()],
        ]

        fn = self.registered_tools["pzk_get_area"]
        result = fn(area_id="area123")

        assert "ID: area123" in result
        assert "Cadence: weekly review" in result
        assert "Projects: 2" in result
        assert "Budgeting (ID: project1)" in result
        assert "Planning (ID: project2)" in result
        assert "2 task(s)" in result
        assert "1 task(s)" in result
        self.mock_zettel_service.search_notes.assert_called_once_with(
            note_type=NoteType.PROJECT,
            area_id="area123",
        )

    def test_update_task_status_tool_removed(self):
        """pzk_update_task_status should no longer be registered; use pzk_update_task instead."""
        assert "pzk_update_task_status" not in self.registered_tools

    def test_error_handling(self):
        """Test error handling in the server."""
        # Test ValueError handling
        value_error = ValueError("Invalid input")
        result = self.server.format_error_response(value_error)
        assert "Error: Invalid input" in result

        # Test IOError handling
        io_error = IOError("File not found")
        result = self.server.format_error_response(io_error)
        assert "Error: File not found" in result

        # Test general exception handling
        general_error = Exception("Something went wrong")
        result = self.server.format_error_response(general_error)
        assert "Error: Something went wrong" in result
