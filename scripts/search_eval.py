"""Search-quality eval harness: capture and compare retrieval across backends.

Builds a fresh index from a *frozen copy* of a notes directory (the live vault
and its database are never touched), runs a fixed query set through every
retrieval channel, and writes the top-k results plus known-item metrics to
JSON. Run it once per backend and diff the two runs with ``--compare``.

It uses only public service APIs, so the same script works before and after a
storage-backend change.

Usage::

    # 1. Generate the query set once (known-item probes sampled from the vault,
    #    plus optional real queries mined elsewhere):
    python scripts/search_eval.py generate --notes-src data/notes \\
        --extra-queries mined.json --out queries.json

    # 2. Capture a run (repeat per backend):
    python scripts/search_eval.py run --notes-src data/notes \\
        --queries queries.json --work-dir /tmp/eval-kuzu --out kuzu.json

    # 3. Compare a run against a baseline:
    python scripts/search_eval.py compare --baseline kuzu.json --candidate sqlite.json

Embedding settings come from the usual ``PARAZETTEL_EMBEDDING_*`` environment
variables (and ``FASTEMBED_CACHE_PATH``), so set them to match the live vault.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

TOP_K = 20
CHANNELS = ("bm25", "hybrid", "vector", "similar")
KNOWLEDGE_TYPES = {"fleeting", "literature", "permanent", "structure", "hub"}


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #


def _read_note_header(path: Path) -> Optional[Dict[str, str]]:
    """Return id/title/type/body-lead for a note without importing the package."""
    import frontmatter

    try:
        post = frontmatter.load(path)
    except Exception:
        return None
    note_id = str(post.metadata.get("id") or path.stem)
    title = str(post.metadata.get("title") or "").strip()
    note_type = str(post.metadata.get("type") or "permanent")
    body = re.sub(r"^#\s+.*\n?", "", post.content.lstrip(), count=1)
    body = body.split("\n## Links", 1)[0].strip()
    if not title:
        return None
    return {"id": note_id, "title": title, "type": note_type, "body": body}


def cmd_generate(args: argparse.Namespace) -> int:
    notes = [
        header
        for header in (
            _read_note_header(p) for p in sorted(Path(args.notes_src).glob("*.md"))
        )
        if header and header["type"] in KNOWLEDGE_TYPES
    ]
    rng = random.Random(args.seed)
    sample = rng.sample(notes, min(args.sample, len(notes)))
    queries: List[Dict[str, Any]] = []
    for note in sample:
        queries.append(
            {"kind": "title", "query": note["title"], "target": note["id"]}
        )
        lead = note["body"][:200].strip()
        dedup = f"{note['title']} {lead}".strip()
        queries.append({"kind": "dedup", "query": dedup, "target": note["id"]})
    if args.extra_queries:
        for item in json.loads(Path(args.extra_queries).read_text()):
            queries.append(
                {"kind": f"real-{item['kind']}", "query": item["query"], "target": None}
            )
    Path(args.out).write_text(json.dumps(queries, indent=1))
    kinds: Dict[str, int] = {}
    for query in queries:
        kinds[query["kind"]] = kinds.get(query["kind"], 0) + 1
    print(f"wrote {len(queries)} queries to {args.out}: {kinds}")
    return 0


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def _prepare_environment(
    work_dir: Path, notes_src: Path, seed_index: Optional[Path] = None
) -> None:
    """Point the package at a private copy of the notes and a fresh index.

    With *seed_index*, the SQLite index starts as a copy of that file (e.g. one
    the Kuzu migration produced from the baseline run), so both runs search the
    very same vectors and only the engines differ. Must run before anything
    from ``parazettel_mcp`` is imported, because the config object reads the
    environment at import time.
    """
    work, src = work_dir.resolve(), notes_src.resolve()
    if work == src or work in src.parents or src in work.parents:
        sys.exit(
            f"--work-dir {work_dir} overlaps --notes-src {notes_src}; refusing to "
            "delete it (the work dir is wiped and must be separate from the notes)."
        )
    if seed_index is not None and work in seed_index.resolve().parents:
        sys.exit(f"--seed-index {seed_index} is inside --work-dir, which is wiped.")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    notes_dir = work_dir / "notes"
    shutil.copytree(notes_src, notes_dir, ignore=shutil.ignore_patterns(".obsidian"))
    db_dir = work_dir / "db"
    db_dir.mkdir(parents=True)
    if seed_index is not None:
        shutil.copy2(seed_index, db_dir / "index.sqlite3")
    os.environ["PARAZETTEL_NOTES_DIR"] = str(notes_dir)
    # Both names are set so the script works on either backend.
    os.environ["PARAZETTEL_GRAPH_DB_PATH"] = str(db_dir / "graph.kuzu")
    os.environ["PARAZETTEL_INDEX_DB_PATH"] = str(db_dir / "index.sqlite3")
    os.environ["PARAZETTEL_BACKEND_MODE"] = "direct"


def _rank(ids: List[str], target: Optional[str]) -> Optional[int]:
    if target is None:
        return None
    try:
        return ids.index(target) + 1
    except ValueError:
        return None


def cmd_run(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).resolve()
    _prepare_environment(
        work_dir,
        Path(args.notes_src).resolve(),
        Path(args.seed_index).resolve() if args.seed_index else None,
    )

    from parazettel_mcp.services.search_service import SearchService
    from parazettel_mcp.services.zettel_service import ZettelService
    from parazettel_mcp.storage.note_repository import NoteRepository

    started = time.perf_counter()
    repository = NoteRepository()
    build_seconds = time.perf_counter() - started
    zettel_service = ZettelService(repository)
    search_service = SearchService(zettel_service)
    embeddings_on = getattr(repository, "_embedding_provider", None) is not None
    print(
        f"index built in {build_seconds:.1f}s "
        f"(embeddings {'on' if embeddings_on else 'off'})",
        flush=True,
    )

    queries = json.loads(Path(args.queries).read_text())
    results: List[Dict[str, Any]] = []
    latencies: Dict[str, List[float]] = {channel: [] for channel in CHANNELS}
    for index, item in enumerate(queries, 1):
        text = item["query"]
        row: Dict[str, Any] = dict(item)

        def timed(channel: str, fn):
            t0 = time.perf_counter()
            try:
                value = fn()
            except Exception as exc:  # record, don't abort the whole run
                row[f"{channel}_error"] = repr(exc)
                value = []
            latencies[channel].append(time.perf_counter() - t0)
            return value

        notes, scores = timed("bm25", lambda: repository.search_scored(text=text)) or (
            [],
            {},
        )
        row["bm25"] = [[n.id, float(scores.get(n.id, 0.0))] for n in notes[:TOP_K]]
        hybrid = timed("hybrid", lambda: search_service.search_combined(text=text))
        row["hybrid"] = [[r.note.id, float(r.score)] for r in hybrid[:TOP_K]]
        if embeddings_on:
            vector = timed("vector", lambda: repository.vector_search(text, limit=TOP_K))
            row["vector"] = [[i, float(s)] for i, s in vector[:TOP_K]]
            similar = timed(
                "similar",
                lambda: zettel_service.find_similar_to_text(
                    text, threshold=0.0, limit=TOP_K
                ),
            )
            row["similar"] = [[n.id, float(s)] for n, s in similar[:TOP_K]]
        for channel in CHANNELS:
            ids = [entry[0] for entry in row.get(channel, [])]
            row[f"{channel}_rank"] = _rank(ids, item.get("target"))
        results.append(row)
        if index % 50 == 0:
            print(f"  {index}/{len(queries)} queries", flush=True)

    payload = {
        "meta": {
            "backend_module": type(repository).__module__,
            "graph_db": os.environ["PARAZETTEL_GRAPH_DB_PATH"],
            "index_db": os.environ["PARAZETTEL_INDEX_DB_PATH"],
            "embedding_model": os.getenv("PARAZETTEL_EMBEDDING_MODEL"),
            "embeddings_on": embeddings_on,
            "note_count": len(list((work_dir / "notes").glob("*.md"))),
            "build_seconds": round(build_seconds, 2),
            "latency_ms_p50": {
                c: round(statistics.median(v) * 1000, 2) for c, v in latencies.items() if v
            },
        },
        "summary": summarize(results),
        "results": results,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print(json.dumps(payload["meta"], indent=1))
    print(json.dumps(payload["summary"], indent=1))
    if hasattr(repository, "close"):
        repository.close()
    return 0


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Known-item metrics per (query kind, channel)."""
    summary: Dict[str, Any] = {}
    kinds = sorted({r["kind"] for r in results if r.get("target")})
    for kind in kinds:
        rows = [r for r in results if r["kind"] == kind]
        for channel in CHANNELS:
            if not any(channel in r for r in rows):
                continue
            ranks = [r.get(f"{channel}_rank") for r in rows]
            n = len(ranks)
            summary[f"{kind}/{channel}"] = {
                "n": n,
                "mrr@10": round(
                    sum(1.0 / rk for rk in ranks if rk and rk <= 10) / n, 4
                ),
                "recall@1": round(sum(1 for rk in ranks if rk == 1) / n, 4),
                "recall@5": round(sum(1 for rk in ranks if rk and rk <= 5) / n, 4),
                "recall@10": round(sum(1 for rk in ranks if rk and rk <= 10) / n, 4),
            }
    return summary


def _overlap(a: List[str], b: List[str], k: int = 10) -> Optional[float]:
    a, b = a[:k], b[:k]
    if not a and not b:
        return None
    return len(set(a) & set(b)) / max(len(a), len(b))


def cmd_compare(args: argparse.Namespace) -> int:
    base = json.loads(Path(args.baseline).read_text())
    cand = json.loads(Path(args.candidate).read_text())
    by_query = {(r["kind"], r["query"]): r for r in base["results"]}

    print("== known-item metrics (baseline -> candidate) ==")
    for key in sorted(set(base["summary"]) | set(cand["summary"])):
        b = base["summary"].get(key, {})
        c = cand["summary"].get(key, {})
        cells = []
        for metric in ("mrr@10", "recall@1", "recall@10"):
            bv, cv = b.get(metric), c.get(metric)
            delta = "" if bv is None or cv is None else f" ({cv - bv:+.3f})"
            cells.append(f"{metric} {bv} -> {cv}{delta}")
        print(f"{key:22s} " + " | ".join(cells))

    print("\n== overlap@10 with baseline, by query kind / channel ==")
    overlaps: Dict[str, List[float]] = {}
    regressions: List[str] = []
    for row in cand["results"]:
        ref = by_query.get((row["kind"], row["query"]))
        if not ref:
            continue
        for channel in CHANNELS:
            if channel not in row or channel not in ref:
                continue
            value = _overlap(
                [e[0] for e in ref[channel]], [e[0] for e in row[channel]]
            )
            if value is not None:
                overlaps.setdefault(f"{row['kind']}/{channel}", []).append(value)
        before, after = ref.get("hybrid_rank"), row.get("hybrid_rank")
        if before and before <= 10 and not (after and after <= 10):
            regressions.append(f"[{row['kind']}] {row['query'][:70]!r}: {before} -> {after}")
    for key in sorted(overlaps):
        values = overlaps[key]
        print(f"{key:22s} mean {statistics.mean(values):.3f}  min {min(values):.2f}  n={len(values)}")

    print(f"\n== hybrid known-item regressions (in top-10 before, not after): {len(regressions)} ==")
    for line in regressions[:40]:
        print("  " + line)
    print(
        "\nbuild seconds:", base["meta"].get("build_seconds"), "->", cand["meta"].get("build_seconds")
    )
    print("p50 latency ms:", base["meta"].get("latency_ms_p50"), "->", cand["meta"].get("latency_ms_p50"))
    return 0


# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="build the fixed query set")
    gen.add_argument("--notes-src", required=True)
    gen.add_argument("--out", required=True)
    gen.add_argument("--sample", type=int, default=200)
    gen.add_argument("--seed", type=int, default=20261006)
    gen.add_argument("--extra-queries", help="JSON list of {kind, query} real queries")
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="index a frozen notes copy and capture results")
    run.add_argument("--notes-src", required=True)
    run.add_argument("--queries", required=True)
    run.add_argument("--work-dir", required=True, help="scratch dir; deleted and recreated")
    run.add_argument("--out", required=True)
    run.add_argument(
        "--seed-index",
        help="SQLite index file to start from instead of building one (same notes)",
    )
    run.set_defaults(func=cmd_run)

    cmp_ = sub.add_parser("compare", help="compare a candidate run against a baseline")
    cmp_.add_argument("--baseline", required=True)
    cmp_.add_argument("--candidate", required=True)
    cmp_.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
