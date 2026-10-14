"""Session-hook helpers that read the vault directly, without the daemon.

Claude Code's SessionStart and UserPromptSubmit hooks run as short-lived
processes with a hard timeout. Normally they ask the daemon; when it can't
answer, these commands open the SQLite index directly instead — WAL lets them
read while any session writes — and print the context to inject (nothing at
all when there is nothing worth injecting):

    python -m parazettel_mcp.hook_cli briefing
    python -m parazettel_mcp.hook_cli recall --mode semantic|lexical < prompt.txt

``recall --mode semantic`` loads the embedding model in this process (~2s on
CPU); ``lexical`` uses only the full-text index (~150ms), which is what the
recall hook falls back to. Neither ever rebuilds the index or waits on a writer.
"""

import argparse
import os
import sys
from typing import List, Optional

from parazettel_mcp.config import config
from parazettel_mcp.models.schema import NoteType

RECALL_THRESHOLD = 0.55  # min cosine similarity worth injecting (semantic)
RECALL_LIMIT = 4  # max notes to surface
MIN_PROMPT_CHARS = 24  # skip trivial prompts ("yes", "continue", "fix that")
# Min BM25 score worth injecting (lexical). Prompts are long, so nearly every
# note matches some term weakly; only a strong top match is worth surfacing.
DEFAULT_RECALL_MIN_BM25 = 10.0


def _services(*, embeddings: bool):
    """Open the index read-mostly: no startup rebuild, embeddings optional."""
    if not embeddings:
        config.embedding_enabled = False
    from parazettel_mcp.services.search_service import SearchService
    from parazettel_mcp.services.zettel_service import ZettelService
    from parazettel_mcp.storage.note_repository import NoteRepository

    repository = NoteRepository(reconcile=False)
    zettel_service = ZettelService(repository=repository)
    return zettel_service, SearchService(zettel_service)


def briefing() -> Optional[str]:
    """Active projects, due tasks, reminders and central notes, as text."""
    zettel_service, search_service = _services(embeddings=False)
    try:
        lines: List[str] = []
        projects = [
            p
            for p in zettel_service.search_notes(note_type=NoteType.PROJECT)
            if (p.status.value if p.status else None) not in ("done", "cancelled")
        ]
        if projects:
            projects.sort(key=lambda p: p.due_date.isoformat() if p.due_date else "9999-12-31")
            lines.append(f"Active projects ({len(projects)}):")
            for p in projects[:8]:
                due = f" — due {p.due_date.isoformat()}" if p.due_date else ""
                lines.append(f"  - {p.title}{due} (ID: {p.id})")
        tasks = zettel_service.get_todays_tasks(True)
        if tasks:
            lines.append(f"Tasks due/overdue ({len(tasks)}):")
            for t in tasks[:10]:
                priority = f" [P{t.priority}]" if t.priority else ""
                due = f" — due {t.due_date.isoformat()}" if t.due_date else ""
                lines.append(f"  -{priority} {t.title}{due} (ID: {t.id})")
        reminders = zettel_service.get_reminders(8)
        if reminders:
            lines.append(f"Reminders due ({len(reminders)}):")
            for n in reminders:
                remind = n.remind_at.isoformat() if n.remind_at else ""
                lines.append(f"  - {n.title} (remind {remind}, ID: {n.id})")
        central = search_service.find_central_notes(6)
        if central:
            lines.append("Most-connected notes (orientation):")
            for note, _count in central[:6]:
                lines.append(f"  - {note.title} (ID: {note.id})")
        return "\n".join(lines) if lines else None
    finally:
        zettel_service.repository.close()


def recall(prompt: str, mode: str) -> Optional[str]:
    """Up to RECALL_LIMIT relevant notes for *prompt*, or None if none clear the bar."""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_CHARS:
        return None
    if mode == "semantic":
        zettel_service, _ = _services(embeddings=True)
        try:
            pairs = zettel_service.find_similar_to_text(
                prompt, threshold=RECALL_THRESHOLD, limit=RECALL_LIMIT
            )
        finally:
            zettel_service.repository.close()
    elif mode == "lexical":
        floor = float(os.getenv("PARAZETTEL_RECALL_MIN_BM25", DEFAULT_RECALL_MIN_BM25))
        zettel_service, _ = _services(embeddings=False)
        try:
            notes, scores = zettel_service.repository.search_scored(text=prompt)
        finally:
            zettel_service.repository.close()
        pairs = [(n, scores.get(n.id, 0.0)) for n in notes[:RECALL_LIMIT]]
        pairs = [(n, s) for n, s in pairs if s >= floor]
    else:
        raise ValueError(f"unknown recall mode {mode!r}")
    if not pairs:
        return None
    return "\n".join(f"  - [{score:.2f}] {note.title} (ID: {note.id})" for note, score in pairs)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("briefing")
    recall_parser = sub.add_parser("recall")
    recall_parser.add_argument("--mode", choices=["semantic", "lexical"], default="semantic")
    args = parser.parse_args(argv)
    if args.command == "briefing":
        text = briefing()
    else:
        text = recall(sys.stdin.read(), args.mode)
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
