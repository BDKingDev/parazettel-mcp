"""Configuration module for the Zettelkasten MCP server."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()


class ZettelkastenConfig(BaseModel):
    """Configuration for the Zettelkasten server."""

    # Base directory for the project
    base_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("PARAZETTEL_BASE_DIR", "."))
    )
    # Storage configuration
    notes_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("PARAZETTEL_NOTES_DIR", "data/notes"))
    )
    # Graph database configuration (Kuzu directory path)
    graph_db_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv("PARAZETTEL_GRAPH_DB_PATH", "data/db/graph.kuzu")
        )
    )
    # Legacy SQLite path – kept for backward compatibility and migration tooling only
    database_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv("PARAZETTEL_DATABASE_PATH", "data/db/parazettel.db")
        )
    )
    # Server configuration
    server_name: str = Field(
        default=os.getenv("PARAZETTEL_SERVER_NAME", "parazettel")
    )
    server_version: str = Field(default="0.5.0")
    # Date format for ID generation (using ISO format for timestamps)
    id_date_format: str = Field(default="%Y%m%dT%H%M%S")
    # Default note template
    default_note_template: str = Field(
        default=(
            "# {title}\n\n"
            "## Metadata\n"
            "- Created: {created_at}\n"
            "- Tags: {tags}\n\n"
            "## Content\n\n"
            "{content}\n\n"
            "## Links\n"
            "{links}\n"
        )
    )

    def get_absolute_path(self, path: Path) -> Path:
        """Convert a relative path to an absolute path based on base_dir."""
        if path.is_absolute():
            return path
        return self.base_dir / path

    def get_graph_db_path(self) -> Path:
        """Get the absolute path for the Kuzu graph database file.

        The parent directory is created if it does not exist.  The path itself
        must *not* be pre-created; Kuzu manages its own file structure.
        """
        graph_db_path = self.get_absolute_path(self.graph_db_path)
        graph_db_path.parent.mkdir(parents=True, exist_ok=True)
        return graph_db_path

    def get_db_url(self) -> str:
        """Get the legacy SQLite database URL (used by migration tooling only)."""
        db_path = self.get_absolute_path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"


# Create a global config instance
config = ZettelkastenConfig()
