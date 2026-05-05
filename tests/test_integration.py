# tests/test_integration.py
"""Integration tests for the Zettelkasten MCP system."""
import datetime
import re
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import LinkType, NoteType
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer
from parazettel_mcp.services.search_service import SearchService
from parazettel_mcp.services.zettel_service import ZettelService


class TestIntegration:
    """Integration tests for the entire Zettelkasten MCP system."""

    @staticmethod
    def _extract_id(message: str) -> str:
        """Extract a created note/task ID from an MCP success message."""
        match = re.search(r"ID: ([^\)\s]+)", message)
        assert match, f"Could not extract an ID from: {message}"
        return match.group(1)

    @pytest.fixture(autouse=True)
    def setup_test_environment(self):
        """Set up test environment using temporary directories."""
        workspace_tmp_root = Path(__file__).resolve().parents[1] / ".tmp" / "test-integration"
        workspace_tmp_root.mkdir(parents=True, exist_ok=True)

        # Create explicit workspace-local directories so the graph DB runs on a
        # writable path throughout the test.
        self.test_root = workspace_tmp_root / uuid4().hex
        self.notes_dir = self.test_root / "notes"
        self.db_dir = self.test_root / "db"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.graph_db_path = self.db_dir / "test_graph.kuzu"

        # Save original config values
        self.original_notes_dir = config.notes_dir
        self.original_graph_db_path = config.graph_db_path

        # Update config for tests
        config.notes_dir = self.notes_dir
        config.graph_db_path = self.graph_db_path

        # Create services
        self.zettel_service = ZettelService()
        self.zettel_service.initialize()
        self.search_service = SearchService(self.zettel_service)

        # Create a real server while capturing the registered MCP tool functions
        self.registered_tools = {}

        def mock_tool_decorator(*args, **kwargs):
            def tool_wrapper(func):
                self.registered_tools[kwargs.get("name")] = func
                return func

            return tool_wrapper

        mock_mcp = type("MockMCP", (), {})()
        mock_mcp.tool = mock_tool_decorator

        with patch("parazettel_mcp.server.mcp_server.FastMCP", return_value=mock_mcp):
            self.server = ZettelkastenMcpServer()

        yield

        # Restore original config
        config.notes_dir = self.original_notes_dir
        config.graph_db_path = self.original_graph_db_path

        # Kuzu releases file handles on garbage collection; no explicit dispose needed.
        _ = getattr(self.zettel_service, "repository", None)
        _ = getattr(getattr(self.server, "zettel_service", None), "repository", None)

        # Clean up test directories
        shutil.rmtree(self.test_root, ignore_errors=True)

    def test_create_note_flow(self):
        """Test the complete flow of creating and retrieving a note."""
        # Use the zettel_service directly to create a note
        title = "Integration Test Note"
        content = "This is a test of the complete note creation flow."
        tags = ["integration", "test", "flow"]

        # Create the note
        note = self.zettel_service.create_note(
            title=title, content=content, note_type=NoteType.PERMANENT, tags=tags
        )
        assert note.id is not None

        # Retrieve the note
        retrieved_note = self.zettel_service.get_note(note.id)
        assert retrieved_note is not None
        assert retrieved_note.title == title

        # Note content includes the title as a markdown header - account for this
        expected_content = f"# {title}\n\n{content}"
        assert retrieved_note.content.strip() == expected_content.strip()

        # Check tags
        for tag in tags:
            assert tag in [t.name for t in retrieved_note.tags]

        # Verify the note exists on disk
        note_file = self.notes_dir / f"{note.id}.md"
        assert note_file.exists(), "Note file was not created on disk"

        # Verify file content
        with open(note_file, "r") as f:
            file_content = f.read()
            assert title in file_content
            assert content in file_content

    def test_knowledge_graph_flow(self):
        """Test creating a small knowledge graph with links and semantic relationships."""
        # Create several notes to form a knowledge graph
        hub_note = self.zettel_service.create_note(
            title="Knowledge Graph Hub",
            content="This is the central hub for our test knowledge graph.",
            note_type=NoteType.HUB,
            tags=["knowledge-graph", "hub", "integration-test"],
        )

        concept1 = self.zettel_service.create_note(
            title="Concept One",
            content="This is the first concept in our knowledge graph.",
            note_type=NoteType.PERMANENT,
            tags=["knowledge-graph", "concept", "integration-test"],
        )

        concept2 = self.zettel_service.create_note(
            title="Concept Two",
            content="This is the second concept, which extends the first.",
            note_type=NoteType.PERMANENT,
            tags=["knowledge-graph", "concept", "integration-test"],
        )

        critique = self.zettel_service.create_note(
            title="Critique of Concepts",
            content="This note critiques and questions the concepts.",
            note_type=NoteType.PERMANENT,
            tags=["knowledge-graph", "critique", "integration-test"],
        )

        # Create links with different semantic meanings
        # Use different link types to avoid uniqueness constraint issues
        self.zettel_service.create_link(
            source_id=hub_note.id,
            target_id=concept1.id,
            link_type=LinkType.REFERENCE,
            description="Main concept",
            bidirectional=True,
        )

        self.zettel_service.create_link(
            source_id=hub_note.id,
            target_id=concept2.id,
            link_type=LinkType.EXTENDS,
            description="Secondary concept",
            bidirectional=True,
        )

        self.zettel_service.create_link(
            source_id=hub_note.id,
            target_id=critique.id,
            link_type=LinkType.SUPPORTS,
            description="Critical perspective",
            bidirectional=True,
        )

        self.zettel_service.create_link(
            source_id=concept2.id,
            target_id=concept1.id,
            link_type=LinkType.REFINES,
            description="Builds upon first concept",
            bidirectional=True,
        )

        self.zettel_service.create_link(
            source_id=critique.id,
            target_id=concept1.id,
            link_type=LinkType.QUESTIONS,
            description="Questions assumptions",
            bidirectional=True,
        )

        self.zettel_service.create_link(
            source_id=critique.id,
            target_id=concept2.id,
            link_type=LinkType.CONTRADICTS,
            description="Contradicts conclusions",
            bidirectional=True,
        )

        # Get all notes linked to the hub
        hub_links = self.zettel_service.get_linked_notes(hub_note.id, "outgoing")
        assert len(hub_links) == 3
        hub_links_ids = {note.id for note in hub_links}
        assert concept1.id in hub_links_ids
        assert concept2.id in hub_links_ids
        assert critique.id in hub_links_ids

        # Get notes extended by concept2
        concept2_links = self.zettel_service.get_linked_notes(concept2.id, "outgoing")
        assert len(concept2_links) >= 1  # At least one link

        # Verify links by tag
        kg_notes = self.zettel_service.get_notes_by_tag("knowledge-graph")
        assert len(kg_notes) == 4  # Should find all 4 notes

    def test_para_hierarchy_today_view_flow_via_mcp_tools(self):
        """Exercise the area -> project -> task -> today view flow via MCP tools."""
        today = datetime.date.today()
        overdue = today - datetime.timedelta(days=1)

        create_area = self.registered_tools["pzk_create_area"]
        create_project = self.registered_tools["pzk_create_project"]
        create_task = self.registered_tools["pzk_create_task"]
        get_note = self.registered_tools["pzk_get_note"]
        get_todays_tasks = self.registered_tools["pzk_get_todays_tasks"]

        area_result = create_area(
            title="Personal Projects",
            content="Personal software and creative projects.",
            cadence="weekly review",
        )
        area_id = self._extract_id(area_result)

        project_result = create_project(
            title="Parazettel MCP",
            content="Build and ship the parazettel fork with PARA/GTD support.",
            source="transcript",
            area_id=area_id,
            outcome="Working MCP server with full GTD workflow support",
            deadline=(today + datetime.timedelta(days=30)).isoformat(),
        )
        project_id = self._extract_id(project_result)

        task_result = create_task(
            title="Write integration tests",
            content="Cover the full area -> project -> task -> today view flow.",
            project_id=project_id,
            status="active",
            due_date=today.isoformat(),
            priority=3,
            remind_at=today.isoformat(),
            estimated_minutes=90,
            context="computer",
            energy_level="high",
        )
        task_id = self._extract_id(task_result)

        overdue_result = create_task(
            title="Overdue task",
            content="Past due.",
            project_id=project_id,
            status="active",
            due_date=overdue.isoformat(),
            priority=4,
        )
        overdue_task_id = self._extract_id(overdue_result)

        task_details = get_note(identifier=task_id)
        assert f"Project ID: {project_id}" in task_details
        assert "@computer" in task_details
        assert "high-energy" in task_details

        todays_tasks = get_todays_tasks(include_overdue=True)
        assert "Today's tasks (2)" in todays_tasks
        assert (
            f"[P4] Overdue task — due {overdue.isoformat()} (ID: {overdue_task_id})"
            in todays_tasks
        )
        assert (
            f"[P3] Write integration tests — due {today.isoformat()} (ID: {task_id})"
            in todays_tasks
        )
        assert todays_tasks.index("Overdue task") < todays_tasks.index(
            "Write integration tests"
        )

    def test_rebuild_index_flow(self):
        """Test the rebuild index functionality with direct file modifications."""
        # Create a note through the service
        note1 = self.zettel_service.create_note(
            title="Original Note",
            content="This is the original content.",
            tags=["rebuild-test"],
        )

        # Manually modify the file to simulate external editing
        note_file = self.notes_dir / f"{note1.id}.md"
        assert note_file.exists(), "Note file was not created on disk"

        # Read the current file content
        with open(note_file, "r") as f:
            file_content = f.read()

        # Modify the file content directly, ensuring we replace the content part only
        # The content in the file will include the title header, so we need to search
        # for the entire content structure
        modified_content = file_content.replace(
            "This is the original content.",
            "This content was manually edited outside the system.",
        )

        # Write the modified content back
        with open(note_file, "w") as f:
            f.write(modified_content)

        # At this point, the file has been modified but the database hasn't been updated

        # Verify the database still has old content by reading through the repository
        modified_file_content = self.zettel_service.get_note(note1.id).content
        assert "manually edited" in modified_file_content

        # Rebuild the index
        self.zettel_service.rebuild_index()

        # Verify the note now has the updated content
        note1_after = self.zettel_service.get_note(note1.id)
        assert (
            "This content was manually edited outside the system."
            in note1_after.content
        )
