# Quick Start Guide

Get Parazettel running quickly, with the recommended shared-daemon setup (keeps the embedding model warm across chats).

## 1. Install

```bash
git clone https://github.com/BDKingDev/parazettel-mcp.git
cd parazettel-mcp

uv venv --python 3.13
uv sync --extra dev
```

## 2. Choose Your Mode

- **Simple / local dev:** use `direct` — safe even with several chats open; writes are serialized by a cross-process lock
- **Embeddings enabled, several chats:** use `daemon` (recommended) so the embedding model and reranker load once

## 3. Set Paths

```bash
export PARAZETTEL_NOTES_DIR="$HOME/Documents/zettelkasten"
export PARAZETTEL_INDEX_DB_PATH="$HOME/Documents/zettelkasten-db/index.sqlite3"
export PARAZETTEL_LOG_LEVEL="INFO"
```

For shared daemon mode:

```bash
export PARAZETTEL_BACKEND_MODE="daemon"
export PARAZETTEL_DAEMON_HOST="127.0.0.1"
export PARAZETTEL_DAEMON_PORT="8766"
```

The daemon auto-shuts down after an idle period (default 3600s); override it with
`--daemon-idle-timeout <seconds>` at startup (`0` keeps it always-on).

## 4. Start the Shared Daemon

Daemon mode is recommended when embeddings are enabled. In normal use the MCP facade now auto-starts the daemon if needed, so this manual startup is optional:

```bash
python -m parazettel_mcp.main \
  --run-daemon \
  --notes-dir "$PARAZETTEL_NOTES_DIR" \
  --index-db-path "$PARAZETTEL_INDEX_DB_PATH" \
  --daemon-host 127.0.0.1 \
  --daemon-port 8766
```

If you only want a single direct MCP process, skip this step.

## 5. Register the MCP Server

Add to `~/.claude.json` or your MCP client config:

```json
{
  "mcpServers": {
    "parazettel": {
      "command": "/absolute/path/to/parazettel-mcp/.venv/bin/python",
      "args": ["-m", "parazettel_mcp.main"],
      "env": {
        "PARAZETTEL_NOTES_DIR": "/absolute/path/to/notes",
        "PARAZETTEL_INDEX_DB_PATH": "/absolute/path/to/data/db/index.sqlite3",
        "PARAZETTEL_BACKEND_MODE": "daemon",
        "PARAZETTEL_DAEMON_HOST": "127.0.0.1",
        "PARAZETTEL_DAEMON_PORT": "8766",
        "PARAZETTEL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

For direct mode, omit the daemon variables or set `PARAZETTEL_BACKEND_MODE=direct`.

## 6. Verify

Launch your MCP client and confirm Parazettel is available. The server currently exposes `31` tools.

Try one or two real calls:

- create an area
- create a note routed to that area
- search for it

## Common Commands

```bash
# Start daemon
python -m parazettel_mcp.main --run-daemon --daemon-host 127.0.0.1 --daemon-port 8766

# Run direct MCP process manually
python -m parazettel_mcp.main --backend-mode direct

# Run daemon-backed MCP process manually
python -m parazettel_mcp.main --backend-mode daemon --daemon-host 127.0.0.1 --daemon-port 8766

# Check daemon status
python -m parazettel_mcp.main --daemon-status

# Stop the managed daemon
python -m parazettel_mcp.main --stop-daemon

```

## If Something Fails

- write-lock timeout (`... .write.lock`): another process is mid-write (usually a long rebuild); retry once it finishes.
- daemon unavailable: start the daemon or fix `PARAZETTEL_DAEMON_HOST` / `PARAZETTEL_DAEMON_PORT`. `python -m parazettel_mcp.main --daemon-status` is the quickest check.
- old `graph.kuzu` or `.db` paths: switch to `PARAZETTEL_INDEX_DB_PATH`, though `PARAZETTEL_GRAPH_DB_PATH` (a `*.kuzu` value places the index at a sibling `index.sqlite3`) and `PARAZETTEL_DATABASE_PATH` are still accepted for compatibility. Existing Kuzu vaults: see INSTALL.md, Step 7.

## Next Docs

- [README.md](README.md)
- [INSTALL.md](INSTALL.md)
- [docs/mcp-testing-guide.md](docs/mcp-testing-guide.md)
