"""Tests for the resource-tuning defaults (code constants, not env-configurable)."""

from parazettel_mcp.config import (
    DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS,
    DEFAULT_KUZU_BUFFER_POOL_BYTES,
    ZettelkastenConfig,
)


def test_kuzu_buffer_pool_field_uses_constant():
    """The buffer-pool field defaults to the code constant (env var overrides it)."""
    assert ZettelkastenConfig().kuzu_buffer_pool_bytes == DEFAULT_KUZU_BUFFER_POOL_BYTES


def test_kuzu_buffer_pool_default_is_bounded():
    """The default must be a positive cap, not 0 (Kuzu's ~80%-of-RAM default).

    A long-lived daemon left at 0 balloons commit charge to tens of GB; guard the
    bound so an accidental revert to 0 is caught.
    """
    assert DEFAULT_KUZU_BUFFER_POOL_BYTES == 3 * 1024**3  # 3 GiB
    # Bounded and positive — never 0 (Kuzu's ~80%-of-RAM default) and not so
    # large it dominates commit charge on a long-lived daemon.
    assert 0 < DEFAULT_KUZU_BUFFER_POOL_BYTES <= 4 * 1024**3


def test_daemon_idle_timeout_field_uses_constant():
    """The idle-timeout field defaults to the code constant."""
    assert (
        ZettelkastenConfig().daemon_idle_timeout_seconds
        == DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
    )


def test_daemon_idle_timeout_constant_is_one_hour():
    """Guard the chosen idle-timeout so an accidental edit is caught."""
    assert DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS == 3600.0
