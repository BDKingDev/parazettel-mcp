"""Tests for the absolute daemon-start command surfaced in daemon-down UX."""

import os
from pathlib import Path

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


def test_format_daemon_restart_command_prefers_embedding_aware_script():
    """When the repo's restart script is present, the daemon-down hint points
    to it (it preserves the embedding env), not the raw --run-daemon command."""
    cmd = config.format_daemon_restart_command()

    repo_root = Path(__file__).resolve().parents[1]
    script_name = "restart_daemon.ps1" if os.name == "nt" else "restart_daemon.sh"
    script = repo_root / "scripts" / script_name

    if script.is_file():
        # Absolute path to the script, run with the platform's interpreter.
        assert script_name in cmd
        assert "--run-daemon" not in cmd
        runner = "pwsh" if os.name == "nt" else "bash"
        assert cmd.startswith(runner + " ")
        first_path = cmd.split(" ", 1)[1].strip('"')
        assert os.path.isabs(first_path), first_path
    else:
        # No script (non-editable install) -> falls back to the raw command.
        assert "--run-daemon" in cmd


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
