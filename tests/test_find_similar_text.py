"""Tests for find_similar_to_text — raw-text semantic search (pre-create check)."""

from unittest.mock import MagicMock

from parazettel_mcp.config import config
from parazettel_mcp.services.zettel_service import ZettelService


def _note(note_id, title="t", content="c"):
    n = MagicMock()
    n.id = note_id
    n.title = title
    n.content = content
    n.tags = []
    return n


def test_returns_empty_when_embeddings_disabled(monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", False)
    repo = MagicMock()
    svc = ZettelService(repository=repo)
    assert svc.find_similar_to_text("a draft claim") == []
    repo.vector_search.assert_not_called()


def test_returns_empty_for_blank_text(monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", True)
    repo = MagicMock()
    svc = ZettelService(repository=repo)
    assert svc.find_similar_to_text("   ") == []
    repo.vector_search.assert_not_called()


def test_converts_distance_to_cosine_and_sorts(monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_metric", "cosine")
    repo = MagicMock()
    repo.vector_search.return_value = [("n1", 0.1), ("n2", 0.4)]  # distances
    notes = {"n1": _note("n1"), "n2": _note("n2")}
    repo.get.side_effect = lambda i: notes.get(i)

    res = ZettelService(repository=repo).find_similar_to_text(
        "draft", threshold=0.5, limit=5
    )

    # cosine = 1 - distance -> n1=0.9, n2=0.6; both clear 0.5, sorted high->low.
    assert [n.id for n, _ in res] == ["n1", "n2"]
    # 1 - 0.1 isn't exactly representable, so compare with a tolerance like n2.
    assert abs(res[0][1] - 0.9) < 1e-9
    assert abs(res[1][1] - 0.6) < 1e-9


def test_filters_below_threshold(monkeypatch):
    monkeypatch.setattr(config, "embedding_enabled", True)
    monkeypatch.setattr(config, "embedding_metric", "cosine")
    repo = MagicMock()
    repo.vector_search.return_value = [("n1", 0.2), ("n2", 0.7)]  # cos 0.8, 0.3
    notes = {"n1": _note("n1"), "n2": _note("n2")}
    repo.get.side_effect = lambda i: notes.get(i)

    res = ZettelService(repository=repo).find_similar_to_text("draft", threshold=0.5)

    assert [n.id for n, _ in res] == ["n1"]  # n2 (cos 0.3) filtered out


def test_find_similar_to_text_is_in_daemon_rpc_allowlist():
    """Daemon-mode RPC dispatch must allow the method (direct-mode tests miss it)."""
    from parazettel_mcp.daemon.server import ALLOWED_SERVICE_METHODS

    assert "find_similar_to_text" in ALLOWED_SERVICE_METHODS["zettel_service"]


def test_returns_empty_for_nonpositive_limit(monkeypatch):
    """A non-positive limit is rejected explicitly, not coerced to 1."""
    monkeypatch.setattr(config, "embedding_enabled", True)
    repo = MagicMock()
    svc = ZettelService(repository=repo)
    assert svc.find_similar_to_text("x", limit=0) == []
    assert svc.find_similar_to_text("x", limit=-5) == []
    repo.vector_search.assert_not_called()
