# Installation Guide

Step-by-step installation for Parazettel MCP, including both direct single-chat usage and the shared daemon setup recommended for multi-chat use.

## Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv) recommended
- Claude Code or another MCP client
- Git

## Step 1: Install the Package

```bash
git clone https://github.com/BDKingDev/parazettel-mcp.git
cd parazettel-mcp

uv venv --python 3.13
uv sync --extra dev
```

Windows PowerShell uses:

```powershell
.\.venv\Scripts\python.exe --version
```

## Step 2: Choose a Runtime Mode

Parazettel now has two runtime modes:

- `direct`: the MCP process opens the graph DB itself. Fine for tests and single-chat use.
- `daemon`: one long-lived local daemon owns the graph DB, and MCP clients proxy through it. This is the recommended mode for multiple chat windows.

## Step 3: Set Environment Variables

At minimum, configure:

```bash
export PARAZETTEL_NOTES_DIR="/absolute/path/to/notes"
export PARAZETTEL_GRAPH_DB_PATH="/absolute/path/to/data/db/graph.kuzu"
export PARAZETTEL_LOG_LEVEL="INFO"
```

For shared-daemon mode, also configure:

```bash
export PARAZETTEL_BACKEND_MODE="daemon"
export PARAZETTEL_DAEMON_HOST="127.0.0.1"
export PARAZETTEL_DAEMON_PORT="8766"
export PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS="1800"
```

Legacy compatibility:

- `PARAZETTEL_DATABASE_PATH` is still accepted for older launchers.
- If it points to `something.db`, Parazettel maps it to a sibling `graph.kuzu` path.

## Step 4: Create the Notes Directory

```bash
mkdir -p /absolute/path/to/notes
mkdir -p /absolute/path/to/data/db
```

Parazettel stores:

- Markdown note files in `PARAZETTEL_NOTES_DIR`
- Kuzu graph index at `PARAZETTEL_GRAPH_DB_PATH`

## Step 5: Register the MCP Server

### Direct mode

Use this for a single chat window or simple local development:

```json
{
  "mcpServers": {
    "parazettel": {
      "command": "/absolute/path/to/parazettel-mcp/.venv/bin/python",
      "args": ["-m", "parazettel_mcp.main"],
      "env": {
        "PARAZETTEL_NOTES_DIR": "/absolute/path/to/notes",
        "PARAZETTEL_GRAPH_DB_PATH": "/absolute/path/to/data/db/graph.kuzu",
        "PARAZETTEL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Shared daemon mode

By default, daemon-backed MCP launches now auto-start the local daemon if it is not already running. Manual startup is only needed if you want explicit process control.

1. Optional manual daemon startup:

```bash
python -m parazettel_mcp.main \
  --run-daemon \
  --notes-dir /absolute/path/to/notes \
  --graph-db-path /absolute/path/to/data/db/graph.kuzu \
  --daemon-host 127.0.0.1 \
  --daemon-port 8766
```

2. Point MCP clients at it:

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

This is the recommended setup when you want multiple chat windows to use Parazettel at the same time.

Daemon lifecycle commands:

```bash
# Check whether the managed daemon is healthy and which PID it owns
python -m parazettel_mcp.main --daemon-status

# Stop the managed daemon cleanly
python -m parazettel_mcp.main --stop-daemon
```

If you want the daemon to shut itself down when it has been unused for a while, keep `PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS` set to a non-zero value. That is safer than tying shutdown directly to one editor session closing.

## Step 6: Verify Installation

Start your MCP client and verify the server is visible. Parazettel currently exposes `30` tools.

Then run a simple smoke test such as creating an area note or fetching an existing note.

## Step 7: Optional Migration from SQLite

If you are migrating an older vault:

```bash
python scripts/migrate_to_graphdb.py \
  --notes-dir /absolute/path/to/notes \
  --graph-db-path /absolute/path/to/data/db/graph.kuzu
```

Notes:

- rerunning against an existing graph target updates/imports current notes
- reruns do not prune stale nodes for deleted markdown files
- use a fresh graph target for a clean one-shot migration

## Troubleshooting

### MCP server starts but a second chat cannot write

You are probably still running in `direct` mode. Switch to shared daemon mode so one process owns `graph.kuzu`.

### `Parazettel daemon is unavailable`

The MCP facade is configured for daemon mode but could not reach or auto-start the daemon. Check host/port settings and whether another local process is already occupying the daemon port. `python -m parazettel_mcp.main --daemon-status` is the fastest way to verify the current daemon state.

### `Could not set lock on file ... graph.kuzu`

That means two direct processes tried to open the same graph DB. Shared daemon mode is the correct fix.

### Old launcher still passes `--database-path`

That is still supported, but you should migrate config to `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path`.

## Next Reading

- [README.md](README.md)
- [QUICKSTART.md](QUICKSTART.md)
- [docs/mcp-testing-guide.md](docs/mcp-testing-guide.md)
