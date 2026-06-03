"""Tests for configuration parsing (env -> ZettelkastenConfig fields)."""

from parazettel_mcp.config import ZettelkastenConfig


def test_kuzu_buffer_pool_defaults_to_zero(monkeypatch):
    """Unset PARAZETTEL_KUZU_BUFFER_POOL_MB -> 0 (use Kuzu's own default)."""
    monkeypatch.delenv("PARAZETTEL_KUZU_BUFFER_POOL_MB", raising=False)
    assert ZettelkastenConfig().kuzu_buffer_pool_bytes == 0


def test_kuzu_buffer_pool_mb_converts_to_bytes(monkeypatch):
    """A megabyte value is converted to bytes."""
    monkeypatch.setenv("PARAZETTEL_KUZU_BUFFER_POOL_MB", "256")
    assert ZettelkastenConfig().kuzu_buffer_pool_bytes == 256 * 1024 * 1024


def test_kuzu_buffer_pool_negative_clamped_to_zero(monkeypatch):
    """A negative value is clamped to 0 rather than producing a negative size."""
    monkeypatch.setenv("PARAZETTEL_KUZU_BUFFER_POOL_MB", "-5")
    assert ZettelkastenConfig().kuzu_buffer_pool_bytes == 0
