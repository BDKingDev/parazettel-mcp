"""Tests for the dedup cross-encoder reranker (no heavy ML deps required)."""

import sys
import threading
import time
import types

import pytest

from parazettel_mcp.config import ZettelkastenConfig
from parazettel_mcp.services.reranker import (
    FastEmbedReranker,
    RerankerError,
    RerankerLoadTimeoutError,
    RerankerProvider,
    build_reranker,
    reranker_enabled,
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
    # CPU must pass an EXPLICIT CPU-only provider list, else fastembed defaults to
    # trying CUDA (spinning up / failing over from the GPU on every load).
    assert captured["init"].get("providers") == ["CPUExecutionProvider"]


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


def test_cpu_device_uses_cpu_provider_and_does_not_preload_cuda(monkeypatch):
    """A CPU device passes a CPU-only provider list and never touches CUDA."""
    captured = {}
    preloaded = {"called": False}
    _fake_cross_encoder(monkeypatch, captured, [1.0])

    ort = types.ModuleType("onnxruntime")
    ort.preload_dlls = lambda: preloaded.__setitem__("called", True)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    FastEmbedReranker("m", device="cpu").score("q", ["d"])

    assert captured["init"].get("providers") == ["CPUExecutionProvider"]
    assert preloaded["called"] is False  # no CUDA DLL preload for a CPU device


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


def test_score_succeeds_once_a_wedged_load_finally_completes(monkeypatch):
    """After a timeout the next calls fast-fail, but once the load actually
    finishes the SAME load is reused and scoring works (no second load)."""
    started = threading.Event()
    release = threading.Event()
    _blocking_cross_encoder(monkeypatch, started, release)

    reranker = FastEmbedReranker("m", device="cpu", load_timeout=0.3)
    try:
        with pytest.raises(RerankerLoadTimeoutError):
            reranker.score("q", ["a"])  # first call times out on the wedged load
        release.set()  # let the wedged load complete...
        reranker._load_thread.join(timeout=5)  # ...and wait until it actually has
        # The completed in-flight future is reused — no second load, no error.
        assert reranker.score("q", ["a", "b"]) == [0.0, 0.0]
    finally:
        release.set()
        if reranker._load_thread is not None:
            reranker._load_thread.join(timeout=5)


def test_second_call_fast_fails_after_a_load_timeout(monkeypatch):
    """Once a wait hits the timeout, later calls fail fast instead of re-paying it.

    A batch of N notes must not block N * timeout on the same wedged load.
    """
    started = threading.Event()
    release = threading.Event()
    _blocking_cross_encoder(monkeypatch, started, release)

    reranker = FastEmbedReranker("m", device="cpu", load_timeout=0.3)
    try:
        t0 = time.perf_counter()
        with pytest.raises(RerankerLoadTimeoutError):
            reranker.score("q", ["a"])  # first call pays the full timeout
        first = time.perf_counter() - t0

        t1 = time.perf_counter()
        with pytest.raises(RerankerLoadTimeoutError):
            reranker.score("q", ["a"])  # second call fast-fails (load still wedged)
        second = time.perf_counter() - t1

        assert reranker._load_timed_out is True
        assert second < first / 2  # didn't re-pay the timeout
        assert second < 0.1
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


def test_prewarm_loads_synchronously_without_a_worker_thread(monkeypatch):
    """prewarm() builds the model on the CALLING thread (no load worker spawned).

    This is the deadlock-safe path: importing fastembed's C-extensions must
    happen on the main thread at startup, NOT on the worker thread _ensure_model
    spawns (which deadlocks on the Windows loader lock inside a live facade).
    """
    captured = {}
    _fake_cross_encoder(monkeypatch, captured, [1.0, 2.0])

    reranker = FastEmbedReranker("m", device="cpu")
    assert reranker.prewarm() is True
    assert reranker._model is not None
    # No background load was used — the model was built inline on this thread.
    assert reranker._load_thread is None
    assert reranker._load_future is None
    # A subsequent score reuses the pre-warmed model (the fake records the call).
    assert reranker.score("q", ["a", "b"]) == [1.0, 2.0]
    assert captured["init"].get("providers") == ["CPUExecutionProvider"]


def test_prewarm_is_idempotent(monkeypatch):
    """A second prewarm() is a no-op once the model is loaded."""
    captured = {"builds": 0}

    class FakeCrossEncoder:
        def __init__(self, **kwargs):
            captured["builds"] += 1

        def rerank(self, query, documents):
            return [0.0 for _ in documents]

    fastembed = types.ModuleType("fastembed")
    fastembed.__path__ = []
    rerank = types.ModuleType("fastembed.rerank")
    rerank.__path__ = []
    cross_encoder = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_encoder.TextCrossEncoder = FakeCrossEncoder
    fastembed.rerank = rerank
    rerank.cross_encoder = cross_encoder
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_encoder)

    reranker = FastEmbedReranker("m", device="cpu")
    assert reranker.prewarm() is True
    assert reranker.prewarm() is True
    assert captured["builds"] == 1  # built exactly once


def test_prewarm_returns_false_on_failure_without_raising(monkeypatch):
    """A failed pre-warm logs and returns False — startup must not crash."""
    for name in list(sys.modules):
        if name == "fastembed" or name.startswith("fastembed."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        FastEmbedReranker, "_preload_cuda_dlls", staticmethod(lambda: None)
    )

    import builtins

    real_import = builtins.__import__

    def _no_fastembed(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("fastembed not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fastembed)

    reranker = FastEmbedReranker("m", device="cpu")
    assert reranker.prewarm() is False  # does not raise
    assert reranker._model is None  # left unloaded for a later lazy attempt


def test_reranker_enabled_matches_build_reranker():
    """reranker_enabled mirrors build_reranker's on/off decision, without building."""
    assert reranker_enabled(ZettelkastenConfig(embedding_enabled=False)) is False
    assert (
        reranker_enabled(
            ZettelkastenConfig(embedding_enabled=True, dedup_rerank_model="")
        )
        is False
    )
    on = ZettelkastenConfig(embedding_enabled=True, dedup_rerank_model="x/model")
    assert reranker_enabled(on) is True
    assert (build_reranker(on) is not None) == reranker_enabled(on)


def test_build_passes_load_timeout_from_config():
    cfg = ZettelkastenConfig(
        embedding_enabled=True, dedup_rerank_load_timeout_seconds=12.5
    )
    reranker = build_reranker(cfg)
    assert isinstance(reranker, FastEmbedReranker)
    assert reranker._load_timeout == 12.5


def test_build_reranker_defaults_to_cpu_not_embedding_device(monkeypatch):
    """The dedup reranker must NOT inherit a GPU embedder's device.

    It defaults to CPU independently of the embedder so the small cross-encoder
    (run only on the <=5 BM25 candidates) leaves the card to the embedder.
    """
    monkeypatch.delenv("PARAZETTEL_DEDUP_RERANK_DEVICE", raising=False)
    cfg = ZettelkastenConfig(
        embedding_enabled=True,
        embedding_device="cuda",
        dedup_rerank_model="x/model",  # explicit so an empty ambient config can't no-op
    )
    reranker = build_reranker(cfg)
    assert isinstance(reranker, FastEmbedReranker)
    assert reranker._device == "cpu"


def test_build_reranker_honors_explicit_rerank_device():
    """An explicit dedup_rerank_device (e.g. for a single always-on session) wins."""
    cfg = ZettelkastenConfig(
        embedding_enabled=True,
        embedding_device="cuda",
        dedup_rerank_device="cuda",
        dedup_rerank_model="x/model",  # explicit so an empty ambient config can't no-op
    )
    assert build_reranker(cfg)._device == "cuda"
