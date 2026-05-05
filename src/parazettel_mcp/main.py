#!/usr/bin/env python
"""Main entry point for the Zettelkasten MCP server."""
import argparse
import logging
import os
import sys
from pathlib import Path

from parazettel_mcp.config import config
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
        "--log-level",
        help="Logging level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=os.environ.get("PARAZETTEL_LOG_LEVEL", "INFO"),
    )
    return parser.parse_args()


def update_config(args):
    """Update the global config with command line arguments."""
    if args.notes_dir:
        config.notes_dir = Path(args.notes_dir)
    if args.graph_db_path:
        config.graph_db_path = Path(args.graph_db_path)


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

    # Create and run the MCP server
    server = None
    try:
        logger.info("Starting Zettelkasten MCP server")
        server = ZettelkastenMcpServer()
        server.run()
    except Exception as e:
        logger.error(f"Error running server: {e}")
        sys.exit(1)
    finally:
        if server is not None:
            server.close()


if __name__ == "__main__":
    main()
