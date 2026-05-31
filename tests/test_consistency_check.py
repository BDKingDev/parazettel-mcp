"""Tests for NoteRepository.check_consistency — file vs graph-index drift detection."""

from parazettel_mcp.models.schema import Note, NoteType


def _make(repo, title, content):
    return repo.create(
        Note(title=title, content=content, note_type=NoteType.PERMANENT)
    )


def test_consistency_clean_vault_reports_consistent(note_repository):
    """A vault written entirely through the repo is internally consistent."""
    _make(note_repository, "One", "First note body.")
    _make(note_repository, "Two", "Second note body.")

    report = note_repository.check_consistency()

    assert report["consistent"] is True
    assert report["missing_from_index"] == []
    assert report["missing_from_files"] == []
    assert report["content_drift"] == []
    assert report["in_sync"] == report["total_files"] == report["total_indexed"] == 2


def test_consistency_detects_file_missing_from_index(note_repository):
    """A markdown file added on disk (not via the server) is flagged as un-indexed."""
    indexed = _make(note_repository, "Indexed", "Indexed body.")
    # Drop a raw note file straight onto disk, bypassing the index.
    orphan_id = "20990101T000000000000000"
    (note_repository.notes_dir / f"{orphan_id}.md").write_text(
        "---\n"
        f"id: {orphan_id}\n"
        "title: Disk Only\n"
        "type: permanent\n"
        "created: 2099-01-01T00:00:00\n"
        "updated: 2099-01-01T00:00:00\n"
        "---\n\n# Disk Only\n\nAdded outside the server.\n",
        encoding="utf-8",
    )

    report = note_repository.check_consistency()

    assert orphan_id in report["missing_from_index"]
    assert indexed.id not in report["missing_from_index"]
    assert report["consistent"] is False


def test_consistency_detects_note_missing_from_files(note_repository):
    """A note whose file is deleted directly is flagged as missing from disk."""
    keep = _make(note_repository, "Keep", "Stays on disk.")
    gone = _make(note_repository, "Gone", "File will be removed.")
    (note_repository.notes_dir / f"{gone.id}.md").unlink()

    report = note_repository.check_consistency()

    assert gone.id in report["missing_from_files"]
    assert keep.id not in report["missing_from_files"]
    assert report["consistent"] is False


def test_consistency_detects_content_drift(note_repository):
    """An external edit to a note body (same id) is flagged as content drift."""
    note = _make(note_repository, "Editable", "Original body content.")
    path = note_repository.notes_dir / f"{note.id}.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("Original body content.", "Externally edited body content."),
        encoding="utf-8",
    )

    report = note_repository.check_consistency()

    assert note.id in report["content_drift"]
    assert report["consistent"] is False
    # The ID set is unchanged, so the cheap startup check would miss this.
    assert report["missing_from_index"] == []
    assert report["missing_from_files"] == []


def test_consistency_rebuild_restores_consistency(note_repository):
    """rebuild_index reconciles drift that check_consistency reports."""
    note = _make(note_repository, "Editable", "Original body content.")
    path = note_repository.notes_dir / f"{note.id}.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("Original body content.", "Externally edited body content."),
        encoding="utf-8",
    )
    assert note_repository.check_consistency()["consistent"] is False

    note_repository.rebuild_index()

    assert note_repository.check_consistency()["consistent"] is True
