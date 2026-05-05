"""Common test fixtures for the Zettelkasten MCP server."""

import os
import tempfile
from pathlib import Path

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.services.zettel_service import ZettelService
from parazettel_mcp.storage.note_repository import NoteRepository


@pytest.fixture
def temp_dirs():
    """Create temporary directories for notes and graph database."""
    with tempfile.TemporaryDirectory() as notes_dir:
        with tempfile.TemporaryDirectory() as graph_db_dir:
            yield Path(notes_dir), Path(graph_db_dir)


@pytest.fixture
def test_config(temp_dirs):
    """Configure with test paths."""
    notes_dir, graph_db_dir = temp_dirs
    graph_db_path = graph_db_dir / "test_graph.kuzu"
    # Save original config values
    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    # Update config for tests
    config.notes_dir = notes_dir
    config.graph_db_path = graph_db_path
    yield config
    # Restore original config
    config.notes_dir = original_notes_dir
    config.graph_db_path = original_graph_db_path


@pytest.fixture
def note_repository(test_config):
    """Create a test note repository backed by a temporary Kuzu graph database."""
    repository = NoteRepository(notes_dir=test_config.notes_dir)
    yield repository
    repository.close()


@pytest.fixture
def zettel_service(note_repository):
    """Create a test ZettelService."""
    service = ZettelService(repository=note_repository)
    yield service
