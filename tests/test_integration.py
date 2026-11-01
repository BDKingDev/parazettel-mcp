# tests/test_integration.py
"""Integration tests for the Zettelkasten MCP system."""
import datetime
import re
import shutil
import sqlite3
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

        if getattr(self, "server", None) is not None:
            self.server.close()
        if getattr(self, "zettel_service", None) is not None:
            self.zettel_service.close()

        # Restore original config
        config.notes_dir = self.original_notes_dir
        config.graph_db_path = self.original_graph_db_path

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

    def _hits(self, note):
        signals = self.zettel_service.repository.get_retrieval_signals([note.id])
        return signals.get(note.id, {}).get("hit_count", 0)

    def test_note_signals_read_counters_without_counting_or_writing(self):
        area = self.zettel_service.create_area_note(title="Area", content="a")
        note = self.zettel_service.create_note(
            title="Read me", content="x", area_id=area.id
        )
        self.registered_tools["pzk_get_note"](identifier=note.id)
        self.registered_tools["pzk_get_note"](identifier=note.id)
        note_file = self.notes_dir / f"{note.id}.md"
        before = (note_file.read_bytes(), note_file.stat().st_mtime_ns)

        signals = self.registered_tools["pzk_note_signals"]
        first = signals(ids=[note.id])
        assert signals(ids=[note.id]) == first
        assert "- Recorded retrievals: 2, last " in first
        assert self._hits(note) == 2
        assert (note_file.read_bytes(), note_file.stat().st_mtime_ns) == before
        assert "reads in Obsidian are not counted" in first

        # Counters survive a rebuild for a note that still exists.
        self.zettel_service.rebuild_index()
        assert "- Recorded retrievals: 2, last " in signals(ids=[note.id])

    def test_note_signals_tell_absent_from_unavailable(self):
        area = self.zettel_service.create_area_note(title="Area", content="a")
        note = self.zettel_service.create_note(
            title="Never read", content="x", area_id=area.id
        )
        signals = self.registered_tools["pzk_note_signals"]
        out = signals(ids=[note.id, "no-such-id"])
        assert "- Recorded retrievals: none recorded" in out
        assert "- Last verified: not recorded" in out
        assert "- Citing notes: none now" in out
        assert "Note not found: no-such-id" in out

        repo = self.server.zettel_service.repository  # the tools' service
        broken = sqlite3.OperationalError("database is locked")
        with patch.object(repo, "get_retrieval_signals", side_effect=broken), \
                patch.object(repo, "get_citation_signals", side_effect=broken):
            out = signals(ids=[note.id])
        assert "- Recorded retrievals: unavailable (database is locked)" in out
        assert "- Citing notes: unavailable (database is locked)" in out

    def test_listing_and_search_record_no_retrieval(self):
        area = self.zettel_service.create_area_note(title="Area", content="a")
        note = self.zettel_service.create_note(
            title="Batch target", content="findable words", area_id=area.id
        )
        tools = self.registered_tools
        tools["pzk_get_notes"](identifiers=[note.id], detail="summary")
        tools["pzk_get_notes"](identifiers=[note.id], detail="ids")
        tools["pzk_search_notes"](query="findable")
        assert self._hits(note) == 0
        tools["pzk_get_notes"](identifiers=[note.id], detail="full")
        assert self._hits(note) == 1

    def test_note_signals_count_current_citing_notes_without_routing(self):
        svc = self.zettel_service
        area = svc.create_area_note(title="Area", content="a")
        target = svc.create_note(title="Target", content="t", area_id=area.id)
        twice = svc.create_note(title="Twice", content="w", area_id=area.id)
        svc.create_link(twice.id, target.id, LinkType.SUPPORTS)
        svc.create_link(twice.id, target.id, LinkType.EXTENDS)
        mention = svc.create_note(
            title="Mention", content=f"See [[{target.id}]].", area_id=area.id
        )
        gone = svc.create_note(title="Gone", content="g", area_id=area.id)
        svc.create_link(gone.id, target.id, LinkType.REFINES)
        svc.remove_link(gone.id, target.id, LinkType.REFINES)
        signals = self.registered_tools["pzk_note_signals"]

        out = signals(ids=[target.id])
        # Two links from one note count once; the removed link doesn't count.
        assert "- Citing notes: 2 now; latest explicit link " in out
        assert f"from {twice.id} (" in out
        assert (
            f"latest inline mention about " in out
            and f"from {mention.id} (approximate: that note's last update)" in out
        )
        # Members' reference/part_of links to their area are routing, not citations.
        assert "- Citing notes: none now" in signals(ids=[area.id])

    def test_verification_record_round_trips_and_stays_opt_in(self):
        svc = self.zettel_service
        area = svc.create_area_note(title="Area", content="a")
        note = svc.create_note(title="Claim", content="x", area_id=area.id)
        update = self.registered_tools["pzk_update_note"]
        signals = self.registered_tools["pzk_note_signals"]
        assert "- Last verified: not recorded" in signals(ids=[note.id])
        assert "mark_verified=true" in update(note_id=note.id, verified_basis="RFC")

        update(
            note_id=note.id,
            mark_verified=True,
            verified_basis="RFC 9110 section 9",
            verified_scope="the caching claim",
            verified_by="km-archivist",
        )
        today = datetime.date.today().isoformat()
        record = {
            "date": today,
            "basis": "RFC 9110 section 9",
            "scope": "the caching claim",
            "verified_by": "km-archivist",
        }
        assert svc.get_note(note.id).metadata["verification"] == record
        # An unrelated update and a rebuild leave the record exactly as written.
        update(note_id=note.id, tags="kept")
        svc.rebuild_index()
        assert svc.get_note(note.id).metadata["verification"] == record
        out = signals(ids=[note.id])
        assert (
            f"- Last verified: {today}; basis: RFC 9110 section 9; scope: the "
            "caching claim; verifier (self-reported): km-archivist"
        ) in out

        # A later partial record replaces the whole one.
        update(note_id=note.id, mark_verified=True, verified_basis="re-read source")
        assert svc.get_note(note.id).metadata["verification"] == {
            "date": today,
            "basis": "re-read source",
        }
        assert "scope: not recorded; verifier: not recorded" in signals(ids=[note.id])

        update(note_id=note.id, mark_verified=False)
        cleared = svc.get_note(note.id)
        assert cleared.last_verified is None
        assert "verification" not in cleared.metadata
        assert "verification" not in (self.notes_dir / f"{note.id}.md").read_text()

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

    def _write_note_file_with_tags(self, note_id: str, tags: list[str]) -> None:
        """Write a note markdown file directly with the given frontmatter tags."""
        tag_block = "tags:\n" + "".join(f"- {tag}\n" for tag in tags)
        (self.notes_dir / f"{note_id}.md").write_text(
            "---\n"
            f"id: {note_id}\n"
            "title: Tag Edit Note\n"
            "type: permanent\n"
            f"{tag_block}"
            "created: '2026-01-01T00:00:00'\n"
            "updated: '2026-01-01T00:00:00'\n"
            "---\n\n# Tag Edit Note\n\nbody\n",
            encoding="utf-8",
        )

    def test_rebuild_reconciles_tag_edits_both_ways(self):
        """Rebuild must reflect on-disk tag adds *and* removals, with no resurrection."""
        note_id = "20260101T000000000000000"

        # Initial state: three tags.
        self._write_note_file_with_tags(note_id, ["alpha", "beta", "gamma"])
        self.zettel_service.rebuild_index()
        assert {t.name for t in self.zettel_service.get_all_tags()} == {
            "alpha",
            "beta",
            "gamma",
        }

        # Remove a tag on disk -> rebuild -> removal is reflected, no resurrection.
        self._write_note_file_with_tags(note_id, ["alpha", "gamma"])
        self.zettel_service.rebuild_index()
        tags_after_remove = {t.name for t in self.zettel_service.get_all_tags()}
        assert tags_after_remove == {"alpha", "gamma"}
        assert "beta" not in tags_after_remove

        # Re-add the removed tag plus a new one -> rebuild -> both present.
        self._write_note_file_with_tags(note_id, ["alpha", "beta", "gamma", "delta"])
        self.zettel_service.rebuild_index()
        assert {t.name for t in self.zettel_service.get_all_tags()} == {
            "alpha",
            "beta",
            "gamma",
            "delta",
        }

    def test_rebuild_drops_orphaned_tag_nodes(self):
        """A tag removed from its last note must not survive a rebuild."""
        keep = self.zettel_service.create_note(
            title="Keep", content="keep", tags=["shared", "kept-only"]
        )
        drop = self.zettel_service.create_note(
            title="Drop", content="drop", tags=["shared", "doomed"]
        )

        # Remove every use of "doomed" by retagging the only note that had it.
        self.zettel_service.update_note(drop.id, tags=["shared"])

        # Before a rebuild the orphan Tag node may linger in the graph, but
        # get_all_tags is edge-derived so it must already exclude it.
        assert "doomed" not in {t.name for t in self.zettel_service.get_all_tags()}

        # After a rebuild the orphan node itself is gone and the surviving tags
        # are exactly those still applied to a note.
        self.zettel_service.rebuild_index()
        assert {t.name for t in self.zettel_service.get_all_tags()} == {
            "shared",
            "kept-only",
        }
        assert keep.id and drop.id  # both notes still present
        assert self.zettel_service.get_note(keep.id) is not None
        assert self.zettel_service.get_note(drop.id) is not None

    def test_get_all_tags_excludes_unused_tags(self):
        """get_all_tags reflects tags in use, not bare Tag-node existence."""
        note = self.zettel_service.create_note(
            title="Tagged", content="c", tags=["live-tag"]
        )
        assert "live-tag" in {t.name for t in self.zettel_service.get_all_tags()}

        # Drop the only tag; the tool output must drop with it.
        self.zettel_service.update_note(note.id, tags=[])
        assert "live-tag" not in {t.name for t in self.zettel_service.get_all_tags()}
