#!/usr/bin/env python
"""Main entry point for the Zettelkasten MCP server."""
import argparse
import logging
import os
import sys
from pathlib import Path

from parazettel_mcp.config import config
from parazettel_mcp.daemon.server import ParazettelDaemonServer
from parazettel_mcp.server.mcp_server import ZettelkastenMcpServer
from parazettel_mcp.utils import setup_logging


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Zettelkasten MCP Server")
    parser.add_argument(
        "--notes-dir",
        help="Directory for storing note files",
        type=str,
        default=os.environ.get("PARAZETTEL_NOTES_DIR"),
    )
    parser.add_argument(
        "--graph-db-path",
        help="Kuzu graph database directory path",
        type=str,
        default=os.environ.get("PARAZETTEL_GRAPH_DB_PATH"),
    )
    parser.add_argument(
        "--database-path",
        help="Deprecated alias for the graph DB path. Legacy *.db values map to a sibling graph.kuzu path.",
        type=str,
        default=os.environ.get("PARAZETTEL_DATABASE_PATH"),
    )
    parser.add_argument(
        "--log-level",
        help="Logging level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=os.environ.get("PARAZETTEL_LOG_LEVEL", "INFO"),
    )
    parser.add_argument(
        "--transport",
        help="MCP transport to use",
        choices=["stdio", "sse"],
        default=os.environ.get("PARAZETTEL_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        help="Host to bind when using SSE transport",
        type=str,
        default=os.environ.get("PARAZETTEL_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        help="Port to bind when using SSE transport",
        type=int,
        default=int(os.environ.get("PARAZETTEL_MCP_PORT", "8765")),
    )
    parser.add_argument(
        "--backend-mode",
        help="Backend mode for MCP tool execution",
        choices=["direct", "daemon"],
        default=os.environ.get("PARAZETTEL_BACKEND_MODE", "direct"),
    )
    parser.add_argument(
        "--run-daemon",
        help="Run the local Parazettel daemon instead of the MCP facade",
        action="store_true",
    )
    parser.add_argument(
        "--daemon-host",
        help="Host to bind the local Parazettel daemon",
        type=str,
        default=os.environ.get("PARAZETTEL_DAEMON_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--daemon-port",
        help="Port to bind the local Parazettel daemon",
        type=int,
        default=int(os.environ.get("PARAZETTEL_DAEMON_PORT", "8766")),
    )
    return parser.parse_args()


def update_config(args):
    """Update the global config with command line arguments."""
    if args.notes_dir:
        config.notes_dir = Path(args.notes_dir)
    if args.graph_db_path:
        config.graph_db_path = Path(args.graph_db_path)
    elif getattr(args, "database_path", None):
        legacy_path = Path(args.database_path)
        if legacy_path.suffix == ".db":
            config.graph_db_path = legacy_path.with_name("graph.kuzu")
        else:
            config.graph_db_path = legacy_path
    config.server_transport = args.transport
    config.server_host = args.host
    config.server_port = args.port
    config.backend_mode = args.backend_mode
    config.daemon_host = args.daemon_host
    config.daemon_port = args.daemon_port


def main():
    """Run the Zettelkasten MCP server."""
    args = parse_args()
    update_config(args)

    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # Ensure directories exist
    notes_dir = config.get_absolute_path(config.notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    graph_db_path = config.get_graph_db_path()
    logger.info(f"Using Kuzu graph database: {graph_db_path}")

    # Create and run the daemon or MCP facade
    daemon = None
    server = None
    try:
        if args.run_daemon:
            logger.info("Starting Parazettel daemon at %s", config.get_daemon_base_url())
            daemon = ParazettelDaemonServer(
                config.daemon_host,
                config.daemon_port,
            )
            daemon.serve_forever()
        else:
            logger.info("Starting Zettelkasten MCP server")
            server = ZettelkastenMcpServer()
            server.run(config.server_transport)
    except Exception as e:
        logger.error(f"Error running server: {e}")
        sys.exit(1)
    finally:
        if daemon is not None:
            daemon.shutdown()
        if server is not None:
            server.close()


if __name__ == "__main__":
    main()
