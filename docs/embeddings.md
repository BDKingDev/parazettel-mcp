# Semantic embeddings (design & plan)

Status: **off by default.** Enable with `PARAZETTEL_EMBEDDING_ENABLED=true` plus an
install tier (below). When disabled, the system behaves exactly as before
(BM25 + lexical) and pulls in no ML dependencies.

## Design

- **Vectors live in Kuzu.** Each `Note` node gets an `embedding FLOAT[dim]`
  property and a native **HNSW** vector index (Kuzu 0.11.x ships the `vector`
  extension statically linked). One store, one backup, one atomic swap — no
  sidecar vector DB.
- **Hybrid retrieval via Reciprocal Rank Fusion.** Keep BM25 for exact terms,
  add dense vectors for meaning, fuse on *rank* (RRF) to avoid the
  score-incompatibility problem. Filtered vector search uses
  `project_graph_cypher` to scope by `note_type` etc.
- **Rebuild-driven index + brute-force fallback.** The HNSW index is (re)built in
  the existing rebuild → atomic-swap pipeline (also backfills embeddings). Notes
  changed since the last rebuild are covered at query time by a brute-force
  cosine pass (`array_cosine_similarity`) over that small dirty set, tracked via
  `embedded_at` / `embedding_model`. Embeddings are computed *outside* the global
  write lock and written *under* it.
- **Provider abstraction.** Everything depends only on the small
  `EmbeddingProvider` protocol (`services/embedding_provider.py`); the model and
  runtime are swappable via config.

## Install tiers

| Tier | Install | `embedding_provider` | Default model | Footprint |
| --- | --- | --- | --- | --- |
| base | *(none)* | `hash` | — | 0 ML deps (smoke/test only; not semantic) |
| **lite** (default) | `pip install 'parazettel-mcp[embeddings-lite]'` | `fastembed` | `BAAI/bge-small-en-v1.5` (384d) | ONNX, ~200 MB, no PyTorch, no HF gating |
| full | `pip install 'parazettel-mcp[embeddings]'` | `sentence-transformers` | `google/embeddinggemma-300m` (768d) | PyTorch ~2 GB, top quality, HF-gated |

## Config (env vars)

- `PARAZETTEL_EMBEDDING_ENABLED` (default `false`)
- `PARAZETTEL_EMBEDDING_PROVIDER` (`fastembed` | `sentence-transformers` | `hash`)
- `PARAZETTEL_EMBEDDING_MODEL`
- `PARAZETTEL_EMBEDDING_DIM` (must match the model; Matryoshka models may truncate)
- `PARAZETTEL_EMBEDDING_METRIC` (`cosine` | `l2` | `dotproduct`)

## Phased delivery (one PR each)

- **PR-0 — foundation:** config + provider abstraction + lite/full tiers + Kuzu
  embedding schema / HNSW vector-index helpers. Each piece is tested in isolation
  (provider with the hash backend; schema/index helpers against real Kuzu) but is
  **not yet wired into the rebuild/create paths** — generation lands in PR-1
  alongside the search that consumes it, so the hot write paths
  (`_build_graph_into`, `_index_note`) aren't changed for code nothing reads yet.
  Off by default; no behavior change.
- **PR-1 — populate + hybrid search:** wire embedding generation into the
  rebuild→swap (backfill + build the HNSW index) and the create/update paths,
  then add vector query + RRF into `search_combined` with a brute-force fallback.
  If the provider is enabled but unavailable, log a warning and **degrade to
  BM25** (never take search down).
- **PR-2 — `find_similar_notes`:** lexical → embedding cosine + HNSW top-K
  (also removes the O(N) full scan).
- **PR-3 — dedup-on-create:** add a semantic signal to the BM25 check.
- **PR-4 — semantic tag tool:** nearest existing tags (existing vault task).
- **PR-5 — auto-link:** propose `[[links]]` from top-K neighbors.

## Deferred decisions / future tasks

- **Evaluate upgrading the default tier from lite → full (EmbeddingGemma).**
  Once the pipeline is live, benchmark retrieval quality on the real vault
  (lite `bge-small`/384 vs full `EmbeddingGemma-300m`/768). Flip the default to
  the full tier only if the quality gain justifies the ~2 GB PyTorch install,
  higher daemon RAM, and HF gating. (Tracked in parazettel.)
- **Cross-encoder reranker** over the top-N fused results for extra precision —
  heavier dependency; only if hybrid alone proves insufficient.
