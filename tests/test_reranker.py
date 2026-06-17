"""Tests for the dedup cross-encoder reranker (no heavy ML deps required)."""

import sys
import threading
import types

import pytest

from parazettel_mcp.config import ZettelkastenConfig
from parazettel_mcp.services.reranker import (
    FastEmbedReranker,
    RerankerError,
    RerankerLoadTimeoutError,
    RerankerProvider,
    build_reranker,
)


def test_build_returns_none_when_embeddings_disabled():
    cfg = ZettelkastenConfig(embedding_enabled=False)
    assert build_reranker(cfg) is None


def test_build_returns_none_when_model_blank():
    cfg = ZettelkastenConfig(embedding_enabled=True, dedup_rerank_model="")
    assert build_reranker(cfg) is None


def test_build_returns_reranker_when_enabled():
    cfg = ZettelkastenConfig(embedding_enabled=True)
    reranker = build_reranker(cfg)
    assert isinstance(reranker, FastEmbedReranker)
    assert isinstance(reranker, RerankerProvider)
    assert reranker.model_id == f"fastembed-rerank:{cfg.dedup_rerank_model}"


def test_score_empty_documents_returns_empty():
    assert FastEmbedReranker("m").score("q", []) == []


def _fake_cross_encoder(monkeypatch, captured, scores):
    """Install a fake fastembed TextCrossEncoder that records its calls.

    Registers the parent packages too: ``from fastembed.rerank.cross_encoder
    import TextCrossEncoder`` needs ``fastembed`` and ``fastembed.rerank`` to
    resolve, so stubbing only the leaf module would still fail when fastembed
    isn't installed (the default when tests run with only the ``dev`` extra).
    """

    class FakeCrossEncoder:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def rerank(self, query, documents):
            captured["query"] = query
            captured["docs"] = list(documents)
            return list(scores)

    fastembed = types.ModuleType("fastembed")
    fastembed.__path__ = []  # mark as a package so submodule imports resolve
    rerank = types.ModuleType("fastembed.rerank")
    rerank.__path__ = []
    cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = FakeCrossEncoder
    fastembed.rerank = rerank
    rerank.cross_encoder = cross_encoder
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)


def test_score_forwards_query_and_documents(monkeypatch):
    captured = {}
    _fake_cross_encoder(monkeypatch, captured, [3.5, -1.0])

    scores = FastEmbedReranker("some/model", device="cpu").score("q", ["a", "b"])

    assert scores == [3.5, -1.0]
    assert captured["query"] == "q"
    assert captured["docs"] == ["a", "b"]
    assert "providers" not in captured["init"]  # CPU passes no providers


def test_cuda_device_requests_provider_and_preloads(monkeypatch):
    captured = {}
    preloaded = {"called": False}
    _fake_cross_encoder(monkeypatch, captured, [1.0])

    ort = types.ModuleType("onnxruntime")
    ort.preload_dlls = lambda: preloaded.__setitem__("called", True)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    FastEmbedReranker("m", device="cuda").score("q", ["d"])

    assert captured["init"].get("providers") == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    assert preloaded["called"] is True


def _blocking_cross_encoder(monkeypatch, started, release):
    """Install a fake TextCrossEncoder whose load blocks until *release* is set.

    Simulates a wedged model-cache filelock: construction hangs, so the load
    worker thread never returns and the caller must hit the load timeout.
    """

    class BlockingCrossEncoder:
        def __init__(self, **kwargs):
            started.set()
            release.wait(timeout=10)  # bounded so a leaked thread always exits

        def rerank(self, query, documents):
            return [0.0 for _ in documents]

    fastembed = types.ModuleType("fastembed")
    fastembed.__path__ = []
    rerank = types.ModuleType("fastembed.rerank")
    rerank.__path__ = []
    cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = BlockingCrossEncoder
    fastembed.rerank = rerank
    rerank.cross_encoder = cross_encoder
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)


def test_score_raises_on_load_timeout(monkeypatch):
    """A wedged model load must raise (not hang) once the timeout elapses."""
    started = threading.Event()
    release = threading.Event()
    _blocking_cross_encoder(monkeypatch, started, release)

    reranker = FastEmbedReranker("m", device="cpu", load_timeout=0.3)
    try:
        with pytest.raises(RerankerLoadTimeoutError):
            reranker.score("q", ["a"])
        assert started.is_set()  # the load actually began before timing out
    finally:
        # Release the blocked worker so its (daemon) thread can exit.
        release.set()
        if reranker._load_thread is not None:
            reranker._load_thread.join(timeout=5)


def test_score_reuses_in_flight_load_after_a_timeout(monkeypatch):
    """A load that finishes after one call's timeout is reused by the next call."""
    started = threading.Event()
    release = threading.Event()
    _blocking_cross_encoder(monkeypatch, started, release)

    reranker = FastEmbedReranker("m", device="cpu", load_timeout=0.3)
    try:
        with pytest.raises(RerankerLoadTimeoutError):
            reranker.score("q", ["a"])  # first call times out on the wedged load
        release.set()  # the wedged load now completes in the background
        # The same in-flight future is reused — no second load, no error.
        assert reranker.score("q", ["a", "b"]) == [0.0, 0.0]
    finally:
        release.set()
        if reranker._load_thread is not None:
            reranker._load_thread.join(timeout=5)


def test_missing_fastembed_raises_reranker_error(monkeypatch):
    """A missing cross-encoder dependency surfaces as RerankerError, not a hang."""
    # Make `import fastembed...` fail inside the load worker.
    for name in list(sys.modules):
        if name == "fastembed" or name.startswith("fastembed."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        FastEmbedReranker,
        "_preload_cuda_dlls",
        staticmethod(lambda: None),
    )

    import builtins

    real_import = builtins.__import__

    def _no_fastembed(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("fastembed not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)

    reranker = FastEmbedReranker("m", device="cpu", load_timeout=5)
    try:
        with pytest.raises(RerankerError):
            reranker.score("q", ["a"])
    finally:
        if reranker._load_thread is not None:
            reranker._load_thread.join(timeout=5)


def test_build_passes_load_timeout_from_config():
    cfg = ZettelkastenConfig(
        embedding_enabled=True, dedup_rerank_load_timeout_seconds=12.5
    )
    reranker = build_reranker(cfg)
    assert isinstance(reranker, FastEmbedReranker)
    assert reranker._load_timeout == 12.5
