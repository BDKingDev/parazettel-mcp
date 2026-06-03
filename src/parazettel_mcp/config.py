"""Configuration module for the Zettelkasten MCP server."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from parazettel_mcp import get_version

# Load caller-specific .env first, then fill any missing values from the repo-root .env.
load_dotenv()
_REPO_ENV_PATH = Path(__file__).parent.parent.parent / ".env"
if _REPO_ENV_PATH.exists():
    load_dotenv(_REPO_ENV_PATH, override=False)


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
    # Graph database configuration (Kuzu database file path)
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
    server_version: str = Field(default_factory=get_version)
    server_transport: str = Field(
        default=os.getenv("PARAZETTEL_MCP_TRANSPORT", "stdio")
    )
    server_host: str = Field(
        default=os.getenv("PARAZETTEL_MCP_HOST", "127.0.0.1")
    )
    server_port: int = Field(
        default=int(os.getenv("PARAZETTEL_MCP_PORT", "8765"))
    )
    backend_mode: str = Field(
        default=os.getenv("PARAZETTEL_BACKEND_MODE", "direct")
    )
    daemon_host: str = Field(
        default=os.getenv("PARAZETTEL_DAEMON_HOST", "127.0.0.1")
    )
    daemon_port: int = Field(
        default=int(os.getenv("PARAZETTEL_DAEMON_PORT", "8766"))
    )
    daemon_rpc_timeout_seconds: float = Field(
        default=float(os.getenv("PARAZETTEL_DAEMON_RPC_TIMEOUT_SECONDS", "300"))
    )
    daemon_idle_timeout_seconds: float = Field(
        default=float(os.getenv("PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS", "0"))
    )
    daemon_runtime_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "PARAZETTEL_DAEMON_RUNTIME_DIR",
                str(Path(tempfile.gettempdir()) / "parazettel-daemon"),
            )
        )
    )
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
    # Embedding / semantic search configuration (disabled by default).
    # When enabled, note bodies are embedded into a dense vector stored on the
    # Note node and indexed with Kuzu's native HNSW vector index, enabling
    # hybrid (BM25 + semantic) retrieval. See services/embedding_provider.py.
    embedding_enabled: bool = Field(
        default_factory=lambda: os.getenv("PARAZETTEL_EMBEDDING_ENABLED", "false")
        .strip()
        .lower()
        in ("1", "true", "yes", "on")
    )
    # Provider that turns text into vectors:
    #   "fastembed" (lite, default) – ONNX, no PyTorch; [embeddings-lite] extra
    #   "sentence-transformers"     – PyTorch; widest model support; [embeddings]
    #   "hash"                      – deterministic, dependency-free; tests only
    embedding_provider: str = Field(
        default=os.getenv("PARAZETTEL_EMBEDDING_PROVIDER", "fastembed")
    )
    # Model identifier passed to the provider. Default is a small on-device model;
    # for the full tier use e.g. "google/embeddinggemma-300m" (dim 768).
    embedding_model: str = Field(
        default=os.getenv("PARAZETTEL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    )
    # Output dimensionality. Must match the model (Matryoshka models may be
    # truncated to a supported smaller dimension). bge-small=384, EmbeddingGemma=768.
    embedding_dim: int = Field(
        default=int(os.getenv("PARAZETTEL_EMBEDDING_DIM", "384"))
    )
    # Distance metric for the HNSW index: "cosine", "l2", or "dotproduct".
    embedding_metric: str = Field(
        default=os.getenv("PARAZETTEL_EMBEDDING_METRIC", "cosine")
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

    def get_daemon_base_url(self) -> str:
        """Get the localhost HTTP base URL for the Parazettel daemon."""
        return f"http://{self.daemon_host}:{self.daemon_port}"

    def get_daemon_runtime_dir(self) -> Path:
        """Get the runtime directory used for daemon metadata like PID files."""
        runtime_dir = self.get_absolute_path(self.daemon_runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir

    def get_daemon_pid_file(self) -> Path:
        """Get the PID file path for the managed local daemon."""
        return self.get_daemon_runtime_dir() / "daemon.pid"

    def format_daemon_start_command(self) -> str:
        """Render an absolute, copy-pasteable command that starts the daemon.

        Uses the current interpreter and absolute vault paths so the command
        works from any working directory (e.g. another repo where notes are
        being created), not just the install directory.

        Deliberately uses ``sys.executable`` (python.exe), not pythonw.exe: this
        command is meant for a human to run manually, where seeing startup logs
        and errors (e.g. a Kuzu lock conflict) matters.
        """
        parts = [
            sys.executable,
            "-m",
            "parazettel_mcp.main",
            "--run-daemon",
            "--notes-dir",
            str(self.get_absolute_path(self.notes_dir)),
            "--graph-db-path",
            str(self.get_absolute_path(self.graph_db_path)),
            "--daemon-host",
            self.daemon_host,
            "--daemon-port",
            str(self.daemon_port),
        ]
        return " ".join(f'"{part}"' if " " in part else part for part in parts)


# Create a global config instance
config = ZettelkastenConfig()
