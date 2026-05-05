#!/usr/bin/env python3
"""Migrate existing parazettel-mcp notes to the Kuzu graph database.

This script scans a notes directory for markdown files that follow the
parazettel frontmatter schema and imports them into a new Kuzu graph database.
It is safe to run multiple times – existing nodes are updated in place and
relationships are rebuilt without duplicates.

Usage
-----
    python scripts/migrate_to_graphdb.py [--notes-dir PATH] [--graph-db-path PATH]

If not supplied, the script falls back to the environment variables
``PARAZETTEL_NOTES_DIR`` and ``PARAZETTEL_GRAPH_DB_PATH``, and then to the
package defaults (``data/notes`` and ``data/db/graph.kuzu``).

Prerequisites
-------------
* Install the package (``pip install -e .``)
* If migrating from an existing SQLite database, ``sqlalchemy`` must also be
  installed (``pip install "parazettel-mcp[migration]"``).

What the script does
--------------------
1. Walks every ``*.md`` file in the notes directory.
2. Parses frontmatter + content with the repository's own parser so that the
   exact same logic used at runtime is exercised during migration.
3. Uses a two-pass strategy:
   * Pass 1 – upsert all Note nodes and Tag nodes.
   * Pass 2 – upsert all LINKS_TO / HAS_TAG relationships.
   This guarantees that link targets always exist when the edges are created.
4. Prints a summary of imported and skipped notes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_to_graphdb")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate parazettel-mcp notes to a Kuzu graph database."
    )
    parser.add_argument(
        "--notes-dir",
        type=str,
        default=os.environ.get("PARAZETTEL_NOTES_DIR", "data/notes"),
        help="Path to the notes directory (default: data/notes or $PARAZETTEL_NOTES_DIR)",
    )
    parser.add_argument(
        "--graph-db-path",
        type=str,
        default=os.environ.get("PARAZETTEL_GRAPH_DB_PATH", "data/db/graph.kuzu"),
        help="Path for the Kuzu graph database file (default: data/db/graph.kuzu)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse notes and report counts without writing to the graph database.",
    )
    return parser.parse_args()


def migrate(notes_dir: Path, graph_db_path: Path, dry_run: bool = False) -> None:
    """Run the migration."""
    if not notes_dir.exists():
        logger.error("Notes directory does not exist: %s", notes_dir)
        sys.exit(1)

    note_files = sorted(notes_dir.glob("*.md"))
    if not note_files:
        logger.warning("No markdown files found in %s", notes_dir)
        return

    logger.info("Found %d markdown file(s) in %s", len(note_files), notes_dir)

    # Import here so that the script fails fast if the package is not installed.
    try:
        from parazettel_mcp.models.graph_db import init_graph_db
        from parazettel_mcp.storage.note_repository import NoteRepository
    except ImportError as exc:
        logger.error(
            "Could not import parazettel_mcp. "
            "Run `pip install -e .` from the repository root: %s",
            exc,
        )
        sys.exit(1)

    if dry_run:
        logger.info("Dry-run mode: parsing notes without writing to the graph database.")
    else:
        logger.info("Initialising graph database at %s …", graph_db_path)
        graph_db_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a temporary NoteRepository pointed at the notes dir.  We bypass
    # rebuild_index_if_needed because we want full control over the import.
    from parazettel_mcp.config import config

    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    config.notes_dir = notes_dir
    config.graph_db_path = graph_db_path

    try:
        # Instantiate a thin repo that just provides the parser and indexer.
        # We must NOT call rebuild_index_if_needed automatically, so we patch
        # the method temporarily.
        repo = _build_repo_without_auto_rebuild()

        # Parse all notes (pass 1 and 2 happen inside rebuild_index)
        imported = 0
        skipped = 0
        notes = []

        for file_path in note_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                note = repo._parse_note_from_markdown(content)
                notes.append(note)
                imported += 1
            except Exception as exc:
                logger.warning("Skipping %s: %s", file_path.name, exc)
                skipped += 1

        logger.info("Parsed %d notes (%d skipped).", imported, skipped)

        if dry_run:
            logger.info("Dry run complete – nothing written.")
            return

        logger.info("Importing into graph database (pass 1: nodes) …")
        import kuzu

        db = init_graph_db(graph_db_path)
        conn = kuzu.Connection(db)

        for note in notes:
            try:
                repo._index_note_nodes_only(note, conn)
            except Exception as exc:
                logger.warning("Failed to index node %s (%s): %s", note.id, note.title, exc)

        logger.info("Importing into graph database (pass 2: relationships) …")
        for note in notes:
            try:
                repo._index_note_relations(note, conn)
            except Exception as exc:
                logger.warning(
                    "Failed to index relationships for %s (%s): %s",
                    note.id,
                    note.title,
                    exc,
                )

        # Verify
        result = conn.execute("MATCH (n:Note) RETURN count(n) AS cnt")
        note_count = result.get_next()[0]
        result = conn.execute("MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS cnt")
        link_count = result.get_next()[0]
        result = conn.execute("MATCH (t:Tag) RETURN count(t) AS cnt")
        tag_count = result.get_next()[0]

        logger.info(
            "Migration complete. Graph database contains: "
            "%d note(s), %d link(s), %d tag(s).",
            note_count,
            link_count,
            tag_count,
        )

    finally:
        config.notes_dir = original_notes_dir
        config.graph_db_path = original_graph_db_path


def _build_repo_without_auto_rebuild():
    """Build a NoteRepository without triggering auto-rebuild."""
    from parazettel_mcp.storage.note_repository import NoteRepository

    # Temporarily replace rebuild_index_if_needed with a no-op so __init__
    # does not try to rebuild (the DB may not exist yet).
    original_method = NoteRepository.rebuild_index_if_needed
    NoteRepository.rebuild_index_if_needed = lambda self: None  # type: ignore[method-assign]
    try:
        from parazettel_mcp.config import config

        repo = NoteRepository(notes_dir=config.get_absolute_path(config.notes_dir))
    finally:
        NoteRepository.rebuild_index_if_needed = original_method  # type: ignore[method-assign]
    return repo


def main() -> None:
    args = parse_args()
    notes_dir = Path(args.notes_dir).expanduser().resolve()
    graph_db_path = Path(args.graph_db_path).expanduser().resolve()
    migrate(notes_dir, graph_db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
