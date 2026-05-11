# Quick Start Guide

Get Parazettel running quickly, with the recommended shared-daemon setup for multi-chat use.

## 1. Install

```bash
git clone https://github.com/BDKingDev/parazettel-mcp.git
cd parazettel-mcp

uv venv --python 3.13
uv sync --extra dev
```

## 2. Choose Your Mode

- **Single chat / local dev:** use `direct`
- **Multiple chats at once:** use `daemon` (recommended)

## 3. Set Paths

```bash
export PARAZETTEL_NOTES_DIR="$HOME/Documents/zettelkasten"
export PARAZETTEL_GRAPH_DB_PATH="$HOME/Documents/zettelkasten-db/graph.kuzu"
export PARAZETTEL_LOG_LEVEL="INFO"
```

For shared daemon mode:

```bash
export PARAZETTEL_BACKEND_MODE="daemon"
export PARAZETTEL_DAEMON_HOST="127.0.0.1"
export PARAZETTEL_DAEMON_PORT="8766"
export PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS="1800"
```

## 4. Start the Shared Daemon

If you want multiple chat windows, daemon mode is recommended. In normal use the MCP facade now auto-starts the daemon if needed, so this manual startup is optional:

```bash
python -m parazettel_mcp.main \
  --run-daemon \
  --notes-dir "$PARAZETTEL_NOTES_DIR" \
  --graph-db-path "$PARAZETTEL_GRAPH_DB_PATH" \
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
        "PARAZETTEL_GRAPH_DB_PATH": "/absolute/path/to/data/db/graph.kuzu",
        "PARAZETTEL_BACKEND_MODE": "daemon",
        "PARAZETTEL_DAEMON_HOST": "127.0.0.1",
        "PARAZETTEL_DAEMON_PORT": "8766",
        "PARAZETTEL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

For single-chat direct mode, omit the daemon variables or set `PARAZETTEL_BACKEND_MODE=direct`.

## 6. Verify

Launch your MCP client and confirm Parazettel is available. The server currently exposes `30` tools.

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

# Migrate an old SQLite-era vault
python scripts/migrate_to_graphdb.py --notes-dir ./data/notes --graph-db-path ./data/db/graph.kuzu
```

## If Something Fails

- `graph.kuzu` lock errors: you are likely using multiple direct processes; use daemon mode.
- daemon unavailable: start the daemon or fix `PARAZETTEL_DAEMON_HOST` / `PARAZETTEL_DAEMON_PORT`. `python -m parazettel_mcp.main --daemon-status` is the quickest check.
- old `.db` paths: switch to `PARAZETTEL_GRAPH_DB_PATH`, though `PARAZETTEL_DATABASE_PATH` is still accepted for compatibility.

## Next Docs

- [README.md](README.md)
- [INSTALL.md](INSTALL.md)
- [docs/mcp-testing-guide.md](docs/mcp-testing-guide.md)
