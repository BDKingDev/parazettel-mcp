"""Regression tests: rebuild_index surfaces unparseable files instead of dropping them silently."""

from parazettel_mcp.models.schema import Note, NoteType


def test_rebuild_index_reports_unparseable_files(note_repository):
    """A malformed .md file is recorded in last_rebuild_skipped, not silently lost."""
    repo = note_repository

    # A valid note that should survive the rebuild.
    good = repo.create(
        Note(title="Good Note", content="Real content here.", note_type=NoteType.PERMANENT)
    )

    # A malformed markdown file dropped directly into the notes dir: no frontmatter
    # id/title, so _parse_note_from_markdown raises and the file is skipped.
    bad_path = repo.notes_dir / "broken-note.md"
    bad_path.write_text("not a real note: no frontmatter, no id\n", encoding="utf-8")

    repo.rebuild_index()

    # The bad file is surfaced...
    assert "broken-note.md" in repo.last_rebuild_skipped
    # ...and the good note is still indexed and retrievable.
    assert repo.get(good.id) is not None


def test_rebuild_index_skipped_is_empty_when_all_parse(note_repository):
    """With only valid notes, last_rebuild_skipped is empty."""
    repo = note_repository
    repo.create(Note(title="One", content="First note.", note_type=NoteType.PERMANENT))
    repo.create(Note(title="Two", content="Second note.", note_type=NoteType.PERMANENT))

    repo.rebuild_index()

    assert repo.last_rebuild_skipped == []
