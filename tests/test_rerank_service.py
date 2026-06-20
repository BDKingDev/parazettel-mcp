"""Tests for the dedup reranker now living in the data-owning ZettelService.

The reranker moved out of the per-session MCP facade and into ZettelService so
the heavy fastembed model loads ONCE (in the daemon) and is pre-warmed on the
main thread — a facade that imported fastembed on its worker thread deadlocked
on the Windows loader lock. These tests pin that contract without a real model
or a database (the repository is mocked; rerank only touches the reranker).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from parazettel_mcp.daemon.server import ALLOWED_SERVICE_METHODS
from parazettel_mcp.services.reranker import RerankerError
from parazettel_mcp.services.zettel_service import ZettelService


def _service_without_db():
    """A ZettelService with a mocked repository (no Kuzu DB needed here)."""
    # conftest's autouse fixture blanks dedup_rerank_model, so __init__ builds no
    # reranker; tests inject their own fake below.
    return ZettelService(repository=MagicMock())


def test_rerank_raises_when_not_configured():
    """With no reranker, rerank fails loud (RerankerError), never silently."""
    service = _service_without_db()
    service._reranker = None
    with pytest.raises(RerankerError):
        service.rerank("q", ["a", "b"])


def test_rerank_delegates_to_the_reranker_score():
    """rerank forwards the query/documents to the reranker and returns its scores."""
    service = _service_without_db()
    captured = {}

    def _score(query, docs):
        captured["query"] = query
        captured["docs"] = list(docs)
        return [float(len(d)) for d in docs]

    service._reranker = SimpleNamespace(score=_score)
    assert service.rerank("query", ["ab", "abcd"]) == [2.0, 4.0]
    assert captured == {"query": "query", "docs": ["ab", "abcd"]}


def test_initialize_prewarms_the_reranker_on_the_calling_thread():
    """initialize() eagerly loads the model (main-thread, deadlock-safe)."""
    service = _service_without_db()
    fake = MagicMock()
    fake.prewarm.return_value = True
    service._reranker = fake
    service.initialize()
    fake.prewarm.assert_called_once_with()


def test_initialize_is_a_noop_when_the_reranker_is_disabled():
    """initialize() must not raise when no reranker is configured."""
    service = _service_without_db()
    service._reranker = None
    service.initialize()  # no exception


def test_initialize_is_idempotent():
    """A second initialize() does not pre-warm again.

    The daemon calls zettel_service.initialize() AND SearchService.initialize()
    forwards to the same shared instance, so the eager load must run once only.
    """
    service = _service_without_db()
    fake = MagicMock()
    fake.prewarm.return_value = True
    service._reranker = fake
    service.initialize()
    service.initialize()
    fake.prewarm.assert_called_once_with()


def test_rerank_is_an_allowed_daemon_rpc_method():
    """The facade reaches the daemon's reranker via the zettel_service RPC surface."""
    assert "rerank" in ALLOWED_SERVICE_METHODS["zettel_service"]
