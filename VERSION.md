# Version History

## Unreleased

Merged to `main` after v0.5.1.1; not yet version-bumped.

### Highlights

- **Search ranks by BM25 relevance.** `pzk_search_notes` now orders text results by the Kuzu full-text index's own relevance score (lexical hits act only as a tiebreaker) instead of a substring heuristic that flattened most matches to a constant. Results surface a `Relevance:` score and a `Match:` context line.
- **`pzk_rebuild_index` rebuilt for safety.** It now builds a fresh index into a temporary database and atomically swaps it in (no in-place clear), fixing a Kuzu bulk-delete segfault on large graphs. It reports unparseable/skipped files instead of dropping them silently, and reliably drops orphaned tag nodes. The pre-rebuild backup no longer races the open DB on Windows (WinError 33).
- **New `pzk_check_consistency` tool** — read-only audit of file-vs-index drift (missing-from-index, missing-from-files, content drift). `31` MCP tools total.
- **`pzk_get_all_tags` is edge-derived** — returns only tags actually in use across the vault (via `HAS_TAG` edges), so orphaned tag nodes never surface.
- **`pzk_find_similar_notes` is content-aware** — blends length-aware lexical overlap of title/content with the structural tag/link signal (excluding PARA routing links), so notes with no shared tags or links can still match.
- **Area reference link syncs on re-route** — changing a note's `area_id` (directly or via project) now rewrites its `## Links` area reference to match.
- **Actionable daemon-down errors** — the daemon-unavailable message now includes an absolute, runnable command to start the daemon.

### AI memory: link integrity, retrieval ergonomics, bidirectional membership

This batch turns the vault from a search index into durable AI memory. Brings the total to **36 MCP tools**.

- **Inline `[[id]]` prose links are first-class** — a wiki-link in a note body (outside `## Links`) is indexed as an `inline` graph edge, scrubbed on delete (leaving readable alias/title text), alias-refreshed on rename, and reported as a dangling ref by `pzk_check_consistency`. Every form is handled (`[[id]]`, `[[id|alias]]`, `[[id#fragment]]`, `[[id.md]]`).
- **Hand-edited `## Links` is honored** — passing content with a `## Links` section to `pzk_update_note` reconciles its entries into the graph (adds/removes links, honors description edits) instead of silently discarding them; routing-derived links are preserved so an edit can't de-route a note. Content with no `## Links` heading leaves links untouched.
- **Durable link timestamps** — a link's `created_at` persists in markdown (invisible HTML comment) and survives a full `pzk_rebuild_index`.
- **Bidirectional PARA membership in both layers** — every routed note carries `part_of` to its container, and the container's markdown carries a materialized `has_part` counter link per member (areas as well as projects). Note embeddings strip the `## Links` section so a large membership list never pollutes a container's semantic vector. `scripts/backfill_counter_links.py` upgrades an existing vault.
- **New AI-ergonomics tools** — `pzk_briefing` (one-call session orientation), `pzk_ingest_batch` (notes + links + tasks in one call with `#N` cross-refs and a per-note dedup gate that flags rather than auto-folds), `pzk_get_neighborhood` (hop-grouped graph map), `pzk_find_tensions` (unlinked same-topic notes framed for fold/link/contradict).
- **Calibrated, AI-friendly output** — the MCP server ships an operating-instructions block (full-claim queries, score calibration, never-guess-IDs, tag reuse); search/similarity results carry a calibrated verdict line; heavy readers accept `detail=ids|summary|full`; not-found errors give did-you-mean recovery; results never truncate silently.
- **Memory primitives** — new `origin` (provenance) and `last_verified` note fields; graph-side retrieval signals (`hit_count`, `last_retrieved_at`) carried across rebuilds; tags normalized on every write (lowercase-hyphenated, `@` GTD prefix preserved).
- **Reliability** — daemon auto-start serialized with an OS file lock (kills the double-spawn read-only race); `delete()` is resilient to unparseable notes; test suite parallelized (~5.5 min → ~2 min) via a session-scoped template DB and a serialized rebuild group.

### Storage: the index moves from Kuzu to SQLite

Kuzu was archived upstream in October 2025 (final release 0.11.3), so the index is now SQLite (stdlib `sqlite3`, WAL mode). Markdown files remain the source of truth; the index rebuilds from them.

- **New index path** — `PARAZETTEL_INDEX_DB_PATH` / `--index-db-path`, default `data/db/index.sqlite3`. `PARAZETTEL_GRAPH_DB_PATH` / `--graph-db-path` are deprecated but still honoured (a `*.kuzu` value places the index at a sibling `index.sqlite3`; never the Kuzu file itself), so existing configs keep working. `PARAZETTEL_DATABASE_PATH` mapping is unchanged.
- **Migration** — stop the daemon and all Claude sessions, then run `scripts/migrate_kuzu_to_sqlite.py`. It opens `graph.kuzu` read-only, rebuilds from markdown, imports retrieval signals and existing vectors, embeds notes that had none, verifies against Kuzu and `check_consistency`, and writes a `kuzu_export.<ts>.json` audit export (`--reembed`, `--force`, `--accept-differences`, `--export-signals-back`). Rollback = previous release + the untouched `graph.kuzu`. The `kuzu` dependency and the migration script have since been removed (recover the script with `git show d0eeb77:scripts/migrate_kuzu_to_sqlite.py`).
- **No more read-only mode** — every MCP process (each Claude session) can read and write. A cross-process write lock (`<index>.write.lock`) serializes writes so concurrent sessions can't lose each other's markdown edits. The daemon is no longer needed for correctness; it still keeps the embedding model and reranker warm.
- **Full-text search on FTS5** — porter stemming and the same English stopword list; digits are now searchable; query text is quoted so it can't inject FTS syntax. BM25 stays higher-is-better.
- **Exact vector search** — float32 vectors searched brute-force (cosine/l2/dotproduct via numpy, now a core dependency); no HNSW index or `PendingEmbedding` table. A note matches on the closer of its doc and title vectors.
- **Faster rebuilds** — a rebuild reuses each stored vector whose model and embedded text are unchanged, and runs in one write transaction (readers see the old index until commit). Backups use SQLite's online backup API; `PARAZETTEL_INDEX_BACKUP_KEEP` (default 5) bounds them.
- **New settings** — `PARAZETTEL_INDEX_BACKUP_KEEP`, `PARAZETTEL_SQLITE_BUSY_TIMEOUT_MS` (default 10000), `PARAZETTEL_DEDUP_MIN_BM25` (default 1.5). `PARAZETTEL_KUZU_BUFFER_POOL_BYTES` is removed (ignored with a warning).
- **`/health` embedding status** — the daemon health payload now includes `index` (`backend`, `path`, `notes`, `journal_mode`) and `embeddings` (`enabled`, `model_id`, `vectors_stored`, `generation`).
- **`scripts/search_eval.py`** — `generate` / `run` / `compare` harness for measuring search quality across backends.
- **Fixed:** a note's vector no longer changes after a rebuild — create and rebuild now embed the same file body.
- **Tests** — `pytest -n 4` is enough (~10s); `--dist loadgroup` is no longer needed because rebuild tests aren't serialized.

## v0.5.1.1 (Current Release)

**Release Date:** 2026-05-11

### Highlights

- `get_tasks()` now hides `done` and `archived` tasks by default
- explicit status filters still allow direct retrieval of `done` and `archived` tasks
- runtime version resolution now prefers the local source checkout version during active development

## v0.5.1

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
