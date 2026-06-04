"""Tests for the embedding provider abstraction (no heavy ML deps required)."""

import builtins

import pytest

from parazettel_mcp.config import ZettelkastenConfig
from parazettel_mcp.services.embedding_provider import (
    EmbeddingProvider,
    FastEmbedProvider,
    HashEmbeddingProvider,
    SentenceTransformerProvider,
    build_embedding_provider,
)


def test_build_returns_none_when_disabled():
    cfg = ZettelkastenConfig(embedding_enabled=False)
    assert build_embedding_provider(cfg) is None


def test_hash_provider_is_deterministic_and_normalized():
    provider = HashEmbeddingProvider(dim=16)
    first = provider.embed_query("hello world")
    second = provider.embed_query("hello world")
    assert len(first) == 16
    assert first == second  # deterministic for identical input
    # Unit length so cosine behaves correctly.
    assert abs(sum(x * x for x in first) - 1.0) < 1e-6
    # Different text yields a different vector.
    assert provider.embed_query("a different note") != first


def test_hash_provider_batch_documents():
    provider = HashEmbeddingProvider(dim=8)
    vectors = provider.embed_documents(["alpha", "beta", "gamma"])
    assert len(vectors) == 3
    assert all(len(v) == 8 for v in vectors)


def test_hash_provider_rejects_nonpositive_dim():
    with pytest.raises(ValueError):
        HashEmbeddingProvider(dim=0)


def test_local_providers_reject_nonpositive_dim():
    # Fail fast on misconfiguration rather than producing empty vectors later.
    with pytest.raises(ValueError):
        SentenceTransformerProvider("some/model", 0)
    with pytest.raises(ValueError):
        FastEmbedProvider("some/model", -1)


def test_factory_builds_hash_provider():
    cfg = ZettelkastenConfig(
        embedding_enabled=True, embedding_provider="hash", embedding_dim=32
    )
    provider = build_embedding_provider(cfg)
    assert isinstance(provider, HashEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.dim == 32
    assert provider.model_id == "hash:deterministic:32"


def test_factory_rejects_unknown_provider():
    cfg = ZettelkastenConfig(embedding_enabled=True, embedding_provider="bogus")
    with pytest.raises(ValueError):
        build_embedding_provider(cfg)


def test_sentence_transformer_provider_reports_missing_dependency(monkeypatch):
    """When sentence-transformers isn't installed, the error names the extra."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = SentenceTransformerProvider("some/model", 8)
    with pytest.raises(RuntimeError, match=r"parazettel-mcp\[embeddings\]"):
        provider.embed_documents(["text"])


def test_sentence_transformer_provider_model_id():
    provider = SentenceTransformerProvider("google/embeddinggemma-300m", 768)
    assert provider.model_id == "sentence-transformers:google/embeddinggemma-300m:768"


def test_factory_builds_fastembed_provider_by_default():
    # "fastembed" is the default provider when embeddings are enabled.
    cfg = ZettelkastenConfig(embedding_enabled=True)
    provider = build_embedding_provider(cfg)
    assert isinstance(provider, FastEmbedProvider)
    assert provider.dim == cfg.embedding_dim
    assert provider.model_id == f"fastembed:{cfg.embedding_model}:{cfg.embedding_dim}"


def test_fastembed_provider_reports_missing_dependency(monkeypatch):
    """When fastembed isn't installed, the error names the lite extra."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = FastEmbedProvider("BAAI/bge-small-en-v1.5", 384)
    with pytest.raises(RuntimeError, match=r"parazettel-mcp\[embeddings-lite\]"):
        provider.embed_documents(["text"])


def test_batch_size_clamped_and_passed_through_factory():
    """batch_size is clamped to >=1 and the factory forwards config's value."""
    assert FastEmbedProvider("m", 8, batch_size=0)._batch_size == 1
    cfg = ZettelkastenConfig(
        embedding_enabled=True, embedding_provider="fastembed", embedding_batch_size=8
    )
    assert build_embedding_provider(cfg)._batch_size == 8


def test_fastembed_passes_batch_size_to_model():
    """embed_documents forwards the configured batch_size to the embedding lib."""
    from unittest.mock import MagicMock

    provider = FastEmbedProvider("m", 4, batch_size=7)
    model = MagicMock()
    model.passage_embed.return_value = [[1.0, 0.0, 0.0, 0.0]]
    provider._model = model  # skip the lazy download
    provider.embed_documents(["hello"])
    assert model.passage_embed.call_args.kwargs.get("batch_size") == 7


def test_sentence_transformer_passes_batch_size_to_model():
    """embed_documents forwards the configured batch_size to model.encode()."""
    from unittest.mock import MagicMock

    provider = SentenceTransformerProvider("m", 4, batch_size=9)
    model = MagicMock()
    model.prompts = {}  # no task prompts -> no prompt_name kwarg
    model.encode.return_value = [[1.0, 0.0, 0.0, 0.0]]
    provider._model = model  # skip the lazy SentenceTransformer load
    provider.embed_documents(["hello"])
    assert model.encode.call_args.kwargs.get("batch_size") == 9
