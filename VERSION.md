# Version History

## v0.5.1.1 (Current Release)

**Release Date:** 2026-05-08

### Highlights

- Kuzu graph database is now the primary runtime index
- new local daemon backend for multi-chat use
- stdio MCP facade can proxy through the daemon instead of opening the DB directly
- direct mode still exists for development, tests, and simple single-chat runs
- shared daemon mode restores safe multi-chat write behavior by giving one process sole graph DB ownership

### Features

- `30` MCP tools for note, task, project, and area workflows
- dual storage: Markdown files as source of truth plus Kuzu graph index
- Kuzu full-text search and graph-backed filtering
- batch note retrieval with `pzk_get_notes`
- tag-based retrieval with `pzk_get_notes_by_tag`
- project context retrieval with `pzk_get_project_notes`
- subproject support with `parent_project_id` and `pzk_create_subproject`
- project summaries with parent/subproject context
- rebuild-time graph backup and migration tooling for SQLite-era vaults

### Runtime and Storage Changes

- primary storage path is now `PARAZETTEL_GRAPH_DB_PATH`
- `PARAZETTEL_DATABASE_PATH` and `--database-path` remain as compatibility inputs
- direct cross-process graph DB ownership is no longer the recommended multi-chat setup
- recommended multi-chat runtime is:
  - one shared local daemon
  - one or more MCP facades configured with `PARAZETTEL_BACKEND_MODE=daemon`

### Documentation Changes

- README updated for daemon-backed multi-chat setup
- INSTALL and QUICKSTART updated for graph DB + daemon runtime
- MCP testing guide updated to reflect current project/subproject and graph behavior

## v0.4.0

### Highlights

- first major Parazettel-branded release
- GTD/PARA features became first-class
- plugin/docs packaging matured significantly

### Features

- note, task, project, and area management through MCP
- recurring tasks
- semantic link graph
- project and area routing
- plugin hooks and extraction workflows

### Historical Note

This release still assumed the older SQLite-era runtime model and did not yet include the daemon-backed multi-chat fix.

## v0.3.0

- status field on notes
- task management with priorities and energy levels
- project and area routing
- reminders

## v0.2.0

- semantic link types
- full-text search
- graph similarity operations

## v0.1.0

- basic Zettelkasten note creation
- Markdown-backed notes with indexed retrieval
- simple tags and search
- forked from `zettelkasten-mcp`
