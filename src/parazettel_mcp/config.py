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


# --- Resource-tuning defaults ---
# Max Kuzu buffer-pool size in bytes. The buffer pool is a lazy page cache: under
# sustained query load (many vector searches paging the DB + HNSW index in) it
# grows toward this cap and stays resident. Kuzu's own default (selected by 0) is
# ~80% of physical RAM *per database instance*, which on a long-lived daemon
# balloons commit charge to tens of GB and inflates the Windows pagefile. So we
# cap it at a bounded default and expose PARAZETTEL_KUZU_BUFFER_POOL_BYTES to tune
# it (0 restores Kuzu's ~80%-RAM default). The test suite bounds it further via
# the conftest fixture so per-test databases stay tiny.
# Kuzu buffer-pool cap. Kept well below the old 8 GiB: the pool is *committed*
# lazily up to this cap, so it dominates the daemon's pagefile/commit charge —
# 3 GiB keeps that bounded while still caching plenty for a typical vault. Raise
# it (PARAZETTEL_KUZU_BUFFER_POOL_BYTES) for a very large vault that thrashes.
DEFAULT_KUZU_BUFFER_POOL_BYTES = 3 * 1024**3  # 3 GiB
# Working-set ceiling that triggers a daemon RECYCLE: when the resident set grows
# past this AND the daemon is idle with no request in flight, it shuts down (a
# fresh one is auto-started on the next request). This bounds Kuzu 0.11.3's
# per-vector-query native leak (which no Python-side cleanup reclaims, and which
# has no upstream fix — Kuzu is archived). 0 disables. Set above the legitimate
# peak (a full rebuild transiently holds the live + temp DB at ~4 GiB) so only a
# genuine leak trips it — the monitor also never recycles while a request runs.
DEFAULT_DAEMON_MAX_RSS_BYTES = 6 * 1024**3  # 6 GiB
# Seconds of inactivity after which the daemon shuts itself down (a fresh one is
# auto-started on the next request). A non-zero default means a daemon left
# behind when an MCP client exits without reaping it reaps itself instead of
# holding the Kuzu DB and embedding model forever.
DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS = 3600.0
# Once the daemon is over the RSS ceiling, wait for at least this many seconds of
# inactivity before recycling, so an in-flight request is never cut off.
DEFAULT_DAEMON_MEMORY_RECYCLE_IDLE_GRACE_SECONDS = 20.0

# --- Dedup-on-create reranker defaults (code constants, not env-configurable) ---
# Cross-encoder that confirms BM25 dedup candidates: it reads both notes together
# and is far more precise than BM25 alone, which over-flags on shared vocabulary
# (true duplicates scored ~7-9, distinct-but-adjacent notes <~1 in testing). The
# 80 MB ms-marco model is lite-tier-friendly. Empty string disables the confirm.
DEFAULT_DEDUP_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
# Minimum cross-encoder score for a candidate to count as a duplicate. Re-derive
# if the model changes.
DEFAULT_DEDUP_RERANK_MIN_SCORE = 3.0
# Max distinct terms kept in a text FTS query. A long query with many moderately
# common terms makes Kuzu's FTS match most of the corpus and drop even the best
# match; reducing to the K lowest-DF (most discriminative) terms bounds the match
# set so the best lexical hit stays findable. Only long queries are reduced.
DEFAULT_FTS_MAX_QUERY_TERMS = 12
# Execution device for the dedup reranker. Defaults to CPU and is INTENTIONALLY
# decoupled from embedding_device: the reranker is loaded per-session in each MCP
# facade, so on CUDA every concurrent (and every orphaned) session stacks another
# cross-encoder onto the GPU — exhausting an 8 GB card and flapping the embedding
# path when a new agent spins up. The model is tiny (~80 MB) and only scores <=5
# short pairs per dedup, so CPU is plenty. Override with PARAZETTEL_DEDUP_RERANK_DEVICE.
DEFAULT_DEDUP_RERANK_DEVICE = "cpu"
# Hard ceiling on the cross-encoder's first-use model LOAD (the one unbounded step
# in the facade — it acquires a filelock on the shared fastembed/HF model cache,
# which can wedge if a prior process died mid-load). A warm load is ~2s; a cold
# GPU init with a download is well under this. Exceeding it means the load is
# genuinely stuck, so we surface a loud, actionable error instead of hanging the
# session forever. Override with PARAZETTEL_DEDUP_RERANK_LOAD_TIMEOUT_SECONDS.
DEFAULT_DEDUP_RERANK_LOAD_TIMEOUT_SECONDS = 45.0


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
    # Idle shutdown timeout (see DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS). Overridable
    # per-launch via the --daemon-idle-timeout CLI flag; 0 keeps it always-on.
    daemon_idle_timeout_seconds: float = Field(
        default=DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
    )
    # Recycle the daemon once its resident set passes this (see
    # DEFAULT_DAEMON_MAX_RSS_BYTES); 0 disables. Bounds the Kuzu vector-query leak.
    daemon_max_rss_bytes: int = Field(
        default=int(
            os.getenv(
                "PARAZETTEL_DAEMON_MAX_RSS_BYTES",
                str(DEFAULT_DAEMON_MAX_RSS_BYTES),
            )
        )
    )
    daemon_memory_recycle_idle_grace_seconds: float = Field(
        default=float(
            os.getenv(
                "PARAZETTEL_DAEMON_MEMORY_RECYCLE_IDLE_GRACE_SECONDS",
                str(DEFAULT_DAEMON_MEMORY_RECYCLE_IDLE_GRACE_SECONDS),
            )
        )
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
    # Normalized (stripped/lowercased) so env values like "COSINE" are accepted.
    embedding_metric: str = Field(
        default_factory=lambda: os.getenv("PARAZETTEL_EMBEDDING_METRIC", "cosine")
        .strip()
        .lower()
    )
    # Batch size for bulk embedding (e.g. during a rebuild). Kept small so the
    # transformer attention tensor (batch x heads x seq^2) stays bounded — large
    # models OOM at the embedding library's default (256 -> ~4GB for mxbai@512).
    embedding_batch_size: int = Field(
        default_factory=lambda: int(os.getenv("PARAZETTEL_EMBEDDING_BATCH_SIZE", "16"))
    )
    # Execution device for local embedding inference: "cpu" (default) or "cuda".
    # "cuda" requires the GPU install extra (fastembed-gpu/onnxruntime-gpu via
    # [embeddings-lite-gpu]; a CUDA build of torch for sentence-transformers) — the
    # provider then selects the CUDA execution provider and preloads its runtime
    # DLLs. Falls back to CPU within the provider if the GPU runtime is missing.
    embedding_device: str = Field(
        default_factory=lambda: os.getenv("PARAZETTEL_EMBEDDING_DEVICE", "cpu")
        .strip()
        .lower()
    )
    # Max Kuzu buffer-pool size in bytes (see DEFAULT_KUZU_BUFFER_POOL_BYTES).
    # PARAZETTEL_KUZU_BUFFER_POOL_BYTES overrides it (0 = Kuzu's ~80%-RAM default).
    kuzu_buffer_pool_bytes: int = Field(
        default=int(
            os.getenv(
                "PARAZETTEL_KUZU_BUFFER_POOL_BYTES",
                str(DEFAULT_KUZU_BUFFER_POOL_BYTES),
            )
        )
    )
    # Dedup-on-create cross-encoder reranker (see DEFAULT_DEDUP_RERANK_MODEL). Only
    # active when embeddings are enabled; empty string disables the rerank confirm
    # (dedup falls back to the BM25 prefilter alone).
    dedup_rerank_model: str = Field(default=DEFAULT_DEDUP_RERANK_MODEL)
    dedup_rerank_min_score: float = Field(default=DEFAULT_DEDUP_RERANK_MIN_SCORE)
    # Reduce a long text FTS query to its K most discriminative (lowest-DF) terms
    # so it doesn't match most of the corpus and drop its best lexical hit. 0
    # disables reduction. See DEFAULT_FTS_MAX_QUERY_TERMS.
    fts_max_query_terms: int = Field(
        default=int(
            os.getenv(
                "PARAZETTEL_FTS_MAX_QUERY_TERMS",
                str(DEFAULT_FTS_MAX_QUERY_TERMS),
            )
        )
    )
    # Timeout on the reranker's first-use model load (see
    # DEFAULT_DEDUP_RERANK_LOAD_TIMEOUT_SECONDS). On timeout the dedup probe raises
    # rather than hanging, so note creation fails loudly instead of silently.
    dedup_rerank_load_timeout_seconds: float = Field(
        default=float(
            os.getenv(
                "PARAZETTEL_DEDUP_RERANK_LOAD_TIMEOUT_SECONDS",
                str(DEFAULT_DEDUP_RERANK_LOAD_TIMEOUT_SECONDS),
            )
        )
    )
    # Reranker execution device (see DEFAULT_DEDUP_RERANK_DEVICE). Decoupled from
    # embedding_device so the dedup cross-encoder defaults to CPU and leaves the
    # GPU to the embedder (the reranker is tiny — it runs only on the <=5 BM25
    # candidates). PARAZETTEL_DEDUP_RERANK_DEVICE overrides it (e.g. 'cuda').
    dedup_rerank_device: str = Field(
        default_factory=lambda: os.getenv(
            "PARAZETTEL_DEDUP_RERANK_DEVICE", DEFAULT_DEDUP_RERANK_DEVICE
        )
        .strip()
        .lower()
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

    def format_daemon_restart_command(self) -> str:
        """Render the recommended daemon (re)start command for a down daemon.

        Prefers the repo's restart script, which preserves the embedding
        environment so semantic search comes back up — the raw ``--run-daemon``
        command does not, and following it would silently start the daemon
        without embeddings. Falls back to that raw command when the script is
        absent (e.g. a non-editable install). Absolute path so it is
        copy-pasteable from any working directory.
        """
        repo_root = Path(__file__).resolve().parents[2]
        script_name = "restart_daemon.ps1" if os.name == "nt" else "restart_daemon.sh"
        script = repo_root / "scripts" / script_name
        if script.is_file():
            runner = "pwsh" if os.name == "nt" else "bash"
            script_str = str(script)
            quoted = f'"{script_str}"' if " " in script_str else script_str
            return f"{runner} {quoted}"
        return self.format_daemon_start_command()


# Create a global config instance
config = ZettelkastenConfig()
