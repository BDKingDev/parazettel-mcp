"""Regression tests for the graph DB migration script."""

import importlib.util
import shutil
from pathlib import Path
from uuid import uuid4

from parazettel_mcp.config import config
from parazettel_mcp.storage.note_repository import NoteRepository


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_graphdb.py"


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_to_graphdb_script", SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_note(
    file_path: Path,
    *,
    note_id: str,
    title: str,
    body: str,
    tags: str,
    links: str = "",
) -> None:
    file_path.write_text(
        (
            "---\n"
            f"id: {note_id}\n"
            f"title: {title}\n"
            "type: permanent\n"
            f"tags: {tags}\n"
            "created: 2026-01-01T00:00:00\n"
            "updated: 2026-01-01T00:00:00\n"
            "---\n"
            f"# {title}\n\n"
            f"{body}\n\n"
            "## Links\n"
            f"{links}"
        ),
        encoding="utf-8",
    )


def test_migrate_dry_run_does_not_create_graph_db():
    """Dry-run migration should parse notes without creating a graph DB file."""
    module = _load_migration_module()
    test_root = Path(".tmp") / "test-migrate-to-graphdb" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "graph.kuzu"
    notes_dir.mkdir(parents=True, exist_ok=True)

    try:
        _write_note(
            notes_dir / "note-1.md",
            note_id="note-1",
            title="Dry Run Note",
            body="Dry run body.",
            tags="migration, dry-run",
        )

        module.migrate(notes_dir, graph_db_path, dry_run=True)

        assert not graph_db_path.exists()
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_migrate_imports_notes_and_links():
    """Full migration should import notes, tags, and note links into Kuzu."""
    module = _load_migration_module()
    test_root = Path(".tmp") / "test-migrate-to-graphdb" / uuid4().hex
    notes_dir = test_root / "notes"
    graph_db_path = test_root / "db" / "graph.kuzu"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    repository = None

    try:
        _write_note(
            notes_dir / "note-1.md",
            note_id="note-1",
            title="First Migrated Note",
            body="First body.",
            tags="migration, shared",
            links="- reference [[note-2]] supports the second note\n",
        )
        _write_note(
            notes_dir / "note-2.md",
            note_id="note-2",
            title="Second Migrated Note",
            body="Second body.",
            tags="migration, shared",
        )

        module.migrate(notes_dir, graph_db_path, dry_run=False)

        config.notes_dir = notes_dir
        config.graph_db_path = graph_db_path
        repository = NoteRepository(notes_dir=notes_dir)

        all_notes = repository.get_all()
        assert len(all_notes) == 2

        linked = repository.find_linked_notes("note-1", "outgoing")
        assert [note.id for note in linked] == ["note-2"]

        tagged = repository.search(tags=["shared"])
        assert {note.id for note in tagged} == {"note-1", "note-2"}
    finally:
        if repository is not None:
            repository.close()
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path
        shutil.rmtree(test_root, ignore_errors=True)
