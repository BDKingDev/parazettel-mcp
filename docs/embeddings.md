# Semantic embeddings (design & plan)

Status: **off by default.** Enable with `PARAZETTEL_EMBEDDING_ENABLED=true` plus an
install tier (below). When disabled, the system behaves exactly as before
(BM25 + lexical) and pulls in no ML dependencies (numpy, used for vector math,
is a core dependency).

## Design

- **Vectors live in the SQLite index.** Each note gets a **doc** vector and a
  **title** vector, stored as float32 BLOBs in the `embeddings` table alongside
  the model id and a hash of the embedded text. One store, one backup — no
  sidecar vector DB.
- **Exact search, no ANN index.** At vault scale (thousands of notes) an exact
  scan is a single matrix-vector product, so vectors are searched brute-force
  (cosine / l2 / dotproduct via numpy) — no HNSW index, no approximation. A note
  matches on the **closer** of its doc and title vectors. Each process caches
  the vector matrices and reloads them only when an embedding write (in *any*
  process) bumps a generation counter, so every session sees fresh vectors.
- **Hybrid retrieval via Reciprocal Rank Fusion.** Keep BM25 for exact terms,
  add dense vectors for meaning, fuse on *rank* (RRF, `k=60`) to avoid the
  score-incompatibility problem. Each result keeps its **BM25 score** so
  score-threshold consumers (dedup-on-create) are unaffected; vector-only hits
  carry score 0. Structural filters are applied to vector hits by intersecting
  with the existing filter query (no duplicated filter logic).
- **Writes update vectors in place; rebuild reuses them.** Creating or updating
  a note writes its vectors directly (there is no separate pending table). A
  rebuild embeds notes *before* its write transaction opens and **reuses a stored
  vector when the model and the embedded text (text hash) are unchanged**, so
  only the first rebuild — or one after a model change — re-embeds the whole
  vault. Create and rebuild embed the same file body, so a note's vector no
  longer changes just because the index was rebuilt.
- **Provider abstraction.** Everything depends only on the small
  `EmbeddingProvider` protocol (`services/embedding_provider.py`); the model and
  runtime are swappable via config.

## Install tiers

| Tier | Install | `embedding_provider` | Default model | Footprint |
| --- | --- | --- | --- | --- |
| base | *(none)* | `hash` | — | 0 ML deps (smoke/test only; not semantic) |
| **lite** (default) | `pip install 'parazettel-mcp[embeddings-lite]'` | `fastembed` | `BAAI/bge-small-en-v1.5` (384d) | ONNX, ~200 MB, no PyTorch, no HF gating |
| lite-gpu | `pip install 'parazettel-mcp[embeddings-lite-gpu]'` | `fastembed` | (same as lite) | lite + onnxruntime-gpu + CUDA 12 wheels; set `PARAZETTEL_EMBEDDING_DEVICE=cuda` |
| full | `pip install 'parazettel-mcp[embeddings]'` | `sentence-transformers` | `google/embeddinggemma-300m` (768d) | PyTorch ~2 GB, top quality, HF-gated |

## Config (env vars)

- `PARAZETTEL_EMBEDDING_ENABLED` (default `false`)
- `PARAZETTEL_EMBEDDING_PROVIDER` (`fastembed` | `sentence-transformers` | `hash`)
- `PARAZETTEL_EMBEDDING_MODEL`
- `PARAZETTEL_EMBEDDING_DIM` (must match the model; Matryoshka models may truncate)
- `PARAZETTEL_EMBEDDING_METRIC` (`cosine` | `l2` | `dotproduct`)
- `PARAZETTEL_EMBEDDING_DEVICE` (default `cpu`; `cuda` to run on an NVIDIA GPU) —
  needs the GPU install (`[embeddings-lite-gpu]`, or a CUDA torch build for the
  full tier). See **GPU acceleration** below.
- `PARAZETTEL_EMBEDDING_BATCH_SIZE` (default `16`) — bulk-embedding batch size.
  Keep it small for large models: the attention tensor is `batch x heads x seq^2`,
  so e.g. mxbai-large at the embedding library's default of 256 needs ~4 GB per
  batch and OOMs; 16 keeps it ~256 MB.
- `FASTEMBED_CACHE_PATH` (fastembed only) — a **persistent** directory for the
  ONNX model so it isn't re-downloaded on every daemon restart (the default is a
  temp dir that may be cleared).

## Choosing a model

`dim` must match the model. Storage/latency are non-issues at a few-thousand
short notes (1024-d ≈ 9 MB; queries are tens of ms warm), so pick on quality vs
footprint. "Beefier" does **not** require the heavy tier — fastembed (lite, ONNX,
no PyTorch, no HF gating) includes large models too:

| Model | Tier | dim | Notes |
| --- | --- | --- | --- |
| `BAAI/bge-small-en-v1.5` | lite | 384 | default; smallest footprint |
| `BAAI/bge-base-en-v1.5` | lite | 768 | solid step up |
| `nomic-ai/nomic-embed-text-v1.5` | lite | 768 | fast on CPU, Matryoshka |
| `mixedbread-ai/mxbai-embed-large-v1` | lite | 1024 | top lite-tier quality |
| `google/embeddinggemma-300m` | full | 768 | strong, but ~2 GB PyTorch **and** HF-gated (needs a token) |

EmbeddingGemma is gated (accept Google's license + a HuggingFace token) and only
runs on the full/torch tier; the beefy fastembed models avoid both.

## GPU acceleration

Local embedding inference runs on CPU by default. On an NVIDIA GPU it is ~2-3
orders of magnitude faster per document (e.g. ~1 ms/doc vs ~hundreds of ms on
CPU), which turns a multi-minute full-vault rebuild into seconds and makes a
heavier model — or a create-time cross-encoder reranker — practical.

Lite tier (fastembed / ONNX Runtime):

1. `pip install 'parazettel-mcp[embeddings-lite-gpu]'`. This pulls
   `onnxruntime-gpu` plus the `nvidia-*-cu12` runtime wheels (CUDA 12 + cuDNN 9),
   so **no system CUDA toolkit is required**. The CUDA 12 build runs on newer
   CUDA drivers (drivers are backward-compatible).
2. Set `PARAZETTEL_EMBEDDING_DEVICE=cuda` on the server env (alongside the other
   `PARAZETTEL_EMBEDDING_*` vars).
3. Restart the daemon. The provider selects the `CUDAExecutionProvider` and calls
   `onnxruntime.preload_dlls()` so ORT finds the CUDA/cuDNN DLLs in the nvidia-*
   wheels. If the GPU runtime is unavailable it falls back to CPU rather than
   failing. Confirm with `onnxruntime.get_available_providers()` (should list
   `CUDAExecutionProvider`).

Full tier (sentence-transformers / PyTorch): install a CUDA build of torch and
set `PARAZETTEL_EMBEDDING_DEVICE=cuda`; the provider passes `device="cuda"` to
`SentenceTransformer`.

The GPU's larger memory also lifts the CPU 4 GB-attention limit, so
`PARAZETTEL_EMBEDDING_BATCH_SIZE` can be raised well above 16 on GPU.

## Enabling on a vault (runbook)

1. Install a tier: `pip install 'parazettel-mcp[embeddings-lite]'` (or
   `[embeddings]` for the full/sentence-transformers tier).
2. Set the env on the MCP server definition (e.g. `~/.claude.json`
   `mcpServers.parazettel.env` and/or `~/.codex/config.toml`) — *not* a
   session-level settings file: `PARAZETTEL_EMBEDDING_ENABLED=true`, the
   provider/model/dim/metric, and a persistent `FASTEMBED_CACHE_PATH`.
3. Restart the daemon so it loads the embedding code, the env, and the model.
4. Run `pzk_rebuild_index` once to backfill embeddings (later rebuilds reuse
   unchanged vectors).
   Large bulk embeds are memory-bound — keep `PARAZETTEL_EMBEDDING_BATCH_SIZE`
   small. Changing the model or dim later requires another rebuild (re-embeds the
   whole vault), so choose before the first rebuild.
5. Verify via the daemon's `/health` endpoint: its `embeddings` block
   (`enabled`, `model_id`, `vectors_stored`, `generation`) should show
   `enabled: true` with `vectors_stored` close to `index.notes`. Far fewer
   vectors than notes means the provider failed silently (e.g. a missing model
   file).

## Phased delivery (one PR each)

*Historical: these PRs were delivered against the earlier Kuzu backend (HNSW
index, `PendingEmbedding` table). The SQLite index replaced both with exact
search over stored vectors; see **Design** above.*

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
