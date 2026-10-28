"""Common test fixtures for the Zettelkasten MCP server."""

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from parazettel_mcp.config import config
from parazettel_mcp.services.zettel_service import ZettelService
from parazettel_mcp.storage.note_repository import NoteRepository


_DEFAULT_PATH_FIELDS = (
    "notes_dir",
    "graph_db_path",
    "database_path",
    "daemon_runtime_dir",
)


@pytest.fixture(autouse=True, scope="session")
def _isolate_default_paths(tmp_path_factory):
    """Point the global config's storage and runtime paths at a temp dir.

    Their defaults are the cwd-relative vault (``data/...``) and the shared
    ``<tempdir>/parazettel-daemon`` runtime dir a real daemon uses for its PID
    file, start lock and logs. A test that never sets its own paths would
    otherwise create or lock those. Tests that do set paths (``test_config``)
    still override these per test.
    """
    root = tmp_path_factory.mktemp("default-paths")
    originals = {name: getattr(config, name) for name in _DEFAULT_PATH_FIELDS}
    config.notes_dir = root / "notes"
    # index_db_path stays unset, so the index derives from graph_db_path as it
    # does by default.
    config.graph_db_path = root / "db" / "graph.kuzu"
    config.database_path = root / "db" / "parazettel.db"
    config.daemon_runtime_dir = root / "runtime"
    yield
    for name, value in originals.items():
        setattr(config, name, value)


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
    """Create workspace-local directories for notes and the index database."""
    test_root = (
        Path(__file__).resolve().parents[1] / ".tmp" / "test-fixtures" / uuid4().hex
    )
    notes_dir = test_root / "notes"
    db_dir = test_root / "db"
    notes_dir.mkdir(parents=True, exist_ok=True)
    db_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield notes_dir, db_dir
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


@pytest.fixture
def test_config(temp_dirs):
    """Configure with test paths.

    Each test gets a private SQLite index; creating one takes milliseconds, so
    there is no shared template to copy.
    """
    notes_dir, db_dir = temp_dirs
    # Save original config values
    original_notes_dir = config.notes_dir
    original_index_db_path = config.index_db_path
    original_graph_db_path = config.graph_db_path
    original_backend_mode = config.backend_mode
    original_daemon_host = config.daemon_host
    original_daemon_port = config.daemon_port
    # Update config for tests
    config.notes_dir = notes_dir
    config.index_db_path = db_dir / "test_index.sqlite3"
    config.graph_db_path = db_dir / "test_graph.kuzu"
    config.backend_mode = "direct"
    config.daemon_host = "127.0.0.1"
    config.daemon_port = 8766
    yield config
    # Restore original config
    config.notes_dir = original_notes_dir
    config.index_db_path = original_index_db_path
    config.graph_db_path = original_graph_db_path
    config.backend_mode = original_backend_mode
    config.daemon_host = original_daemon_host
    config.daemon_port = original_daemon_port


@pytest.fixture
def note_repository(test_config):
    """Create a test note repository backed by a temporary SQLite index."""
    repository = NoteRepository(notes_dir=test_config.notes_dir)
    yield repository
    repository.close()


@pytest.fixture
def zettel_service(note_repository):
    """Create a test ZettelService."""
    service = ZettelService(repository=note_repository)
    yield service
