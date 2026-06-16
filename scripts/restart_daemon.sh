#!/usr/bin/env bash
# Restart the Parazettel daemon cleanly with the correct embedding environment.
#
# Stops any running daemon and starts a fresh detached one bound to THIS repo's
# vault. Because the package is installed editable, the new daemon picks up the
# latest code (and new graph columns are added idempotently on open), so this is
# the right way to apply code changes to a running daemon.
#
# Embedding settings default to this vault's GPU config; any PARAZETTEL_* /
# FASTEMBED_* value already exported takes precedence. Edit the defaults below if
# your vault uses a different model/device.
#
# Usage: bash scripts/restart_daemon.sh
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$repo/.venv/Scripts/python.exe"          # Windows venv layout
[ -x "$py" ] || py="$repo/.venv/bin/python"  # POSIX venv layout
[ -x "$py" ] || py="$(command -v python || true)"
if [ -z "$py" ]; then
  echo "Python not found: no interpreter in .venv and none on PATH." >&2
  echo "Create the venv (uv venv --python 3.13) or activate one first." >&2
  exit 1
fi

: "${PARAZETTEL_NOTES_DIR:=$repo/data/notes}"
: "${PARAZETTEL_GRAPH_DB_PATH:=$repo/data/db/graph.kuzu}"
: "${PARAZETTEL_BACKEND_MODE:=daemon}"
: "${PARAZETTEL_DAEMON_HOST:=127.0.0.1}"
: "${PARAZETTEL_DAEMON_PORT:=8766}"
: "${PARAZETTEL_EMBEDDING_ENABLED:=true}"
: "${PARAZETTEL_EMBEDDING_PROVIDER:=fastembed}"
: "${PARAZETTEL_EMBEDDING_MODEL:=mixedbread-ai/mxbai-embed-large-v1}"
: "${PARAZETTEL_EMBEDDING_DIM:=1024}"
: "${PARAZETTEL_EMBEDDING_METRIC:=cosine}"
: "${PARAZETTEL_EMBEDDING_DEVICE:=cuda}"
: "${FASTEMBED_CACHE_PATH:=$repo/data/fastembed_cache}"
export PARAZETTEL_NOTES_DIR PARAZETTEL_GRAPH_DB_PATH PARAZETTEL_BACKEND_MODE \
  PARAZETTEL_DAEMON_HOST PARAZETTEL_DAEMON_PORT PARAZETTEL_EMBEDDING_ENABLED \
  PARAZETTEL_EMBEDDING_PROVIDER PARAZETTEL_EMBEDDING_MODEL PARAZETTEL_EMBEDDING_DIM \
  PARAZETTEL_EMBEDDING_METRIC PARAZETTEL_EMBEDDING_DEVICE FASTEMBED_CACHE_PATH

echo "Stopping any running Parazettel daemon..."
"$py" -m parazettel_mcp.main --stop-daemon

echo "Starting a fresh detached daemon (embeddings: $PARAZETTEL_EMBEDDING_MODEL on $PARAZETTEL_EMBEDDING_DEVICE)..."
# ensure_daemon_running spawns a properly detached background daemon and blocks
# only until the health endpoint is ready.
"$py" -c "import argparse; from parazettel_mcp import main as m; m.ensure_daemon_running(argparse.Namespace(log_level='INFO'))"

echo "--- Daemon status ---"
"$py" -m parazettel_mcp.main --daemon-status
