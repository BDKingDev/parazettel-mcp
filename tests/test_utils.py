"""Tests for small utility helpers."""

import logging
import sys
from datetime import datetime
from types import SimpleNamespace

from parazettel_mcp.utils import format_note_for_display, parse_tags, setup_logging


def _restore_root_logging(saved_level: int, saved_handlers: list) -> None:
    """Close any handlers we added and restore the root logger to its prior state."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler):
            handler.close()
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_setup_logging_falls_back_to_info_and_logs_to_stderr(monkeypatch):
    """Unknown levels fall back to INFO; stderr is always a handler; no file."""
    import parazettel_mcp.utils as utils_mod

    monkeypatch.setattr(utils_mod, "_default_log_file", lambda: None)
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        setup_logging("not-a-real-level", enable_faulthandler=False)
        assert root.level == logging.INFO
        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stderr
            for h in root.handlers
        )
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
    finally:
        _restore_root_logging(saved_level, saved_handlers)


def test_setup_logging_adds_file_handler_alongside_stderr(tmp_path):
    """An explicit log file is added as a FileHandler in addition to stderr."""
    log_file = tmp_path / "parazettel.log"
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        setup_logging("debug", log_file=str(log_file), enable_faulthandler=False)
        assert root.level == logging.DEBUG
        file_handlers = [
            h for h in root.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1
        assert any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stderr
            for h in root.handlers
        )
        logging.getLogger("pzk.test").warning("hello-file-handler")
        for handler in file_handlers:
            handler.flush()
        assert "hello-file-handler" in log_file.read_text(encoding="utf-8")
    finally:
        _restore_root_logging(saved_level, saved_handlers)


def test_setup_logging_without_faulthandler_tears_down_prior_install(tmp_path):
    """Re-initializing without faulthandler must disable it and close its stream."""
    import parazettel_mcp.utils as utils_mod

    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    try:
        log_file = tmp_path / "fh.log"
        setup_logging("info", log_file=str(log_file), enable_faulthandler=True)
        assert utils_mod._faulthandler_stream is not None
        # Re-init without faulthandler: the prior stream must be torn down, not leaked.
        setup_logging("info", log_file=str(log_file), enable_faulthandler=False)
        assert utils_mod._faulthandler_stream is None
    finally:
        _restore_root_logging(saved_level, saved_handlers)


def test_parse_tags_trims_values_and_skips_empty_entries():
    """parse_tags should normalize whitespace and ignore empty items."""
    assert parse_tags("") == []
    assert parse_tags(" alpha, ,beta , gamma ,, ") == ["alpha", "beta", "gamma"]


def test_format_note_for_display_includes_tags_and_link_descriptions():
    """format_note_for_display should render tags and both link formats."""
    link_with_description = SimpleNamespace(
        link_type=SimpleNamespace(value="reference"),
        target_id="note-2",
        description="Related note",
    )
    link_without_description = SimpleNamespace(
        link_type=SimpleNamespace(value="supports"),
        target_id="note-3",
        description="",
    )

    rendered = format_note_for_display(
        title="Test Note",
        id="note-1",
        content="Body text",
        tags=["alpha", "beta"],
        created_at=datetime(2026, 4, 22, 10, 0, 0),
        updated_at=datetime(2026, 4, 22, 11, 0, 0),
        links=[link_with_description, link_without_description],
    )

    assert "# Test Note" in rendered
    assert "Tags: alpha, beta" in rendered
    assert "Body text" in rendered
    assert "- reference: note-2 - Related note" in rendered
    assert "- supports: note-3" in rendered
