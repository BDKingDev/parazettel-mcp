"""Common test fixtures for the Zettelkasten MCP server."""

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.services.zettel_service import ZettelService
from parazettel_mcp.storage.note_repository import NoteRepository


@pytest.fixture(autouse=True)
def _disable_dedup_reranker():
    """Keep the dedup cross-encoder off by default so no test downloads a model.

    Reranker-specific tests use their own config instances or inject a fake, so
    this only guards against an unrelated test accidentally triggering a real
    model load via the global config singleton.
    """
    original = config.dedup_rerank_model
    config.dedup_rerank_model = ""
    yield
    config.dedup_rerank_model = original


@pytest.fixture
def temp_dirs():
    """Create workspace-local directories for notes and graph database."""
    test_root = (
        Path(__file__).resolve().parents[1] / ".tmp" / "test-fixtures" / uuid4().hex
    )
    notes_dir = test_root / "notes"
    graph_db_dir = test_root / "db"
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield notes_dir, graph_db_dir
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


@pytest.fixture
def test_config(temp_dirs):
    """Configure with test paths."""
    notes_dir, graph_db_dir = temp_dirs
    graph_db_path = graph_db_dir / "test_graph.kuzu"
    # Save original config values
    original_notes_dir = config.notes_dir
    original_graph_db_path = config.graph_db_path
    original_backend_mode = config.backend_mode
    original_daemon_host = config.daemon_host
    original_daemon_port = config.daemon_port
    # Update config for tests
    config.notes_dir = notes_dir
    config.graph_db_path = graph_db_path
    config.backend_mode = "direct"
    config.daemon_host = "127.0.0.1"
    config.daemon_port = 8766
    yield config
    # Restore original config
    config.notes_dir = original_notes_dir
    config.graph_db_path = original_graph_db_path
    config.backend_mode = original_backend_mode
    config.daemon_host = original_daemon_host
    config.daemon_port = original_daemon_port


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
