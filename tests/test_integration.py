# tests/test_integration.py
"""Integration tests for the Zettelkasten MCP system."""
import datetime
import os
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

        with patch("parazettel_mcp.server.mcp_server.MCPServer", return_value=mock_mcp):
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

        # A file that can't be read is reported without its path, which an OS
        # read error would otherwise carry (here a directory in its place).
        note_file = self.notes_dir / f"{note.id}.md"
        note_file.unlink()
        note_file.mkdir()
        out = signals(ids=[note.id])
        assert "(its file doesn't parse)" in out
        assert "- Last verified: unavailable (its file couldn't be read)" in out
        assert note_file.name not in out.replace(f"Note {note.id} ", "")
        assert str(self.notes_dir) not in out

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

        def verify(**details):
            """Mark verified; return the stored date, which is the day of the
            call on either side of a midnight crossing."""
            before = datetime.date.today().isoformat()
            update(note_id=note.id, mark_verified=True, **details)
            after = datetime.date.today().isoformat()
            stored = svc.get_note(note.id).metadata["verification"]["date"]
            assert stored in (before, after)
            return stored

        today = verify(
            verified_basis="RFC 9110 section 9",
            verified_scope="the caching claim",
            verified_by="km-archivist",
        )
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
        again = verify(verified_basis="re-read source")
        assert svc.get_note(note.id).metadata["verification"] == {
            "date": again,
            "basis": "re-read source",
        }
        assert "scope: not recorded; verifier: not recorded" in signals(ids=[note.id])

        update(note_id=note.id, mark_verified=False)
        cleared = svc.get_note(note.id)
        assert cleared.last_verified is None
        assert "verification" not in cleared.metadata
        assert "verification" not in (self.notes_dir / f"{note.id}.md").read_text()

    def test_delete_tool_removes_a_note_whose_file_no_longer_parses(self):
        """pzk_delete_note deletes a note whose file can't be parsed, whether or
        not a cached copy is still around; a missing id is still not found."""
        note = self.zettel_service.create_note(
            title="Corrupt Me", content="x", note_type=NoteType.PERMANENT
        )
        note_file = self.notes_dir / f"{note.id}.md"
        before = note_file.stat()
        note_file.write_text("---\nid: [unclosed\ntitle: : :\n---\nGarbled.\n")
        # Move the mtime so no cached copy of the readable note can match.
        os.utime(note_file, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        delete = self.registered_tools["pzk_delete_note"]
        result = delete(note_id=note.id)

        assert result.startswith(f"Note deleted successfully: {note.id}"), result
        assert not note_file.exists()
        assert "deleted successfully" not in delete(note_id=note.id)

    def test_near_duplicate_create_without_fastembed(self, monkeypatch, caplog):
        """With embeddings on but no fastembed (the sentence-transformers-only
        extra), a create whose duplicate check finds a candidate reports it
        instead of failing on the missing cross-encoder, and says why once."""
        import builtins
        import importlib.util

        from parazettel_mcp.config import DEFAULT_DEDUP_RERANK_MODEL
        from parazettel_mcp.services import reranker

        real_find_spec, real_import = importlib.util.find_spec, builtins.__import__

        def no_fastembed(name, *args, **kwargs):
            if name == "fastembed" or name.startswith("fastembed."):
                raise ImportError("fastembed not installed")
            return real_import(name, *args, **kwargs)

        def find_spec(name, *args, **kwargs):
            if name == "fastembed":
                return None
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", find_spec)
        monkeypatch.setattr(builtins, "__import__", no_fastembed)
        monkeypatch.setattr(reranker, "_warned_fastembed_missing", False)
        monkeypatch.setattr(config, "embedding_enabled", True)
        monkeypatch.setattr(config, "embedding_provider", "hash")
        monkeypatch.setattr(config, "dedup_rerank_model", DEFAULT_DEDUP_RERANK_MODEL)

        tools = {}
        mock_mcp = type("MockMCP", (), {})()
        mock_mcp.tool = lambda *a, **k: (lambda f: tools.setdefault(k.get("name"), f))
        with caplog.at_level("WARNING"):
            with patch(
                "parazettel_mcp.server.mcp_server.MCPServer", return_value=mock_mcp
            ):
                server = ZettelkastenMcpServer()
            try:
                svc = server.zettel_service
                area = svc.create_area_note(title="Baking", content="a")
                create = tools["pzk_create_note"]
                # Unrelated notes give the shared words below enough BM25 weight
                # for the duplicate check to find a candidate.
                for topic in ("Tax deadlines", "Bike repair", "Jazz chords", "Tides"):
                    svc.create_note(
                        title=topic, content=f"{topic} notes.", area_id=area.id
                    )
                body = "Feed the sourdough starter the night before baking bread."
                first = create(
                    title="Sourdough needs a lively starter",
                    content=body, area_id=area.id, source="manual",
                )
                second = create(
                    title="A lively sourdough starter matters",
                    content=body, area_id=area.id, source="manual",
                )
            finally:
                server.close()

        assert first.startswith("Note created successfully"), first
        assert second.startswith('Not created: "A lively sourdough'), second
        assert "Sourdough needs a lively starter" in second
        warned = [r for r in caplog.records if "needs fastembed" in r.getMessage()]
        assert len(warned) == 1

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

    def test_area_and_project_reads_hide_generated_links(self):
        """pzk_get_area / pzk_get_project show the curated body, not one ## Links
        line per member, and say how many links were hidden."""
        svc = self.server.zettel_service
        area = svc.create_area_note(title="Busy area", content="Placeholder.")
        lane = svc.create_note(title="Lane entry note", content="x", area_id=area.id)
        for i in range(40):
            svc.create_note(title=f"Member {i:02d}", content="x", area_id=area.id)
        project = svc.create_project_note(
            title="Busy project", content="Project goal.", area_id=area.id
        )
        for i in range(25):
            svc.create_note(
                title=f"Project note {i:02d}", content="x", project_id=project.id
            )
        lane_map = f"Main lanes:\n- **Entry**\n  - [[{lane.id}|Lane entry note]]"
        svc.update_note(area.id, content=f"# Busy area\n\n{lane_map}")
        area = svc.get_note(area.id)
        project = svc.get_note(project.id)
        assert len(area.links) >= 42 and len(project.links) >= 25

        area_out = self.registered_tools["pzk_get_area"](area_id=area.id)
        assert lane_map in area_out
        assert "## Links" not in area_out
        assert "Member 07" not in area_out
        assert f"[{len(area.links)} link(s) hidden" in area_out
        assert "Busy project (ID:" in area_out  # the ## Projects list still follows

        project_out = self.registered_tools["pzk_get_project"](project_id=project.id)
        assert "Project goal." in project_out
        assert "## Links" not in project_out
        assert f"[{len(project.links)} link(s) hidden" in project_out
        assert "Project note 07 (ID:" in project_out  # from Notes:, not ## Links

        full = self.registered_tools["pzk_get_note"](
            identifier=area.id, include_links=True
        )
        assert "Member 07" in full and len(area_out) < len(full) / 3

    def test_container_reads_announce_a_prose_only_links_section(self):
        """A hand-edited ## Links section with no list lines is still hidden, and
        the response says so; sections after it are kept verbatim."""
        svc = self.server.zettel_service
        area = svc.create_area_note(title="Prose area", content="Placeholder.")
        project = svc.create_project_note(
            title="Prose project", content="x", area_id=area.id
        )
        prose = "I link these by hand when a lane gets crowded."
        cadence = "## Review cadence\n\nMonthly, first Sunday.\n"
        for note in (area, project):
            path = svc.repository.notes_dir / f"{note.id}.md"
            head, _, _ = path.read_text(encoding="utf-8").partition("## Links")
            path.write_text(f"{head}## Links\n\n{prose}\n\n{cadence}", encoding="utf-8")

        area_out = self.registered_tools["pzk_get_area"](area_id=area.id)
        project_out = self.registered_tools["pzk_get_project"](project_id=project.id)
        for out, note in ((area_out, area), (project_out, project)):
            assert prose not in out
            assert (
                f"[Hidden: Links section — see pzk_get_linked_notes('{note.id}')" in out
            )
            assert cadence in out

        # The full section is still one call away, and pzk_get_note's default read
        # announces the hidden section too.
        full = self.registered_tools["pzk_get_note"](
            identifier=area.id, include_links=True
        )
        assert prose in full and cadence in full
        default = self.registered_tools["pzk_get_note"](identifier=area.id)
        assert prose not in default and "[Links section hidden" in default

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

    def _neighborhood_graph(self):
        """An area with 30 unrelated siblings and a described knowledge thread.

        C -refines-> K1 -extends-> K3; K2 -supports-> C; K4 -contradicts-> K2;
        K1 -supports-> C as well (two edges on one pair); map M -supports-> C
        with no description; C mentions K5 inline in its prose.
        """
        svc = self.server.zettel_service
        area = svc.create_area_note(title="Hub area", content="Area hub.")

        def note(title, content="x", **kwargs):
            return svc.create_note(
                title=title, content=content, area_id=area.id, **kwargs
            )

        for i in range(30):
            note(f"Sibling {i:02d}")
        k5 = note("K5 mentioned inline")
        c = note("Center claim", content=f"Builds on [[{k5.id}]].")
        k1, k2, k3, k4 = (note(f"K{i}") for i in range(1, 5))
        m = note("Map M", note_type=NoteType.STRUCTURE)
        svc.create_link(c.id, k1.id, LinkType.REFINES, "K1 adds the keep-or-skip test")
        svc.create_link(k1.id, c.id, LinkType.SUPPORTS)
        svc.create_link(k2.id, c.id, LinkType.SUPPORTS, "K2 is the worked evidence")
        svc.create_link(k1.id, k3.id, LinkType.EXTENDS, "K3 applies K1 to maps")
        svc.create_link(k4.id, k2.id, LinkType.CONTRADICTS, "K4 disputes K2's sample")
        svc.create_link(m.id, c.id, LinkType.SUPPORTS)
        return dict(area=area, c=c, k1=k1, k2=k2, k3=k3, k4=k4, k5=k5, m=m)

    @staticmethod
    def _entry(out, note):
        """The output line for *note*, or None."""
        return next((ln for ln in out.splitlines() if f"(ID: {note.id}," in ln), None)

    def test_neighborhood_follows_knowledge_links_not_area_routing(self):
        g = self._neighborhood_graph()
        out = self.registered_tools["pzk_get_neighborhood"](note_id=g["c"].id)
        hops, routing = out.split("Routing (not expanded):")

        assert "Sibling" not in out
        for key in ("k1", "k2", "k5", "m"):
            assert f"[via {g['c'].id}:" in self._entry(hops, g[key])
        assert f"[via {g['k1'].id}: -> extends: \"K3 applies K1 to maps\"]" in hops
        assert (
            f"[via {g['k2'].id}: <- contradicts: \"K4 disputes K2's sample\"]" in hops
        )
        # Both edges on the C-K1 pair, each with only its own stored description.
        assert '-> refines: "K1 adds the keep-or-skip test"; <- supports]' in hops
        assert self._entry(hops, g["m"]).endswith(f"[via {g['c'].id}: <- supports]")
        assert self._entry(hops, g["k5"]).endswith("-> inline]")
        # The area is listed once, as routing, and not expanded.
        assert out.count(f"(ID: {g['area'].id},") == 1
        assert self._entry(routing, g["area"]).endswith(
            "[-> reference (routing); -> part_of (routing); <- has_part (routing)]"
        )
        assert "include_routing=true" in out

    def test_neighborhood_order_does_not_depend_on_neighbor_order(self):
        g = self._neighborhood_graph()
        tool = self.registered_tools["pzk_get_neighborhood"]
        first = tool(note_id=g["c"].id)
        real = self.server.zettel_service.get_linked_notes
        with patch.object(
            self.server.zettel_service,
            "get_linked_notes",
            side_effect=lambda nid, d: list(reversed(real(nid, d))),
        ):
            assert tool(note_id=g["c"].id) == first

    def test_neighborhood_cap_and_include_routing(self):
        g = self._neighborhood_graph()
        tool = self.registered_tools["pzk_get_neighborhood"]
        # max_nodes counts every note shown: center, linked notes, routing entries.
        capped = tool(note_id=g["c"].id, max_nodes=3)
        assert "Capped at 3 notes" in capped
        assert len([ln for ln in capped.splitlines() if "[via " in ln]) == 2
        assert self._entry(capped, g["area"]) is None
        assert "- … 1 more; list them with" in capped  # the area, not dropped silently
        alone = tool(note_id=g["c"].id, max_nodes=1)
        assert "— 0 connected, 1 routing note(s)" in alone
        assert "Capped at 1 notes" in alone
        assert not [
            ln for ln in alone.splitlines() if ln.startswith("- ") and "(ID: " in ln
        ]
        roomy = tool(note_id=g["c"].id, max_nodes=8)
        assert "Routing (not expanded)" in roomy and self._entry(roomy, g["area"])

        old = tool(note_id=g["c"].id, include_routing=True, max_nodes=60)
        assert "Routing (not expanded)" not in old
        assert f"[via {g['area'].id}: -> has_part (routing);" in old  # siblings

    def test_neighborhood_cap_notice_only_when_a_note_was_left_out(self):
        """No area, so no routing entries share the budget."""
        svc = self.server.zettel_service
        tool = self.registered_tools["pzk_get_neighborhood"]
        c, x, y, z = (svc.create_note(title=t, content="x") for t in "CXYZ")
        svc.create_link(c.id, x.id, LinkType.SUPPORTS)
        svc.create_link(c.id, y.id, LinkType.EXTENDS)

        # A note with links never reads "No links yet"; the cap says why.
        alone = tool(note_id=c.id, max_nodes=1)
        assert "No links yet" not in alone
        assert "Capped at 1 notes" in alone

        # The budget fills at hop 1, but hop 2 has nothing more to show.
        full = tool(note_id=c.id, depth=2, max_nodes=3)
        assert len([ln for ln in full.splitlines() if "[via " in ln]) == 2
        assert "Capped" not in full

        svc.create_link(y.id, z.id, LinkType.REFINES)
        over = tool(note_id=c.id, depth=2, max_nodes=3)
        assert self._entry(over, z) is None
        assert "Capped at 3 notes" in over

    def test_neighborhood_member_seed_does_not_expand_a_lane_mapped_area(self):
        """A lane map naming its members doesn't reopen the sibling flood from a
        member seed: the area is listed once under Routing and not expanded."""
        g = self._neighborhood_graph()
        svc = self.server.zettel_service
        siblings = [
            n for n in svc.get_all_notes() if n.title.startswith("Sibling")
        ]
        lanes = "\n".join(f"- [[{n.id}]]" for n in [g["c"], *siblings])
        svc.update_note(g["area"].id, content=f"Lanes:\n{lanes}")
        tool = self.registered_tools["pzk_get_neighborhood"]

        out = tool(note_id=g["c"].id, depth=2)
        assert "Sibling" not in out
        assert out.count(f"(ID: {g['area'].id},") == 1
        routing = out.split("Routing (not expanded):")[1]
        area_entry = self._entry(routing, g["area"])
        assert "<- inline" in area_entry and "[via " not in area_entry
        assert f"[via {g['c'].id}: -> refines:" in self._entry(out, g["k1"])

        # include_routing still opens the area and its members.
        wide = tool(note_id=g["c"].id, include_routing=True, max_nodes=60)
        assert f"[via {g['area'].id}:" in wide and "Sibling" in wide

    def test_neighborhood_ignores_refs_inside_a_tilde_fence(self):
        """A [[member]] inside an area's ~~~ example is not an inline ref, so the
        member stays a plain routing entry with no inline edge."""
        svc = self.server.zettel_service
        area = svc.create_area_note(title="Fenced area", content="a")
        member = svc.create_note(title="Filed member", content="m", area_id=area.id)
        svc.update_note(
            area.id, content=f"Lane map example:\n\n~~~\n- [[{member.id}]]\n~~~\n"
        )
        tool = self.registered_tools["pzk_get_neighborhood"]

        for _ in range(2):
            out = tool(note_id=area.id, max_nodes=60)
            routing = out.split("Routing (not expanded):")[1]
            entry = self._entry(routing, member)
            assert entry is not None and "[via " not in entry
            assert "inline" not in entry
            self.zettel_service.rebuild_index()

    def test_neighborhood_container_seed_lists_members_and_keeps_knowledge_edges(self):
        g = self._neighborhood_graph()
        svc = self.server.zettel_service
        # The lane map links its own member C inline. Naming a member in the
        # container's prose is filing, like the member's reference to its area,
        # so it is routing; the inline edge stays visible in the label.
        svc.update_note(g["area"].id, content=f"Lanes:\n- [[{g['c'].id}]]")
        tool = self.registered_tools["pzk_get_neighborhood"]

        out = tool(note_id=g["area"].id, depth=2, max_nodes=14)
        assert "Hop 1:" not in out  # the area has no knowledge links of its own
        routing = out.split("Routing (not expanded):")[1]
        everyone = tool(note_id=g["area"].id, depth=2, max_nodes=60)
        c_entry = self._entry(everyone.split("Routing (not expanded):")[1], g["c"])
        assert "-> inline" in c_entry and "[via " not in c_entry
        assert f"[via {g['c'].id}:" not in everyone  # C is not expanded
        # The members fill the budget, never left empty.
        assert "Sibling 00" in routing and "more; list them with" in routing
        shown = [ln for ln in out.splitlines() if ln.startswith("- ") and "(ID: " in ln]
        assert len(shown) == 14 - 1  # every listed note counts toward max_nodes
        assert "— 0 connected, " in out and " routing note(s)" in out

        # A written link to the area makes the pair traversable from the note,
        # and the area then expands through its knowledge edges only.
        svc.create_link(g["k3"].id, g["area"].id, LinkType.SUPPORTS)
        from_k3 = tool(note_id=g["k3"].id, depth=2)
        area_entry = self._entry(from_k3, g["area"])
        assert "[via " in area_entry and "-> supports" in area_entry
        entries = [ln for ln in from_k3.splitlines() if ln.startswith("- ")]
        assert entries and not any("Sibling" in ln for ln in entries)

        # The mirror: a member naming its own area in prose is filing too, so
        # it isn't reached through the area from K3 or "connected" to the area.
        s0 = next(n for n in svc.get_all_notes() if n.title == "Sibling 00")
        svc.update_note(s0.id, content=f"Filed under [[{g['area'].id}]].")
        assert self._entry(tool(note_id=g["k3"].id, depth=2), s0) is None
        from_area = tool(note_id=g["area"].id, depth=2, max_nodes=60)
        s0_entry = self._entry(from_area.split("Routing (not expanded):")[1], s0)
        assert "<- inline (routing)" in s0_entry and "[via " not in s0_entry
