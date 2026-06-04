"""Tests for the resource-tuning defaults (code constants, not env-configurable)."""

from parazettel_mcp.config import (
    DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS,
    DEFAULT_KUZU_BUFFER_POOL_BYTES,
    ZettelkastenConfig,
)


def test_kuzu_buffer_pool_field_uses_constant():
    """The buffer-pool field defaults to the code constant (0 = Kuzu default)."""
    assert ZettelkastenConfig().kuzu_buffer_pool_bytes == DEFAULT_KUZU_BUFFER_POOL_BYTES


def test_daemon_idle_timeout_field_uses_constant():
    """The idle-timeout field defaults to the code constant."""
    assert (
        ZettelkastenConfig().daemon_idle_timeout_seconds
        == DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
    )


def test_daemon_idle_timeout_constant_is_one_hour():
    """Guard the chosen idle-timeout so an accidental edit is caught."""
    assert DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS == 3600.0
