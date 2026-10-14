#!/usr/bin/env python3
"""One-time migration from the Kuzu graph database to the SQLite index.

The markdown notes are the source of truth, so the SQLite index is simply
rebuilt from them. Two things exist only in the old Kuzu database and are
carried over explicitly:

* retrieval signals (``hit_count`` / ``last_retrieved_at``), and
* the stored embedding vectors — imported so the migration doesn't spend many
  minutes re-embedding the whole vault on CPU. Imported vectors carry no text
  hash, so the next ``pzk_rebuild_index`` re-embeds them from the current files
  (pass ``--reembed`` to do that now instead). Notes Kuzu had NO vector for
  (e.g. created while the embedding model was unavailable) are embedded now,
  when embeddings are configured, so none are left out of semantic search.

The Kuzu database is opened READ-ONLY and never modified (except by the
explicit ``--export-signals-back`` rollback mode), so rolling back is just
pointing the old code at it again.

Stop the daemon and every Claude session first: Kuzu refuses to open a database
another process holds.

Usage
-----
    python scripts/migrate_kuzu_to_sqlite.py [--notes-dir PATH] [--kuzu-path PATH]
        [--index-db-path PATH] [--reembed] [--force] [--accept-differences]

    # Rollback helper: copy hit counts recorded in SQLite back into Kuzu.
    python scripts/migrate_kuzu_to_sqlite.py --export-signals-back

Requires the ``kuzu`` package (``pip install "parazettel-mcp[legacy-kuzu]"``).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from parazettel_mcp.config import config
from parazettel_mcp.storage.index_db import (
    EMBEDDING_GENERATION_KEY,
    bump_counter,
    transaction,
    ts_in,
    vector_to_blob,
)

_KUZU_BUFFER_POOL_BYTES = 1024**3  # read-only scan; 1 GiB is plenty


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--notes-dir", help="Notes directory (default: config)")
    parser.add_argument("--kuzu-path", help="Kuzu database file (default: config graph_db_path)")
    parser.add_argument("--index-db-path", help="SQLite index to create (default: config)")
    parser.add_argument(
        "--reembed",
        action="store_true",
        help="Embed every note now instead of importing the Kuzu vectors (slow on CPU)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Move an existing SQLite index aside (as a .bak) instead of refusing",
    )
    parser.add_argument(
        "--accept-differences",
        action="store_true",
        help="Exit 0 even if note/link/tag sets differ from Kuzu (they are still reported)",
    )
    parser.add_argument(
        "--export-signals-back",
        action="store_true",
        help="Rollback helper: write SQLite retrieval signals back into the Kuzu DB",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- #
# Kuzu side (read-only)
# --------------------------------------------------------------------------- #


def _open_kuzu(path: Path, *, read_only: bool):
    try:
        import kuzu
    except ImportError:
        sys.exit(
            "The kuzu package is required to read the old database: "
            'pip install "parazettel-mcp[legacy-kuzu]"'
        )
    if not path.exists():
        sys.exit(f"No Kuzu database at {path}")
    db = kuzu.Database(str(path), read_only=read_only, buffer_pool_size=_KUZU_BUFFER_POOL_BYTES)
    return db, kuzu.Connection(db)


def _rows(conn, query: str, params: Optional[Dict[str, Any]] = None) -> List[list]:
    result = conn.execute(query, params or {})
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


def read_kuzu(path: Path) -> Dict[str, Any]:
    """Snapshot everything needed from the Kuzu database."""
    db, conn = _open_kuzu(path, read_only=True)
    try:
        note_ids = {r[0] for r in _rows(conn, "MATCH (n:Note) RETURN n.id")}
        links = collections.Counter(
            (r[0], r[1], r[2])
            for r in _rows(
                conn,
                "MATCH (s:Note)-[r:LINKS_TO]->(t:Note) RETURN s.id, t.id, r.link_type",
            )
        )
        tags = {
            (r[0], r[1])
            for r in _rows(conn, "MATCH (n:Note)-[:HAS_TAG]->(t:Tag) RETURN n.id, t.name")
        }
        signals = [
            {
                "id": r[0],
                "hit_count": int(r[1] or 0),
                "last_retrieved_at": r[2].isoformat() if r[2] else None,
            }
            for r in _rows(
                conn,
                "MATCH (n:Note) WHERE n.hit_count IS NOT NULL "
                "RETURN n.id, n.hit_count, n.last_retrieved_at",
            )
        ]
        columns = {r[1] for r in _rows(conn, "CALL TABLE_INFO('Note') RETURN *")}
        vectors: Dict[str, Tuple[str, List[float], Optional[List[float]]]] = {}
        if "embedding" in columns:
            for note_id, doc, title, model in _rows(
                conn,
                "MATCH (n:Note) WHERE n.embedding IS NOT NULL "
                "RETURN n.id, n.embedding, n.title_embedding, n.embedding_model",
            ):
                vectors[note_id] = (model, doc, title)
            # Vectors written since the last Kuzu rebuild live in PendingEmbedding
            # and are newer than the indexed ones.
            for note_id, doc, title, model in _rows(
                conn,
                "MATCH (p:PendingEmbedding) WHERE p.embedding IS NOT NULL "
                "RETURN p.id, p.embedding, p.title_embedding, p.embedding_model",
            ):
                vectors[note_id] = (model, doc, title)
    finally:
        conn.close()
        db.close()
    return {
        "note_ids": note_ids,
        "links": links,
        "tags": tags,
        "signals": signals,
        "vectors": vectors,
    }


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #


def _describe(name: str, kuzu_side, sqlite_side) -> List[str]:
    """Human-readable differences between two collections (empty if equal)."""
    if kuzu_side == sqlite_side:
        return []
    if isinstance(kuzu_side, collections.Counter):
        only_kuzu = list((kuzu_side - sqlite_side).elements())
        only_sqlite = list((sqlite_side - kuzu_side).elements())
    else:
        only_kuzu = sorted(kuzu_side - sqlite_side)
        only_sqlite = sorted(sqlite_side - kuzu_side)
    lines = [f"{name}: {len(only_kuzu)} only in Kuzu, {len(only_sqlite)} only in SQLite"]
    lines += [f"    kuzu-only   {item}" for item in only_kuzu[:15]]
    lines += [f"    sqlite-only {item}" for item in only_sqlite[:15]]
    return lines


def _embed_missing(repository, conn) -> None:
    """Embed the notes that have no vector for the configured model."""
    from parazettel_mcp.services.embedding_provider import build_embedding_provider

    provider = build_embedding_provider(config)
    if provider is None:
        return
    missing_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM notes WHERE id NOT IN "
            "(SELECT note_id FROM embeddings WHERE model = ?)",
            (provider.model_id,),
        )
    ]
    if not missing_ids:
        return
    print(f"Embedding {len(missing_ids)} notes that had no vector ({provider.model_id})...")
    repository._embedding_provider = provider
    notes = [n for n in (repository.get(i) for i in missing_ids) if n is not None]
    embeddings = repository._compute_embeddings(notes)
    with transaction(conn):
        repository._write_embeddings(conn, embeddings)
    print(f"  embedded {len(embeddings)}/{len(missing_ids)}")


def migrate(args: argparse.Namespace) -> int:
    if args.notes_dir:
        config.notes_dir = Path(args.notes_dir)
    if args.index_db_path:
        config.index_db_path = Path(args.index_db_path)
    kuzu_path = Path(args.kuzu_path) if args.kuzu_path else config.get_legacy_kuzu_path()
    index_path = config.get_index_db_path()
    if index_path.resolve() == kuzu_path.resolve():
        sys.exit("The index path must differ from the Kuzu path.")

    if index_path.exists():
        if not args.force:
            sys.exit(f"{index_path} already exists; pass --force to move it aside.")
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            existing = Path(f"{index_path}{suffix}")
            if existing.exists():
                shutil.move(str(existing), f"{existing}.{stamp}.pre-migration.bak")

    print(f"Reading Kuzu database (read-only): {kuzu_path}")
    snapshot = read_kuzu(kuzu_path)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    export_path = kuzu_path.with_name(f"kuzu_export.{stamp}.json")
    export_path.write_text(
        json.dumps(
            {
                "kuzu_path": str(kuzu_path),
                "notes": len(snapshot["note_ids"]),
                "links": sum(snapshot["links"].values()),
                "tag_memberships": len(snapshot["tags"]),
                "vectors": len(snapshot["vectors"]),
                "signals": snapshot["signals"],
            },
            indent=1,
        )
    )
    print(
        f"  {len(snapshot['note_ids'])} notes, {sum(snapshot['links'].values())} links, "
        f"{len(snapshot['tags'])} tag memberships, {len(snapshot['vectors'])} vectors, "
        f"{len(snapshot['signals'])} notes with retrieval signals"
    )
    print(f"  audit export: {export_path}")

    # Build the index from the markdown files. Unless re-embedding, construct
    # the repository with embeddings off so the rebuild doesn't embed anything;
    # the Kuzu vectors are imported below instead.
    embedding_enabled = config.embedding_enabled
    if not args.reembed:
        config.embedding_enabled = False
    from parazettel_mcp.services.zettel_service import ZettelService
    from parazettel_mcp.storage.note_repository import NoteRepository

    print(f"Building SQLite index from markdown: {index_path}")
    try:
        repository = NoteRepository()
    finally:
        config.embedding_enabled = embedding_enabled
    service = ZettelService(repository=repository)
    if repository.last_rebuild_skipped:
        print(f"  WARNING: {len(repository.last_rebuild_skipped)} unparseable files skipped")

    conn = repository._get_conn()
    with transaction(conn):
        if not args.reembed and snapshot["vectors"]:
            conn.executemany(
                "INSERT INTO embeddings"
                "(note_id, model, dim, doc_vec, title_vec, text_hash, embedded_at) "
                "SELECT ?, ?, ?, ?, ?, NULL, ? WHERE EXISTS (SELECT 1 FROM notes WHERE id = ?)",
                [
                    (
                        note_id,
                        model,
                        len(doc),
                        vector_to_blob(doc),
                        vector_to_blob(title) if title is not None else None,
                        ts_in(datetime.datetime.now()),
                        note_id,
                    )
                    for note_id, (model, doc, title) in snapshot["vectors"].items()
                ],
            )
            bump_counter(conn, EMBEDDING_GENERATION_KEY)
        conn.executemany(
            "INSERT INTO retrieval_signals(note_id, hit_count, last_retrieved_at) "
            "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM notes WHERE id = ?) "
            "ON CONFLICT(note_id) DO UPDATE SET hit_count = excluded.hit_count, "
            "last_retrieved_at = excluded.last_retrieved_at",
            [
                (
                    s["id"],
                    s["hit_count"],
                    ts_in(datetime.datetime.fromisoformat(s["last_retrieved_at"]))
                    if s["last_retrieved_at"]
                    else None,
                    s["id"],
                )
                for s in snapshot["signals"]
            ],
        )

    if not args.reembed and config.embedding_enabled:
        _embed_missing(repository, conn)

    # Verify against Kuzu and the files.
    sqlite_ids = {r[0] for r in conn.execute("SELECT id FROM notes")}
    sqlite_links = collections.Counter(
        (r[0], r[1], r[2])
        for r in conn.execute("SELECT source_id, target_id, link_type FROM links")
    )
    sqlite_tags = {(r[0], r[1]) for r in conn.execute("SELECT note_id, tag FROM note_tags")}
    vectors = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    signals = conn.execute(
        "SELECT count(*), coalesce(sum(hit_count), 0) FROM retrieval_signals"
    ).fetchone()
    differences = (
        _describe("notes", snapshot["note_ids"], sqlite_ids)
        + _describe("links", snapshot["links"], sqlite_links)
        + _describe("tag memberships", snapshot["tags"], sqlite_tags)
    )
    report = service.check_consistency()
    print(
        f"SQLite index: {len(sqlite_ids)} notes, {sum(sqlite_links.values())} links, "
        f"{len(sqlite_tags)} tag memberships, {vectors} vectors, "
        f"{signals[0]} notes / {signals[1]} hits of retrieval signals"
    )
    print(f"check_consistency: consistent={report['consistent']}")
    repository.close()

    if differences:
        print("Differences from Kuzu (Kuzu may itself have drifted from the files):")
        print("\n".join(differences))
    if not report["consistent"]:
        print("ERROR: the new index is not consistent with the markdown files:")
        print(json.dumps({k: v for k, v in report.items() if v}, indent=1, default=str))
        return 1
    if differences and not args.accept_differences:
        print("Re-run with --accept-differences once you've reviewed them.")
        return 1
    print("Migration complete.")
    return 0


def export_signals_back(args: argparse.Namespace) -> int:
    """Rollback helper: copy SQLite retrieval signals into the Kuzu database."""
    import sqlite3

    if args.index_db_path:
        config.index_db_path = Path(args.index_db_path)
    kuzu_path = Path(args.kuzu_path) if args.kuzu_path else config.get_legacy_kuzu_path()
    source = sqlite3.connect(str(config.get_index_db_path()))
    rows = source.execute(
        "SELECT note_id, hit_count, last_retrieved_at FROM retrieval_signals"
    ).fetchall()
    source.close()
    db, conn = _open_kuzu(kuzu_path, read_only=False)
    try:
        conn.execute(
            "UNWIND $rows AS row MATCH (n:Note {id: row.id}) "
            "SET n.hit_count = row.hit_count, n.last_retrieved_at = row.ts",
            {
                "rows": [
                    {
                        "id": note_id,
                        "hit_count": hits,
                        "ts": datetime.datetime.fromisoformat(ts) if ts else None,
                    }
                    for note_id, hits, ts in rows
                ]
            },
        )
    finally:
        conn.close()
        db.close()
    print(f"Wrote retrieval signals for {len(rows)} notes back into {kuzu_path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.export_signals_back:
        return export_signals_back(args)
    return migrate(args)


if __name__ == "__main__":
    sys.exit(main())
