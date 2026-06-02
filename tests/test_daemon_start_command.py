"""Tests for the absolute daemon-start command surfaced in daemon-down UX."""

import os

from parazettel_mcp.config import config
from parazettel_mcp.main import format_daemon_status


def test_format_daemon_start_command_is_absolute_and_complete():
    """The rendered command must be runnable from any directory."""
    cmd = config.format_daemon_start_command()

    assert "--run-daemon" in cmd
    assert "--notes-dir" in cmd
    assert "--graph-db-path" in cmd
    assert "--daemon-host" in cmd
    assert "--daemon-port" in cmd

    # The interpreter (first token) must be an absolute path so a bare `python`
    # in some other repo's environment is never assumed.
    first_token = cmd.split(" ", 1)[0].strip('"')
    assert os.path.isabs(first_token), first_token


def test_daemon_status_message_includes_start_command():
    """`--daemon-status` for a stopped daemon tells the user how to start it."""
    status = {
        "healthy": False,
        "health": None,
        "pid": None,
        "pid_running": False,
        "pid_file": "irrelevant",
        "base_url": "http://127.0.0.1:8766",
        "error": "Parazettel daemon is unavailable.",
    }

    message = format_daemon_status(status)

    assert "Parazettel daemon is not running." in message
    assert "To start it:" in message
    assert "--run-daemon" in message
