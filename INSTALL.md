# Installation Guide

Step-by-step installation for Parazettel MCP, including both direct usage and the shared daemon setup (which keeps the embedding model warm across chats).

## Prerequisites

- Python 3.11+ whose bundled SQLite includes FTS5 (true of uv-managed and most standard builds); the setup below uses 3.13
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

- `direct`: the MCP process opens the SQLite index itself. Safe for multiple chat windows: every process can read and write, and a cross-process write lock (`<index>.write.lock`) serializes writes.
- `daemon`: one long-lived local daemon serves the index, and MCP clients proxy through it. Not required for correctness, but it keeps the embedding model and reranker warm, so it is the recommended mode when embeddings are enabled.

## Step 3: Set Environment Variables

At minimum, configure:

```bash
export PARAZETTEL_NOTES_DIR="/absolute/path/to/notes"
export PARAZETTEL_INDEX_DB_PATH="/absolute/path/to/data/db/index.sqlite3"
export PARAZETTEL_LOG_LEVEL="INFO"
```

For shared-daemon mode, also configure:

```bash
export PARAZETTEL_BACKEND_MODE="daemon"
export PARAZETTEL_DAEMON_HOST="127.0.0.1"
export PARAZETTEL_DAEMON_PORT="8766"
```

The daemon auto-shuts down after an idle period (default 3600s); override it with
`--daemon-idle-timeout <seconds>` at startup (`0` keeps it always-on).

Optional tuning:

- `PARAZETTEL_INDEX_BACKUP_KEEP`: timestamped pre-rebuild index backups to keep (default 5)
- `PARAZETTEL_SQLITE_BUSY_TIMEOUT_MS`: how long to wait on a locked index (default 10000)
- `PARAZETTEL_DEDUP_MIN_BM25`: lexical dedup-on-create floor (default 1.5)

Legacy compatibility:

- `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path` are deprecated but still honoured. When no index path is set, a `*.kuzu` value places the index at a sibling `index.sqlite3` (never the Kuzu file itself); any other value is used as-is.
- `PARAZETTEL_DATABASE_PATH` is still accepted for older launchers. If it points to `something.db`, Parazettel maps it to a sibling `graph.kuzu` path, which in turn places the index at a sibling `index.sqlite3`.
- `PARAZETTEL_KUZU_BUFFER_POOL_BYTES` is no longer used; it is ignored with a warning.

## Step 4: Create the Notes Directory

```bash
mkdir -p /absolute/path/to/notes
mkdir -p /absolute/path/to/data/db
```

Parazettel stores:

- Markdown note files in `PARAZETTEL_NOTES_DIR`
- SQLite index at `PARAZETTEL_INDEX_DB_PATH` (default `data/db/index.sqlite3`), rebuildable from the markdown files at any time

## Step 5: Register the MCP Server

### Direct mode

Use this for simple local setups (multiple chat windows are fine):

```json
{
  "mcpServers": {
    "parazettel": {
      "command": "/absolute/path/to/parazettel-mcp/.venv/bin/python",
      "args": ["-m", "parazettel_mcp.main"],
      "env": {
        "PARAZETTEL_NOTES_DIR": "/absolute/path/to/notes",
        "PARAZETTEL_INDEX_DB_PATH": "/absolute/path/to/data/db/index.sqlite3",
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
  --index-db-path /absolute/path/to/data/db/index.sqlite3 \
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

This is the recommended setup when embeddings are enabled, so each chat doesn't load its own copy of the model.

Daemon lifecycle commands:

```bash
# Check whether the managed daemon is healthy and which PID it owns
python -m parazettel_mcp.main --daemon-status

# Stop the managed daemon cleanly
python -m parazettel_mcp.main --stop-daemon
```

The daemon shuts itself down after an idle period (default 3600s), which is safer than tying shutdown directly to one editor session closing. Override the window with `--daemon-idle-timeout <seconds>` at startup (pass `0` to keep it always-on).

## Step 6: Verify Installation

Start your MCP client and verify the server is visible. Parazettel currently exposes `31` tools.

Then run a simple smoke test such as creating an area note or fetching an existing note.

## Step 7: Vaults from a Kuzu-based release

New vaults need no migration: the index is created on first start. The one-time
Kuzu → SQLite migration script has been retired. If you still have a Kuzu vault
to convert, recover the script from history (this finds the commit that deleted
it, so it works whatever the commit hashes are):

```bash
git show "$(git rev-list -n 1 HEAD -- scripts/migrate_kuzu_to_sqlite.py)^:scripts/migrate_kuzu_to_sqlite.py" > migrate_kuzu_to_sqlite.py
pip install "kuzu>=0.11.3,<0.12"
```

## Troubleshooting

### `Parazettel daemon is unavailable`

The MCP facade is configured for daemon mode but could not reach or auto-start the daemon. Check host/port settings and whether another local process is already occupying the daemon port. `python -m parazettel_mcp.main --daemon-status` is the fastest way to verify the current daemon state.

### `Timed out ... waiting for ... index.sqlite3.write.lock`

Writes from every process queue on the cross-process write lock; a write waits up to 10 minutes for another process's write (usually a long rebuild that is re-embedding on CPU) before giving up. Retry once the other write finishes. The lock is released automatically when the holding process exits. (Plain SQLite `database is locked` errors are governed by `PARAZETTEL_SQLITE_BUSY_TIMEOUT_MS`, default 10000.)

### Old launcher still passes `--graph-db-path` or `--database-path`

That is still supported, but you should migrate config to `PARAZETTEL_INDEX_DB_PATH` / `--index-db-path`.

## Next Reading

- [README.md](README.md)
- [QUICKSTART.md](QUICKSTART.md)
- [docs/mcp-testing-guide.md](docs/mcp-testing-guide.md)
