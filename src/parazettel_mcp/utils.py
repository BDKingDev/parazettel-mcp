"""Utility functions for the Zettelkasten MCP server."""

import faulthandler
import logging
import os
import sys
from datetime import datetime
from typing import Optional

_LOG_FORMAT = "%(asctime)s [%(levelname)s] pid=%(process)d %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
# Kept open for the process lifetime so faulthandler can write a native-crash
# stack into the log file; closed and reopened on re-init.
_faulthandler_stream = None


def _default_log_file() -> Optional[str]:
    """Per-PID log file under the runtime ``logs/`` dir, or ``None`` if unavailable.

    One file per process so a specific facade's or daemon's history survives the
    process and can be read AFTER the fact: the stdio facade's stderr is
    ephemeral, and an auto-spawned daemon's stdout/stderr go to DEVNULL, so
    without this a reranker stall or a crash would leave no trace to debug.
    """
    try:
        from parazettel_mcp.config import config

        log_dir = config.get_daemon_runtime_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return str(log_dir / f"parazettel-{os.getpid()}.log")
    except Exception:
        return None


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    *,
    enable_faulthandler: bool = True,
):
    """Configure root logging to stderr AND a persistent per-process file.

    Both destinations are used: stderr for live/interactive runs (it is safe for
    the stdio facade — stdout carries the MCP protocol), and a file so logs
    persist for after-the-fact debugging. faulthandler additionally dumps native
    crash stacks (segfaults / access violations) into the log file — the one
    failure class Python logging cannot catch.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Explicit log file path; when omitted, a per-PID file under the
            runtime ``logs/`` dir is used (see :func:`_default_log_file`).
        enable_faulthandler: Install faulthandler to the log file. Disable in
            tests so no file handle outlives the test.
    """
    global _faulthandler_stream

    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT)
    root = logging.getLogger()
    root.setLevel(numeric_level)
    # Drop handlers from a prior init so output isn't duplicated on re-config.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    target = log_file or _default_log_file()
    if target:
        try:
            file_handler = logging.FileHandler(target, mode="a", encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            target = None

    if target and enable_faulthandler:
        try:
            if _faulthandler_stream is not None:
                _faulthandler_stream.close()
        except Exception:
            pass
        try:
            _faulthandler_stream = open(target, "a", encoding="utf-8")
            faulthandler.enable(file=_faulthandler_stream)
        except OSError:
            pass


def parse_tags(tags_str: str) -> list[str]:
    """Parse a comma-separated list of tags into a list of tag strings.
    Args:
        tags_str: Comma-separated string of tags
    Returns:
        List of tag strings
    """
    if not tags_str:
        return []
    return [tag.strip() for tag in tags_str.split(",") if tag.strip()]


def format_note_for_display(
    title: str,
    id: str,
    content: str,
    tags: list[str],
    created_at: datetime,
    updated_at: datetime,
    links: Optional[list] = None,
) -> str:
    """Format a note for display in the console.
    Args:
        title: Note title
        id: Note ID
        content: Note content
        tags: List of tags
        created_at: Creation timestamp
        updated_at: Update timestamp
        links: Optional list of links
    Returns:
        Formatted string representation of the note
    """
    result = f"# {title}\n"
    result += f"ID: {id}\n"
    result += f"Created: {created_at.isoformat()}\n"
    result += f"Updated: {updated_at.isoformat()}\n"

    if tags:
        result += f"Tags: {', '.join(tags)}\n"

    result += f"\n{content}\n"

    if links:
        result += "\n## Links\n"
        for link in links:
            if hasattr(link, "description") and link.description:
                result += (
                    f"- {link.link_type.value}: {link.target_id} - {link.description}\n"
                )
            else:
                result += f"- {link.link_type.value}: {link.target_id}\n"

    return result
