"""Tests for the dedup cross-encoder reranker (no heavy ML deps required)."""

import sys
import types

from parazettel_mcp.config import ZettelkastenConfig
from parazettel_mcp.services.reranker import (
    FastEmbedReranker,
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
    """Install a fake fastembed TextCrossEncoder that records its calls."""

    class FakeCrossEncoder:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def rerank(self, query, documents):
            captured["query"] = query
            captured["docs"] = list(documents)
            return list(scores)

    module = types.ModuleType("fastembed.rerank.cross_encoder")
    module.TextCrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", module)


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
