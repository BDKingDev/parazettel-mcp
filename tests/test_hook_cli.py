"""Tests for the daemon-free session-hook helpers (parazettel_mcp.hook_cli)."""

import datetime

import pytest

from parazettel_mcp import hook_cli
from parazettel_mcp.config import config
from parazettel_mcp.models.schema import Note, NoteType
from parazettel_mcp.storage.note_repository import NoteRepository

PROMPT = "How do I verify the strain dropdown filtering by species on production?"


@pytest.fixture
def vault(zettel_service, monkeypatch):
    """A small vault: one area, one project with a due task, a matching note, and
    unrelated filler (BM25 needs a corpus where the query terms are rare)."""
    monkeypatch.setattr(config, "embedding_enabled", config.embedding_enabled)
    area = zettel_service.create_area_note(title="Odoo Platform", content="ERP work")
    project = zettel_service.create_project_note(
        title="Odoo-BioTrack Integration", content="BioTrack sync", area_id=area.id
    )
    zettel_service.create_task(
        title="Prod-verify strain species filtering",
        content="Check the strain dropdown filters by species.",
        project_id=project.id,
        due_date=datetime.date.today() - datetime.timedelta(days=1),
    )
    zettel_service.repository.create(
        Note(
            title="Strain dropdown is filtered by the selected species",
            content="The strain dropdown filtering by species is applied in the form view.",
            note_type=NoteType.PERMANENT,
        )
    )
    for topic in ("grocery lists", "weekly meal plans", "bookkeeping ledgers",
                  "garden watering", "bicycle repair", "tax filing", "music practice",
                  "travel packing"):
        zettel_service.repository.create(
            Note(title=topic.title(), content=f"Notes on {topic}.", note_type=NoteType.PERMANENT)
        )
    return project


def test_briefing_lists_projects_and_overdue_tasks(vault):
    text = hook_cli.briefing()
    assert "Active projects (1):" in text
    assert "Odoo-BioTrack Integration" in text
    assert "Tasks due/overdue (1):" in text
    assert "Prod-verify strain species filtering" in text


def test_lexical_recall_surfaces_the_matching_note(vault, monkeypatch):
    monkeypatch.setenv("PARAZETTEL_RECALL_MIN_BM25", "0.5")
    text = hook_cli.recall(PROMPT, "lexical")
    assert text is not None
    assert text.splitlines()[0].endswith(")")
    assert "Strain dropdown is filtered by the selected species" in text


def test_lexical_recall_stays_silent_below_the_floor(vault, monkeypatch):
    monkeypatch.setenv("PARAZETTEL_RECALL_MIN_BM25", "1000")
    assert hook_cli.recall(PROMPT, "lexical") is None


def test_semantic_recall_uses_the_embedding_model(vault, monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_provider", "hash")
    monkeypatch.setattr(config, "embedding_dim", 16)
    embedder = NoteRepository()
    embedder.rebuild_index()  # the vault was created with embeddings off
    embedder.close()
    monkeypatch.setattr(hook_cli, "RECALL_THRESHOLD", -1.0)  # hash vectors aren't semantic
    text = hook_cli.recall(PROMPT, "semantic")
    assert text is not None and text.startswith("  - [")


def test_trivial_prompts_are_ignored(vault):
    assert hook_cli.recall("yes", "lexical") is None


def test_hook_reads_never_rebuild_or_take_the_write_lock(test_config, monkeypatch):
    """Hooks run on a deadline: opening with reconcile=False must not reconcile."""
    NoteRepository(notes_dir=test_config.notes_dir).close()  # create the schema
    (test_config.notes_dir / "hand-dropped.md").write_text(
        "---\nid: hand-dropped\ntitle: Hand dropped\ntype: permanent\n---\n# Hand dropped\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        NoteRepository,
        "rebuild_index_if_needed",
        lambda self: pytest.fail("a hook read must not reconcile the index"),
    )
    repo = NoteRepository(notes_dir=test_config.notes_dir, reconcile=False)
    repo.close()


def test_cli_prints_nothing_when_nothing_qualifies(vault, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("ok"))
    assert hook_cli.main(["recall", "--mode", "lexical"]) == 0
    assert capsys.readouterr().out == ""
