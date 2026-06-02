# Parazettel MCP

A Zettelkasten knowledge management system extended with GTD and PARA workflow support, running as a Model Context Protocol (MCP) server. Fork of [zettelkasten-mcp](https://github.com/entanglr/zettelkasten-mcp).

## What is Parazettel?

Parazettel combines two complementary systems:

**Zettelkasten** — a graph-first knowledge method built on atomic notes, semantic links, and emergent insight. Each note is a discrete idea; meaning grows from the connections between notes.

**GTD + PARA** — a workflow layer on top of the same graph. Tasks, projects, and areas live as first-class note types with statuses, due dates, priorities, energy levels, and GTD contexts. The inbox → ready → active → done lifecycle applies to any note, not just tasks.

The result is one unified vault where knowledge notes and action items share the same ID format, tagging system, and semantic link graph.

---

## Features

- Create atomic notes with unique timestamp-based IDs
- Link notes with typed semantic relationships and inverse-link handling
- Tag notes for categorical organisation
- Search notes by content, tags, type, status, project, area, and more
- Dual storage: Markdown files (source of truth) + Kuzu graph index (fast queries and traversals)
- Graph-backed search, link traversal, and PARA/GTD routing queries
- Knowledge notes must be routed to an `area_id` directly or inherit one from `project_id`
- Tasks with status lifecycle, priorities, energy levels, GTD contexts, and due dates
- Projects linked to areas with optional subprojects via the PARA hierarchy
- Project views include task summaries, routed notes, parent-project context, and direct subprojects
- Today view and reminder surfacing
- Recurring tasks that auto-spawn the next instance on completion
- Obsidian-style wiki-link aliases like `[[id|Title]]` are normalized on read and rebuild

---

## Note Types

| Type | Handle | Description |
| --- | --- | --- |
| Fleeting | `fleeting` | Quick, unprocessed captures |
| Literature | `literature` | Notes from reading material |
| Permanent | `permanent` | Well-formulated, evergreen notes |
| Structure | `structure` | Index/outline notes that organise others |
| Hub | `hub` | Entry points to major topic areas |
| Task | `task` | Atomic actionable item (GTD next action) |
| Project | `project` | Active outcome with a deadline (PARA) |
| Area | `area` | Ongoing responsibility with no end date (PARA) |

## Note Status

Applies to any note type. Tasks flow through the action lifecycle; knowledge notes use `evergreen` and `archived`.

| Status | Meaning |
| --- | --- |
| `inbox` | Captured but not yet triaged |
| `ready` | Triaged and waiting to be picked up |
| `scheduled` | Committed to a specific date |
| `active` | In progress |
| `waiting` | Waiting on someone or something |
| `someday` | Someday/maybe |
| `done` | Completed |
| `cancelled` | Cancelled |
| `archived` | Inactive but retrievable |
| `evergreen` | Mature, stable knowledge note |

## Link Types

### Semantic (knowledge) links

| Primary | Inverse | Relationship |
| --- | --- | --- |
| `reference` | `reference` | Simple reference (symmetric) |
| `extends` | `extended_by` | Builds upon another note |
| `refines` | `refined_by` | Clarifies or improves another |
| `contradicts` | `contradicted_by` | Presents opposing views |
| `questions` | `questioned_by` | Poses questions about another |
| `supports` | `supported_by` | Provides evidence for another |
| `related` | `related` | Generic relationship (symmetric) |

### Structural (PARA/GTD) links

| Primary | Inverse | Relationship |
| --- | --- | --- |
| `part_of` | `has_part` | Task/note belongs to a project or area |
| `blocks` | `blocked_by` | One task blocks another |

Parazettel also understands Obsidian-style piped wiki-links such as `[[NOTE_ID|Displayed Title]]`. On ingest, the target note ID is normalized from the link target, so aliases and heading fragments do not break indexing. When a note is rewritten, touched wiki-links are normalized to the target note title when the alias is safe to render. Renaming a note refreshes incoming title aliases, and deleting a note removes incoming markdown references.

---

## Available MCP Tools

All tools are prefixed `pzk_`.

### Knowledge management

| Tool | Description |
| --- | --- |
| `pzk_create_note` | Create a note with title, content, source, and required area/project routing for non-area notes |
| `pzk_get_note` | Retrieve a note by ID or title |
| `pzk_get_notes` | Retrieve multiple notes by ID or title in one call |
| `pzk_get_notes_by_tag` | Retrieve multiple notes with an exact tag match in one call |
| `pzk_update_note` | Update content, type, tags, and project/area routing |
| `pzk_delete_note` | Delete a note |
| `pzk_create_link` | Create a typed link between notes |
| `pzk_remove_link` | Remove a link |
| `pzk_search_notes` | Search by text, tags, type, status, project_id, or area_id |
| `pzk_get_linked_notes` | Get notes linked to/from a note |
| `pzk_get_all_tags` | List all tags currently applied to a note (reuse these before inventing new ones) |
| `pzk_find_similar_notes` | Find notes similar to a given note (content-aware: title/content overlap plus shared tags/links) |
| `pzk_find_central_notes` | Find most-connected notes |
| `pzk_find_orphaned_notes` | Find notes with no connections |
| `pzk_list_notes_by_date` | List notes by creation or update date |
| `pzk_rebuild_index` | Rebuild the Kuzu graph index from Markdown files |
| `pzk_check_consistency` | Read-only audit of file-vs-index drift (run before deciding to rebuild) |

### Task management

| Tool | Description |
| --- | --- |
| `pzk_create_task` | Create a task with status, due date, priority, energy, context, reminders |
| `pzk_update_task` | Update task fields, status, or project assignment; recurring completion spawns the next instance |
| `pzk_get_tasks` | Query tasks by status, project, due date, priority |
| `pzk_get_todays_tasks` | Tasks due today + overdue, sorted by priority |
| `pzk_get_reminders` | Notes and tasks with `remind_at` ≤ today |

### Project and area management

| Tool | Description |
| --- | --- |
| `pzk_create_area` | Create an area note with optional review cadence |
| `pzk_get_area` | Get an area with linked projects and task counts |
| `pzk_list_areas` | List all areas |
| `pzk_create_project` | Create a top-level project or a subproject with `parent_project_id` |
| `pzk_create_subproject` | Create a subproject under an existing parent project |
| `pzk_list_projects` | List active projects sorted by deadline |
| `pzk_get_project` | Get a project summary with task counts, next tasks, routed notes, parent project, and subprojects |
| `pzk_get_project_notes` | Get full routed note context for a project |
| `pzk_get_project_tasks` | Get all tasks for a project |

---

## Storage Architecture

1. **Markdown files** — source of truth. Human-readable, version-controllable, directly editable. Each note is `{id}.md` with YAML frontmatter.
2. **Kuzu graph database** — indexing and traversal layer. Used for search, link traversal, and PARA/GTD routing queries. Automatically rebuilt from files when needed via `pzk_rebuild_index`.

> **Rebuild safety:** `pzk_rebuild_index` creates a timestamped file snapshot backup of the graph database, then builds a fresh index into a temporary database and atomically swaps it into place (the live index is never cleared in place). Files that fail to parse are reported rather than silently dropped. While rebuild is running, the daemon reports maintenance mode and other RPC calls are rejected until it completes.
>
> **Obsidian aliases:** piped wiki links like `[[20260322T181907454570000|Habits and Habit Creation]]` are normalized to the underlying note ID during reads and index rebuilds.

> **Compatibility:** `PARAZETTEL_DATABASE_PATH` and `--database-path` are still accepted for migration and legacy launchers, but the primary runtime path is `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path`.

---

## Installation

```bash
git clone https://github.com/BDKingDev/parazettel-mcp.git
cd parazettel-mcp

# Install dependencies (creates .venv with Python 3.13)
uv venv --python 3.13
uv sync --extra dev
```

### Connecting to Claude Desktop

For a single chat or development-only direct run, add this to your Claude Desktop MCP configuration:

```json
{
  "mcpServers": {
    "parazettel": {
      "command": "/absolute/path/to/parazettel-mcp/.venv/bin/python",
      "args": ["-m", "parazettel_mcp.main"],
      "env": {
        "PARAZETTEL_NOTES_DIR": "/absolute/path/to/parazettel-mcp/data/notes",
        "PARAZETTEL_GRAPH_DB_PATH": "/absolute/path/to/parazettel-mcp/data/db/graph.kuzu",
        "PARAZETTEL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

For multi-chat use, Parazettel can run one shared local daemon and point stdio MCP launches at that daemon. In normal daemon mode, the MCP facade now auto-starts the daemon if it is missing, so manual startup is optional.

```bash
python -m parazettel_mcp.main \
  --run-daemon \
  --daemon-host 127.0.0.1 \
  --daemon-port 8766
```

Then configure the MCP entry to proxy through it:

```json
{
  "mcpServers": {
    "parazettel": {
      "command": "/absolute/path/to/parazettel-mcp/.venv/bin/python",
      "args": ["-m", "parazettel_mcp.main"],
      "env": {
        "PARAZETTEL_NOTES_DIR": "/absolute/path/to/parazettel-mcp/data/notes",
        "PARAZETTEL_GRAPH_DB_PATH": "/absolute/path/to/parazettel-mcp/data/db/graph.kuzu",
        "PARAZETTEL_BACKEND_MODE": "daemon",
        "PARAZETTEL_DAEMON_HOST": "127.0.0.1",
        "PARAZETTEL_DAEMON_PORT": "8766",
        "PARAZETTEL_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Daemon lifecycle commands:

```bash
# See whether the managed daemon is healthy and which PID it owns
python -m parazettel_mcp.main --daemon-status

# Stop the managed daemon cleanly
python -m parazettel_mcp.main --stop-daemon
```

If you want the daemon to shut itself down after inactivity, set:

- `PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS`
- or `--daemon-idle-timeout`

An idle timeout is the safest approximation to “close the daemon once all sessions are gone,” because one chat closing should not kill the daemon while another chat is still using it.

### Connecting to Claude Code (CLI)

Add the same entry to `~/.claude.json` under `mcpServers`.

---

## Configuration

Copy `.env.example` to `.env` and set paths as needed. The primary runtime storage path is `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path`. Caller or current-working-directory `.env` values are loaded first, and the repo-root `.env` fills in any missing values.

Key runtime settings:

- `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path`: primary Kuzu graph path
- `PARAZETTEL_BACKEND_MODE` / `--backend-mode`: `direct` or `daemon`
- `PARAZETTEL_DAEMON_HOST` / `--daemon-host`: local daemon bind/connect host
- `PARAZETTEL_DAEMON_PORT` / `--daemon-port`: local daemon bind/connect port
- `PARAZETTEL_DAEMON_IDLE_TIMEOUT_SECONDS` / `--daemon-idle-timeout`: optional idle shutdown window for the daemon
- `PARAZETTEL_MCP_TRANSPORT` / `--transport`: MCP transport, `stdio` or `sse`

```bash
python -m parazettel_mcp.main \
  --notes-dir ./data/notes \
  --graph-db-path ./data/db/graph.kuzu \
  --backend-mode direct
```

To run the shared daemon manually:

```bash
python -m parazettel_mcp.main \
  --run-daemon \
  --notes-dir ./data/notes \
  --graph-db-path ./data/db/graph.kuzu \
  --daemon-host 127.0.0.1 \
  --daemon-port 8766
```

To run the MCP facade against that daemon:

```bash
python -m parazettel_mcp.main \
  --backend-mode daemon \
  --daemon-host 127.0.0.1 \
  --daemon-port 8766
```

Legacy launchers can still pass `PARAZETTEL_DATABASE_PATH` or `--database-path`. If a legacy `.db` path is supplied, Parazettel maps it to a sibling `graph.kuzu` path for compatibility.

In daemon mode, stdio MCP launches will first try the daemon health endpoint. If the daemon is not already running, Parazettel auto-starts it in the background and then connects to it. Manual daemon startup is still available when you want explicit process control.

You can inspect or stop the managed daemon at any time:

```bash
python -m parazettel_mcp.main --daemon-status
python -m parazettel_mcp.main --stop-daemon
```

If you are migrating an existing vault from SQLite to Kuzu, use:

```bash
python scripts/migrate_to_graphdb.py \
  --notes-dir ./data/notes \
  --graph-db-path ./data/db/graph.kuzu
```

Rerunning the migration against an existing target updates current notes and links, but it does not prune stale graph nodes for source files that were deleted later. For a clean one-shot migration, use a fresh target path.

---

## Running Tests

```bash
uv sync --extra dev
.venv/bin/python -m pytest tests/
```

See [`docs/mcp-testing-guide.md`](docs/mcp-testing-guide.md) for a full tool-by-tool testing walkthrough using the live MCP server.

---

## Documentation

| Document | Description |
| --- | --- |
| [`docs/para-gtd-guide.md`](docs/para-gtd-guide.md) | PARA/GTD workflow — areas, projects, tasks, today view, reminders |
| [`docs/mcp-testing-guide.md`](docs/mcp-testing-guide.md) | All 31 tools with example calls and expected output |
| [`docs/project-knowledge/user/link-types-in-zettelkasten-mcp-server.md`](docs/project-knowledge/user/link-types-in-zettelkasten-mcp-server.md) | Full link type reference |
| [`docs/prompts/system/system-prompt.md`](docs/prompts/system/system-prompt.md) | System prompt for Claude |
| [`docs/prompts/chat/`](docs/prompts/chat/) | Chat prompts for knowledge workflows |

---

## ⚠️ Important Notice

**USE AT YOUR OWN RISK**: This software is experimental and provided as-is without warranty. Always back up your notes regularly.

## Credits

Fork of [zettelkasten-mcp](https://github.com/entanglr/zettelkasten-mcp) by Peter J. Herrel. GTD/PARA extensions developed with Claude.

## License

MIT License
