"""MCP server implementation for the Zettelkasten."""

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Protocol, Tuple

from mcp.server.fastmcp import FastMCP
from parazettel_mcp.config import config
from parazettel_mcp.daemon.client import (
    DaemonRpcClient,
    DaemonUnavailableError,
    RemoteServiceProxy,
)
from parazettel_mcp.models.graph_db import GraphDatabaseReadOnlyError
from parazettel_mcp.models.schema import (
    LinkType,
    Note,
    NoteSource,
    NoteStatus,
    NoteType,
)
from parazettel_mcp.services.reranker import RerankerError, build_reranker
from parazettel_mcp.services.search_service import SearchService
from parazettel_mcp.services.zettel_service import ZettelService

logger = logging.getLogger(__name__)

_DAEMON_HEALTH_TIMEOUT_SECONDS = 5.0

# Note types that get a duplicate check on create. Knowledge notes only — area,
# project, and task are structural/action-item types handled by their own tools
# (pzk_create_area / pzk_create_project / pzk_create_task) and are not deduped.
_DEDUP_NOTE_TYPES = frozenset(
    {
        NoteType.FLEETING,
        NoteType.LITERATURE,
        NoteType.PERMANENT,
        NoteType.STRUCTURE,
        NoteType.HUB,
    }
)
# A candidate must clear this BM25 relevance score to count as a likely
# duplicate. Tuned to flag clear title/content overlap without crying wolf on
# every loosely-related note; callers can always bypass with check_duplicates.
_DEDUP_MIN_SCORE = 1.5
_DEDUP_MAX_CANDIDATES = 5

# Cosine-similarity bands for the verdict line appended to semantic results.
# Empirically calibrated on this vault: a high score means same TOPIC (not
# necessarily same claim), and a low top score is a reliable novelty signal.
_SIM_STRONG = 0.80
_SIM_MODERATE = 0.60

# Note IDs are timestamp-shaped; used to give targeted guidance when a lookup
# by a fabricated/typo'd ID fails.
_NOTE_ID_SHAPE_RE = re.compile(r"^\d{8}T\d+$")


def _strip_links_section(content: str) -> Tuple[str, int]:
    """Remove the materialized ``## Links`` section from note content for display.

    Note bodies carry a ``## Links`` section that mirrors the link graph (one
    line per edge). For hub/area notes that is one line per member — hundreds of
    lines that bloat context every time the note is read. This drops that section
    (heading through the next ``## `` heading or EOF) and returns
    ``(stripped_content, hidden_link_count)``. Inline ``[[id]]`` references in
    prose are left untouched — only the structured section is removed.
    """
    lines = content.splitlines()
    out: List[str] = []
    hidden = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip().lower() == "## links":
            j = i + 1
            while j < n and not lines[j].lstrip().startswith("## "):
                if lines[j].strip().startswith("- "):
                    hidden += 1
                j += 1
            # Drop any blank lines we already emitted just before the heading so
            # the body doesn't end with a dangling gap.
            while out and out[-1].strip() == "":
                out.pop()
            i = j
            continue
        out.append(line)
        i += 1
    stripped = "\n".join(out).rstrip()
    if stripped:
        stripped += "\n"
    return stripped, hidden

# Operating manual injected into every MCP client session. This is the one
# piece of documentation the calling model is guaranteed to see, so the
# empirically-derived usage rules live here rather than only in the README.
_SERVER_INSTRUCTIONS = """\
Parazettel is a Zettelkasten + PARA/GTD vault used as persistent AI memory.

Operating rules (empirically calibrated on this vault):
- Phrase semantic queries as a complete claim or sentence, NOT keywords — a
  full-claim query can outrank a terse one by two orders of magnitude of rank.
  Use pzk_find_similar_to_text for meaning-based recall and pre-create dedup;
  use pzk_search_notes for lexical/filtered search.
- Score calibration: a low top similarity (< ~0.6) reliably means the idea is
  NOVEL to the vault. A high similarity means same TOPIC — open the note and
  confirm it is the same atomic claim before treating it as a duplicate.
- Never fabricate note IDs. Copy them exactly from tool output, or pass the
  note title instead — most lookup tools accept either.
- Reuse existing tags before minting new ones: pzk_suggest_tags(text) returns
  the closest existing tags by meaning (pzk_get_all_tags lists every tag). Tags
  are normalized to lowercase-hyphenated form on write. pzk_suggest_areas(text)
  likewise shortlists the area to route a note under.
- Start a work session with pzk_briefing (active projects, due tasks,
  reminders, recent notes) so the vault is consulted before new work begins.
- For multi-note captures (ingesting a transcript or document), use
  pzk_ingest_batch — notes, links, and tasks in one call — instead of many
  individual create calls.
- Editing the "## Links" section inside note content via pzk_update_note IS
  honored (entries are reconciled into the link graph). Inline [[id]] refs in
  prose are indexed automatically and cleaned up on delete/rename.
- pzk_get_note omits a note's "## Links" section by default (it is large for
  area/hub notes); explore links with pzk_get_linked_notes / pzk_get_neighborhood,
  or pass include_links=true.
"""


class BackendBundle(Protocol):
    """Service bundle contract for direct and daemon-backed MCP execution."""

    zettel_service: Any
    search_service: Any

    def initialize(self) -> None: ...
    def close(self) -> None: ...


class DirectBackendBundle:
    """Direct in-process service bundle."""

    def __init__(self, zettel_service: ZettelService, search_service: SearchService):
        self.zettel_service = zettel_service
        self.search_service = search_service

    def initialize(self) -> None:
        self.zettel_service.initialize()
        self.search_service.initialize()

    def close(self) -> None:
        self.zettel_service.close()


class DaemonBackendBundle:
    """Thin MCP bundle that proxies service calls through the local daemon."""

    def __init__(self, base_url: str):
        self.health_client = DaemonRpcClient(
            base_url, timeout_seconds=_DAEMON_HEALTH_TIMEOUT_SECONDS
        )
        self.rpc_client = DaemonRpcClient(
            base_url, timeout_seconds=config.daemon_rpc_timeout_seconds
        )
        self.zettel_service = RemoteServiceProxy(self.rpc_client, "zettel_service")
        self.search_service = RemoteServiceProxy(self.rpc_client, "search_service")

    def initialize(self) -> None:
        self.health_client.health()

    def close(self) -> None:
        return None


class ZettelkastenMcpServer:
    """MCP server for Zettelkasten."""

    def __init__(self):
        """Initialize the MCP server."""
        self.mcp = FastMCP(
            config.server_name,
            instructions=_SERVER_INSTRUCTIONS,
            version=config.server_version,
            host=config.server_host,
            port=config.server_port,
        )
        self.backend = self._build_backend()
        self.zettel_service = self.backend.zettel_service
        self.search_service = self.backend.search_service
        # Optional cross-encoder that confirms dedup-on-create candidates. Built
        # cheaply here (the model is loaded lazily on first use); None when the
        # feature is off, in which case dedup uses the BM25 prefilter alone.
        self._reranker = build_reranker(config)
        # Initialize services
        self.initialize()
        # Register tools
        self._register_tools()
        self._register_resources()
        self._register_prompts()

    def _build_backend(self) -> BackendBundle:
        """Build direct or daemon-backed services based on config."""
        if config.backend_mode == "daemon":
            return DaemonBackendBundle(config.get_daemon_base_url())
        zettel_service = ZettelService()
        search_service = SearchService(zettel_service)
        return DirectBackendBundle(zettel_service, search_service)

    def initialize(self) -> None:
        """Initialize services."""
        self.backend.initialize()
        logger.info("Zettelkasten MCP server initialized")

    def close(self) -> None:
        """Release resources held by the MCP server."""
        self.backend.close()

    def format_error_response(self, error: Exception) -> str:
        """Format an error response in a consistent way.

        Args:
            error: The exception that occurred

        Returns:
            Formatted error message with appropriate level of detail
        """
        # Generate a unique error ID for traceability in logs
        error_id = str(uuid.uuid4())[:8]

        if isinstance(error, ValueError):
            # Domain validation errors - typically safe to show to users
            logger.error(f"Validation error [{error_id}]: {str(error)}")
            return f"Error: {str(error)}"
        elif isinstance(error, DaemonUnavailableError):
            logger.error(f"Daemon connectivity error [{error_id}]: {str(error)}")
            return (
                f"Error: {str(error)}\n"
                f"Restart it for this vault (preserves embeddings) with: "
                f"{config.format_daemon_restart_command()}"
            )
        elif isinstance(error, GraphDatabaseReadOnlyError):
            # Read-only fallback errors are safe and actionable for users
            logger.error(f"Read-only graph error [{error_id}]: {str(error)}")
            return f"Error: {str(error)}"
        elif isinstance(error, RerankerError):
            # The dedup reranker stalled or failed. Its message is controlled and
            # actionable (it names the fix), so surface it verbatim rather than a
            # generic error — this is the loud failure we deliberately chose over a
            # silent BM25 fallback so a stuck reranker is visible, not invisible.
            logger.error(f"Dedup reranker error [{error_id}]: {str(error)}")
            return f"Error: {str(error)}"
        elif isinstance(error, (IOError, OSError)):
            # File system errors - don't expose paths or detailed error messages
            logger.error(f"File system error [{error_id}]: {str(error)}", exc_info=True)
            return f"Unable to access the requested resource. Error ID: {error_id}"
        else:
            # Unexpected errors - log with full stack trace but return generic message
            logger.error(f"Unexpected error [{error_id}]: {str(error)}", exc_info=True)
            return f"An unexpected error occurred. Error ID: {error_id}"

    def _find_duplicate_candidates(
        self, title: str, content: str
    ) -> List[Tuple[Note, float]]:
        """Return existing notes that look like near-duplicates of (title, content).

        Uses the same BM25 text search as pzk_search_notes. The title is the
        strongest dedup signal, so it leads the query, with a little content
        context appended. The BM25 SEARCH is best-effort (a search failure yields
        no candidates so a flaky search never blocks creation); the cross-encoder
        rerank confirm, however, surfaces its errors (see :meth:`_rerank_confirm`)
        — a stuck/failed reranker fails the create loudly rather than silently
        degrading dedup.
        """
        query = title.strip()
        if content:
            # Slice before strip so a large note body isn't fully stripped just
            # to take a 200-char lead; only append when the slice is non-empty.
            content_lead = content[:200].strip()
            if content_lead:
                query = f"{query} {content_lead}"
        if not query.strip():
            return []
        search_started = time.perf_counter()
        try:
            results = self.search_service.search_combined(text=query)
        except Exception as exc:  # pragma: no cover - dedup is advisory only
            logger.warning("Duplicate check skipped (search failed): %s", exc)
            return []
        logger.debug(
            "dedup probe: BM25 search returned in %.2fs for query lead %r",
            time.perf_counter() - search_started,
            query[:80],
        )

        candidates: List[Tuple[Note, float]] = []
        for result in results:
            score = result.score
            if not isinstance(score, (int, float)):
                continue
            if score < _DEDUP_MIN_SCORE:
                continue
            # Only knowledge notes are duplicate candidates — an unrelated task,
            # project, or area match must never block creating a knowledge note.
            if result.note.note_type not in _DEDUP_NOTE_TYPES:
                continue
            candidates.append((result.note, float(score)))
            if len(candidates) >= _DEDUP_MAX_CANDIDATES:
                break
        return self._rerank_confirm(title, content, candidates)

    @staticmethod
    def _dedup_text(title: str, content: str) -> str:
        """Combine title + a content lead into the text the reranker compares."""
        # Slice before strip so a large note body isn't fully stripped just to
        # take a 600-char lead (mirrors _find_duplicate_candidates' content lead).
        body = (content or "")[:600].strip()
        return f"{(title or '').strip()}\n{body}".strip()

    def _rerank_confirm(
        self, title: str, content: str, candidates: List[Tuple[Note, float]]
    ) -> List[Tuple[Note, float]]:
        """Keep only BM25 candidates a cross-encoder confirms as true duplicates.

        BM25 over-flags on shared vocabulary; the reranker reads both notes
        together and is far more precise, so it drops topically-adjacent-but-
        distinct false positives.

        A reranker LOAD/SCORE failure (e.g. a wedged model-cache lock that hits
        the load timeout) is surfaced, NOT swallowed: we deliberately do not fall
        back to BM25-only here. A silent fallback hides a broken dedup probe and
        was indistinguishable from a hang during debugging; failing loudly makes
        the problem visible and points at the fix (decision 2026-06-17). Only the
        benign shape guards below (wrong score count / non-numeric score) keep the
        BM25 candidates, since there the reranker ran but returned something
        unusable — that is a quirk, not a stall.
        """
        if not candidates or self._reranker is None:
            return candidates
        query = self._dedup_text(title, content)
        documents = [
            self._dedup_text(note.title, note.content) for note, _ in candidates
        ]
        logger.debug(
            "dedup rerank: confirming %d BM25 candidate(s) with %s",
            len(documents),
            getattr(self._reranker, "model_id", "reranker"),
        )
        started = time.perf_counter()
        try:
            scores = self._reranker.score(query, documents)
        except RerankerError:
            logger.error(
                "dedup rerank FAILED after %.1fs — surfacing error (no BM25 "
                "fallback)",
                time.perf_counter() - started,
                exc_info=True,
            )
            raise
        except Exception as exc:
            logger.error(
                "dedup rerank FAILED unexpectedly after %.1fs: %s — surfacing error",
                time.perf_counter() - started,
                exc,
                exc_info=True,
            )
            raise RerankerError(f"dedup reranker failed: {exc}") from exc
        if len(scores) != len(candidates):
            logger.warning(
                "Dedup rerank skipped (unexpected score count): expected %d, got %d",
                len(candidates),
                len(scores),
            )
            return candidates
        threshold = config.dedup_rerank_min_score
        confirmed: List[Tuple[Note, float]] = []
        for (note, bm25), rerank_score in zip(candidates, scores):
            if not isinstance(rerank_score, (int, float)):
                # A non-numeric score would crash the threshold compare; stay
                # best-effort and fall back to the BM25 candidates.
                logger.warning("Dedup rerank skipped (non-numeric score %r)", rerank_score)
                return candidates
            if rerank_score >= threshold:
                confirmed.append((note, bm25))
        logger.debug(
            "dedup rerank: confirmed %d/%d candidate(s) as duplicates in %.2fs "
            "(threshold=%.1f)",
            len(confirmed),
            len(candidates),
            time.perf_counter() - started,
            threshold,
        )
        return confirmed

    @staticmethod
    def _format_duplicate_warning(
        title: str, duplicates: List[Tuple[Note, float]]
    ) -> str:
        """Render the 'possible duplicates found, not created' response."""
        lines = [
            f'Not created: "{title}" looks similar to {len(duplicates)} existing '
            f"note(s). Review them before adding a near-duplicate:",
            "",
        ]
        for i, (note, score) in enumerate(duplicates, 1):
            # Slice before normalizing so we don't process a huge note body just
            # to truncate it to 160 chars. A little headroom (200) covers any
            # whitespace collapsed out of the lead.
            snippet = note.content[:200].replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:160] + "..."
            lines.append(f"{i}. {note.title} (ID: {note.id})")
            lines.append(f"   Relevance: {score:.3f}")
            if note.tags:
                lines.append(
                    f"   Tags: {', '.join(tag.name for tag in note.tags)}"
                )
            lines.append(f"   Preview: {snippet}")
            lines.append("")
        lines.append(
            "If one of these is the same idea, update it with pzk_update_note "
            "instead of creating a new note. If this note is genuinely distinct, "
            "call pzk_create_note again with check_duplicates=false to create it."
        )
        return "\n".join(lines)

    @staticmethod
    def _format_note_result(note: Note, include_links: bool = False) -> str:
        """Render a note using the standard MCP note output.

        The body's ``## Links`` section is omitted by default: it mirrors the link
        graph and is one line per edge, which for hub/area notes is hundreds of
        lines that bloat context on every read. Explore links with
        pzk_get_linked_notes / pzk_get_neighborhood, or pass ``include_links=True``
        to inline them.
        """
        result = f"ID: {note.id}\n"
        result += f"Type: {note.note_type.value}\n"
        result += f"Created: {note.created_at.isoformat()}\n"
        result += f"Updated: {note.updated_at.isoformat()}\n"
        if note.project_id:
            result += f"Project ID: {note.project_id}\n"
        if note.area_id:
            result += f"Area ID: {note.area_id}\n"
        if note.tags:
            result += f"Tags: {', '.join(tag.name for tag in note.tags)}\n"
        body = note.content
        hidden = 0
        if not include_links:
            body, hidden = _strip_links_section(body)
        result += f"\n{body}\n"
        if hidden:
            result += (
                f"\n[{hidden} link(s) hidden — explore with pzk_get_linked_notes / "
                "pzk_get_neighborhood, or re-fetch with include_links=true]\n"
            )
        return result

    def _resolve_note_identifier(self, identifier: str) -> Optional[Note]:
        """Resolve a note by ID first, then by title."""
        normalized = str(identifier).strip()
        if not normalized:
            return None
        note = self.zettel_service.get_note(normalized)
        if note:
            return note
        return self.zettel_service.get_note_by_title(normalized)

    def _not_found_message(self, identifier: str) -> str:
        """Build a prescriptive not-found error with recovery guidance.

        Fabricated/typo'd IDs are a known failure mode for LLM callers, so the
        error teaches the fix instead of just reporting the miss: id-shaped
        identifiers get the never-guess-IDs rule; title-like identifiers get
        the closest fuzzy matches so the retry can succeed in one shot.
        """
        identifier = str(identifier).strip()
        msg = f"Note not found: {identifier}"
        if _NOTE_ID_SHAPE_RE.match(identifier):
            msg += (
                "\nThis looks like a note ID that does not exist. IDs must be "
                "copied exactly from prior tool output — never constructed or "
                "guessed from timestamps. If you know the title, pass it "
                "instead, or locate the note with pzk_search_notes."
            )
            return msg
        try:
            results = list(self.search_service.search_combined(text=identifier))[:3]
            if results:
                msg += "\nClosest matches:\n" + "\n".join(
                    f"- {r.note.title} (ID: {r.note.id})" for r in results
                )
                msg += "\nIf one of these is the intended note, retry with its ID."
        except Exception as exc:  # pragma: no cover - suggestions are best-effort
            logger.debug(
                "Could not compute not-found suggestions for %r: %s",
                identifier,
                exc,
            )
        return msg

    @staticmethod
    def _semantic_verdict(top_similarity: float) -> str:
        """One-line interpretation of a semantic-similarity result set.

        Bakes the vault's score calibration into the tool output so every
        caller (including weaker models) gets the interpretation for free
        instead of re-deriving what the raw numbers mean.
        """
        if top_similarity >= _SIM_STRONG:
            return (
                "Verdict: strong match — same topic is certain, but open the "
                "top note and confirm it is the same atomic CLAIM before "
                "folding/deduping; dense clusters score high on distinct atoms."
            )
        if top_similarity >= _SIM_MODERATE:
            return (
                "Verdict: moderate matches — treat these as link candidates, "
                "not duplicates."
            )
        return (
            "Verdict: weak matches only — this content is likely novel to the "
            "vault (a calibrated-low top score is a reliable novelty signal). "
            "Safe to create as a new note."
        )

    @staticmethod
    def _lexical_verdict(results: List[Any]) -> str:
        """One-line interpretation of a BM25/hybrid text-search result set."""
        top = 0.0
        for result in results:
            if isinstance(result.score, (int, float)):
                top = max(top, float(result.score))
        if top >= _DEDUP_MIN_SCORE:
            return (
                "Verdict: strong lexical match present (top BM25 "
                f"{top:.2f}) — review the top results before creating "
                "overlapping content."
            )
        if top > 0.0:
            return (
                f"Verdict: moderate lexical matches (top BM25 {top:.2f}). For "
                "meaning-based recall, also try pzk_find_similar_to_text with "
                "a full-claim query."
            )
        return (
            "Verdict: no lexical match — any results above are semantic-only "
            "hits. Confirm novelty with pzk_find_similar_to_text before "
            "concluding the vault has nothing on this."
        )

    @staticmethod
    def _format_note_summary(note: Note) -> str:
        """Compact one-result rendering used by detail='summary' outputs."""
        line = f"- {note.title} (ID: {note.id}, type: {note.note_type.value}"
        if note.status:
            line += f", status: {note.status.value}"
        line += ")"
        if note.tags:
            line += f"\n  Tags: {', '.join(tag.name for tag in note.tags)}"
        preview = note.content[:200].replace("\n", " ").strip()
        if len(preview) > 150:
            preview = preview[:150] + "..."
        if preview:
            line += f"\n  Preview: {preview}"
        return line

    def _render_notes_with_detail(
        self, notes: List[Note], detail: str, include_links: bool = False
    ) -> str:
        """Render a note list at the requested detail level (ids/summary/full)."""
        if detail == "ids":
            return "\n".join(f"- {note.title} (ID: {note.id})" for note in notes)
        if detail == "summary":
            return "\n".join(self._format_note_summary(note) for note in notes)
        return "\n---\n\n".join(
            self._format_note_result(note, include_links) for note in notes
        )

    @staticmethod
    def _truncation_notice(shown: int, total: int, hint: str = "") -> str:
        """Explicit more-results line so truncation is never silent."""
        if total <= shown:
            return ""
        notice = f"\n({total - shown} more result(s) not shown"
        if hint:
            notice += f" — {hint}"
        return notice + ")\n"

    @staticmethod
    def _describe_relation(node: Note, neighbor: Note) -> str:
        """Short label for how *neighbor* connects to *node* (direction + type)."""
        for link in node.links:
            if link.target_id == neighbor.id:
                return f"-> {link.link_type.value}"
        for link in neighbor.links:
            if link.target_id == node.id:
                return f"<- {link.link_type.value}"
        if neighbor.id in getattr(node, "inline_refs", []):
            return "-> inline"
        if node.id in getattr(neighbor, "inline_refs", []):
            return "<- inline"
        return "linked"

    def _suggested_links_message(self, note_id: str) -> str:
        """Propose link candidates for a freshly created note (best-effort).

        Runs the same hybrid similarity the skills use manually, so every
        create — from any client — gets connection candidates without an extra
        round-trip. Failures are swallowed; suggestions must never break create.
        """
        try:
            similar = self.zettel_service.find_similar_notes(str(note_id), 0.35)
            similar = [(n, s) for n, s in similar if n.id != note_id][:3]
            if not similar:
                return ""
            lines = [
                "",
                "Suggested links (semantic neighbors — link with "
                "pzk_create_link if related; moderate scores are link "
                "candidates, not duplicates):",
            ]
            for note, score in similar:
                lines.append(
                    f"- {note.title} (ID: {note.id}, similarity {score:.2f})"
                )
            return "\n".join(lines)
        except Exception:  # pragma: no cover - advisory only
            return ""

    @staticmethod
    def _normalize_identifier_list(identifiers: List[str]) -> List[str]:
        """Strip empty values and de-duplicate while preserving order."""
        seen = set()
        normalized: List[str] = []
        for identifier in identifiers:
            value = str(identifier).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _get_project_preview_tasks(
        tasks: List[Note], limit: int = 5
    ) -> List[Note]:
        """Return the most actionable project tasks for summary views."""
        actionable_statuses = {NoteStatus.ACTIVE, NoteStatus.READY}
        status_order = {NoteStatus.ACTIVE: 0, NoteStatus.READY: 1}
        far_future = datetime.max.date()
        actionable = [task for task in tasks if task.status in actionable_statuses]
        return sorted(
            actionable,
            key=lambda task: (
                status_order.get(task.status, 99),
                task.due_date or far_future,
                -(task.priority or 0),
                task.title.lower(),
            ),
        )[:limit]

    @staticmethod
    def _format_project_preview_task(task: Note) -> str:
        """Render one actionable task in a compact, parseable summary format."""
        line = f"- [{task.status.value if task.status else 'none'}] {task.title} (ID: {task.id})"
        details = []
        if task.priority:
            details.append(f"P{task.priority}")
        if task.due_date:
            details.append(f"due {task.due_date}")
        if details:
            line += " - " + ", ".join(details)
        return line

    def _register_tools(self) -> None:
        """Register MCP tools."""

        # Create a new note
        @self.mcp.tool(name="pzk_create_note")
        def pzk_create_note(
            title: str,
            content: str,
            note_type: str = "permanent",
            tags: Optional[str] = None,
            source: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            area_id: Optional[str] = None,
            origin: Optional[str] = None,
            check_duplicates: bool = True,
        ) -> str:
            """Create a new Zettelkasten note.

            Creating many notes at once (e.g. ingesting a transcript)? Use
            pzk_ingest_batch instead — notes, links, and tasks in one call.
            Reuse existing tags (pzk_suggest_tags shortlists the closest by
            meaning; pzk_get_all_tags lists them all) before minting new ones;
            tags are normalized to lowercase-hyphenated form.

            Args:
                title: The title of the note
                content: The main content of the note
                note_type: Type of note. Knowledge types: fleeting, literature, permanent,
                    structure, hub. Action-item types: task, project, area.
                    For tasks prefer pzk_create_task which exposes task-specific fields.
                tags: Comma-separated list of tags (optional)
                source: Origin of the note. Required for all note types except area.
                status: Optional workflow status such as inbox, evergreen, or archived.
                project_id: Optional project to route the note under; inherits the project's area.
                area_id: ID of the area this note belongs to when project_id is not provided.
                origin: Fine-grained provenance — the URL, chat/session id, file path,
                    or meeting the note came from (optional but valuable for trust).
                check_duplicates: When True (default), search for existing notes that look
                    like near-duplicates of this title/content BEFORE creating. If strong
                    matches are found, the note is NOT created and the matches are returned
                    for you to review — then either update the existing note with
                    pzk_update_note, or call pzk_create_note again with check_duplicates=false
                    to create it anyway. Set to false to skip the check.
            """
            try:
                # Validate title up front so an empty/whitespace-only title
                # returns the expected validation error rather than being routed
                # into the duplicate-check path (which runs before create_note).
                if not title or not title.strip():
                    return "Error: title is required."

                # Convert note_type string to enum
                try:
                    note_type_enum = NoteType(note_type.lower())
                except ValueError:
                    return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                # Convert tags string to list
                tag_list = []
                if tags:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                note_source = NoteSource.MANUAL
                if source:
                    try:
                        note_source = NoteSource(source.lower())
                    except ValueError:
                        return (
                            f"Invalid source: {source}. "
                            f"Valid: {', '.join(s.value for s in NoteSource)}"
                        )
                elif note_type_enum != NoteType.AREA:
                    return (
                        "source is required for all note types except area. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )

                note_status = None
                if status is not None:
                    normalized_status = status.strip().lower()
                    if normalized_status:
                        try:
                            note_status = NoteStatus(normalized_status)
                        except ValueError:
                            return (
                                f"Invalid status: {status}. "
                                f"Valid: {', '.join(s.value for s in NoteStatus)}"
                            )

                resolved_area_id = area_id
                if note_type_enum == NoteType.AREA:
                    if project_id:
                        return "Area notes cannot specify project_id."
                    if area_id:
                        return (
                            "Area notes assign their own area_id automatically. "
                            "Do not pass area_id."
                        )
                else:
                    if not project_id and not area_id:
                        return (
                            "area_id or project_id is required for all note types "
                            "except area."
                        )
                    if project_id:
                        project = self.zettel_service.get_note(project_id)
                        if not project or project.note_type != NoteType.PROJECT:
                            return (
                                f"project_id {project_id} is not a valid project note."
                            )
                        if not project.area_id:
                            return (
                                f"project_id {project_id} does not have an area_id."
                            )
                        if area_id and area_id != project.area_id:
                            return (
                                f"area_id {area_id} does not match project "
                                f"{project_id} area_id {project.area_id}."
                            )
                        resolved_area_id = project.area_id
                    if resolved_area_id:
                        area = self.zettel_service.get_note(resolved_area_id)
                        if not area or area.note_type != NoteType.AREA:
                            return (
                                f"area_id {resolved_area_id} is not a valid area note."
                            )

                # Before creating, surface likely duplicates so the caller can
                # decide to reuse/update an existing note instead of silently
                # accreting a near-identical one. Knowledge notes only — area /
                # project / task are structural/action-item types handled by their
                # own create tools.
                if check_duplicates and note_type_enum in _DEDUP_NOTE_TYPES:
                    duplicates = self._find_duplicate_candidates(title, content)
                    if duplicates:
                        return self._format_duplicate_warning(title, duplicates)

                # Create the note
                note = self.zettel_service.create_note(
                    title=title,
                    content=content,
                    note_type=note_type_enum,
                    tags=tag_list,
                    source=note_source,
                    status=note_status,
                    project_id=project_id,
                    area_id=resolved_area_id,
                    origin=origin,
                )
                out = f"Note created successfully with ID: {note.id}"
                if note_type_enum in _DEDUP_NOTE_TYPES:
                    out += self._suggested_links_message(note.id)
                return out
            except Exception as e:
                return self.format_error_response(e)

        # Get a note by ID or title
        @self.mcp.tool(name="pzk_get_note")
        def pzk_get_note(identifier: str, include_links: bool = False) -> str:
            """Retrieve a note by ID or title.

            The note's ``## Links`` section is omitted by default — it mirrors the
            link graph and is huge for hub/area notes (one line per member). To see
            a note's links, use pzk_get_linked_notes or pzk_get_neighborhood, or
            pass include_links=True here.

            Args:
                identifier: The ID or title of the note (IDs must be copied
                    from prior tool output, never guessed)
                include_links: Inline the note's ## Links section in the body
                    (default False to keep area/hub reads small)
            """
            try:
                identifier = str(identifier)
                note = self._resolve_note_identifier(identifier)
                if not note:
                    return self._not_found_message(identifier)
                # Track explicit retrieval (recency/frequency signals for
                # future recall ranking). Best-effort; never blocks the read.
                self.zettel_service.record_retrieval([note.id])
                return self._format_note_result(note, bool(include_links))
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_notes")
        def pzk_get_notes(
            identifiers: List[str],
            detail: str = "full",
            include_links: bool = False,
        ) -> str:
            """Retrieve multiple notes by ID or title in one call.
            Args:
                identifiers: Note IDs or titles to retrieve
                detail: Output detail — 'full' (default, complete content),
                    'summary' (title/tags/preview), or 'ids' (titles + IDs only).
                    Prefer 'summary' when skimming many notes to save context.
                include_links: With detail='full', inline each note's ## Links
                    section (default False to keep area/hub reads small).
            """
            try:
                detail = str(detail).strip().lower()
                if detail not in {"full", "summary", "ids"}:
                    return "Invalid detail: use 'full', 'summary', or 'ids'."
                normalized = self._normalize_identifier_list(identifiers)
                if not normalized:
                    return "Provide at least one note identifier."

                notes: List[Note] = []
                seen_note_ids = set()
                missing: List[str] = []
                for identifier in normalized:
                    note = self._resolve_note_identifier(identifier)
                    if note:
                        if note.id not in seen_note_ids:
                            notes.append(note)
                            seen_note_ids.add(note.id)
                    else:
                        missing.append(identifier)

                if not notes:
                    out = "No notes found for the provided identifiers."
                    if missing:
                        out += "\n\nMissing identifiers:\n"
                        out += "\n".join(
                            f"- {self._not_found_message(identifier)}"
                            for identifier in missing
                        )
                    return out

                self.zettel_service.record_retrieval([n.id for n in notes])
                out = f"Notes retrieved ({len(notes)}/{len(normalized)}):\n\n"
                out += self._render_notes_with_detail(
                    notes, detail, bool(include_links)
                )
                if missing:
                    out += "\n\nMissing identifiers:\n"
                    out += "\n".join(f"- {identifier}" for identifier in missing)
                    out += (
                        "\n(IDs must be copied from prior tool output; titles "
                        "also work.)"
                    )
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_notes_by_tag")
        def pzk_get_notes_by_tag(
            tag: str, limit: int = 50, detail: str = "full"
        ) -> str:
            """Retrieve notes with an exact tag match.
            Args:
                tag: Tag name to retrieve
                limit: Maximum results
                detail: Output detail — 'full' (default), 'summary', or 'ids'.
                    Prefer 'summary' when skimming a large tag to save context.
            """
            try:
                detail = str(detail).strip().lower()
                if detail not in {"full", "summary", "ids"}:
                    return "Invalid detail: use 'full', 'summary', or 'ids'."
                normalized_tag = str(tag).strip()
                if not normalized_tag:
                    return "Provide a tag name."
                if limit <= 0:
                    return "Limit must be greater than 0."

                all_notes = self.zettel_service.get_notes_by_tag(normalized_tag)
                notes = all_notes[:limit]
                if not notes:
                    return f"No notes found with tag '{normalized_tag}'."

                # Record retrieval only when full content is actually returned;
                # an ids/summary browse isn't a deliberate read.
                if detail == "full":
                    self.zettel_service.record_retrieval([n.id for n in notes])
                out = f"Notes tagged '{normalized_tag}' ({len(notes)}):\n\n"
                out += self._render_notes_with_detail(notes, detail)
                out += self._truncation_notice(
                    len(notes), len(all_notes), "raise limit to see the rest"
                )
                return out
            except Exception as e:
                return self.format_error_response(e)

        # Update a note
        @self.mcp.tool(name="pzk_update_note")
        def pzk_update_note(
            note_id: str,
            title: Optional[str] = None,
            content: Optional[str] = None,
            note_type: Optional[str] = None,
            tags: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            area_id: Optional[str] = None,
            origin: Optional[str] = None,
            mark_verified: Optional[bool] = None,
        ) -> str:
            """Update an existing note.

            Link editing via content IS honored: if the new content includes a
            "## Links" section, its entries are reconciled into the link graph
            (removed lines unlink, added lines link). Content WITHOUT a
            "## Links" heading leaves the note's links untouched.

            Args:
                note_id: The ID of the note to update
                title: New title (optional)
                content: New content (optional)
                note_type: New note type (optional)
                tags: New comma-separated list of tags (optional)
                status: New workflow status (optional). Pass empty string to clear it.
                project_id: New project routing (optional). Pass empty string to clear it.
                parent_project_id: New parent project / project routing (optional). Pass empty string to clear it.
                area_id: New area routing (optional). Pass empty string to clear it.
                origin: New provenance string (URL, chat id, file). Pass empty string to clear it.
                mark_verified: True records today as the date this note's claim
                    was last confirmed still true (last_verified); False clears it.
            """
            try:
                # Get the note
                note = self.zettel_service.get_note(str(note_id))
                if not note:
                    return self._not_found_message(note_id)

                # Convert note_type string to enum if provided
                note_type_enum = None
                if note_type:
                    try:
                        note_type_enum = NoteType(note_type.lower())
                    except ValueError:
                        return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                # Convert tags string to list if provided
                tag_list = None
                if tags is not None:  # Allow empty string to clear tags
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                update_kwargs = {
                    "note_id": note_id,
                    "title": title,
                    "content": content,
                    "note_type": note_type_enum,
                    "tags": tag_list,
                }
                if status is not None:
                    normalized_status = status.strip().lower()
                    if normalized_status:
                        try:
                            update_kwargs["status"] = NoteStatus(normalized_status)
                        except ValueError:
                            return (
                                f"Invalid status: {status}. "
                                f"Valid: {', '.join(s.value for s in NoteStatus)}"
                            )
                    else:
                        update_kwargs["status"] = None
                if parent_project_id is not None:
                    normalized_parent_project_id = parent_project_id.strip()
                    if project_id is not None:
                        normalized_project_id = project_id.strip()
                        if normalized_project_id != normalized_parent_project_id:
                            return (
                                "project_id and parent_project_id must match when "
                                "both are provided."
                            )
                    update_kwargs["project_id"] = (
                        normalized_parent_project_id or None
                    )
                if project_id is not None:
                    normalized_project_id = project_id.strip()
                    update_kwargs.setdefault(
                        "project_id", normalized_project_id or None
                    )
                if area_id is not None:
                    normalized_area_id = area_id.strip()
                    update_kwargs["area_id"] = normalized_area_id or None
                if origin is not None:
                    update_kwargs["origin"] = origin.strip() or None
                if mark_verified is not None:
                    import datetime as _dt

                    update_kwargs["last_verified"] = (
                        _dt.date.today() if mark_verified else None
                    )

                # Update the note
                updated_note = self.zettel_service.update_note(**update_kwargs)
                return f"Note updated successfully: {updated_note.id}"
            except Exception as e:
                return self.format_error_response(e)

        # Delete a note
        @self.mcp.tool(name="pzk_delete_note")
        def pzk_delete_note(note_id: str) -> str:
            """Delete a note.
            Args:
                note_id: The ID of the note to delete
            """
            try:
                # Check if note exists
                note = self.zettel_service.get_note(note_id)
                if not note:
                    return self._not_found_message(note_id)

                # Delete the note
                self.zettel_service.delete_note(str(note_id))
                return (
                    f"Note deleted successfully: {note_id}\n"
                    "Incoming references were scrubbed: ## Links entries "
                    "removed and inline [[wiki-links]] in prose unlinked."
                )
            except Exception as e:
                return self.format_error_response(e)

        # Add a link between notes
        @self.mcp.tool(name="pzk_create_link")
        def pzk_create_link(
            source_id: str,
            target_id: str,
            link_type: str = "reference",
            description: Optional[str] = None,
            bidirectional: bool = False,
        ) -> str:
            """Create a link between two notes.

            For many links at once (e.g. wiring up a fresh ingestion), use
            pzk_ingest_batch with a links list instead of repeated calls.

            Args:
                source_id: ID of the source note
                target_id: ID of the target note
                link_type: Type of link (reference, extends, refines, contradicts, questions, supports, related)
                description: Optional description of the link
                bidirectional: Whether to create a link in both directions
            """
            try:
                # Convert link_type string to enum
                try:
                    source_id_str = str(source_id)
                    target_id_str = str(target_id)
                    link_type_enum = LinkType(link_type.lower())
                except ValueError:
                    valid = ", ".join(
                        t.value for t in LinkType if t != LinkType.INLINE
                    )
                    return f"Invalid link type: {link_type}. Valid types are: {valid}"
                if link_type_enum == LinkType.INLINE:
                    return (
                        "'inline' links are derived from [[wiki-links]] in note "
                        "prose and cannot be created directly. Use an explicit "
                        "type (reference, extends, supports, ...) or edit the "
                        "note content."
                    )
                # Validate both endpoints up front so a bad ID gets recovery
                # guidance instead of a bare not-found error.
                if not self.zettel_service.get_note(source_id_str):
                    return self._not_found_message(source_id_str)
                if not self.zettel_service.get_note(target_id_str):
                    return self._not_found_message(target_id_str)

                # Create the link
                source_note, target_note = self.zettel_service.create_link(
                    source_id=source_id,
                    target_id=target_id,
                    link_type=link_type_enum,
                    description=description,
                    bidirectional=bidirectional,
                )
                if bidirectional:
                    return f"Bidirectional link created between {source_id} and {target_id}"
                else:
                    return f"Link created from {source_id} to {target_id}"
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    return "A link of this type already exists between these notes. Try a different link type."
                return self.format_error_response(e)

        self.pzk_create_link = pzk_create_link

        # Remove a link between notes
        @self.mcp.tool(name="pzk_remove_link")
        def pzk_remove_link(
            source_id: str, target_id: str, bidirectional: bool = False
        ) -> str:
            """Remove a link between two notes.
            Args:
                source_id: ID of the source note
                target_id: ID of the target note
                bidirectional: Whether to remove the link in both directions
            """
            try:
                # Remove the link
                source_note, target_note = self.zettel_service.remove_link(
                    source_id=str(source_id),
                    target_id=str(target_id),
                    bidirectional=bidirectional,
                )
                if bidirectional:
                    return f"Bidirectional link removed between {source_id} and {target_id}"
                else:
                    return f"Link removed from {source_id} to {target_id}"
            except Exception as e:
                return self.format_error_response(e)

        # Search for notes
        @self.mcp.tool(name="pzk_search_notes")
        def pzk_search_notes(
            query: Optional[str] = None,
            tags: Optional[str] = None,
            note_type: Optional[str] = None,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            area_id: Optional[str] = None,
            limit: int = 10,
        ) -> str:
            """Search for notes by text, tags, type, status, or PARA routing fields.

            Lexical-first (BM25 + semantic blend). For pure meaning-based recall
            or pre-create dedup, prefer pzk_find_similar_to_text and phrase the
            query as a complete claim, not keywords.

            Args:
                query: Text to search for in titles and content
                tags: Comma-separated list of tags to filter by
                note_type: Type of note to filter by
                status: Filter by workflow status
                project_id: Filter to notes routed to this project
                area_id: Filter to notes routed to this area
                limit: Maximum number of results to return
            """
            try:
                # Convert tags string to list if provided
                tag_list = None
                if tags:
                    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

                # Convert note_type string to enum if provided
                note_type_enum = None
                if note_type:
                    try:
                        note_type_enum = NoteType(note_type.lower())
                    except ValueError:
                        return f"Invalid note type: {note_type}. Valid types are: {', '.join(t.value for t in NoteType)}"

                status_enum = None
                if status:
                    try:
                        status_enum = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"

                # Perform search
                results = self.search_service.search_combined(
                    text=query,
                    tags=tag_list,
                    note_type=note_type_enum,
                    status=status_enum,
                    project_id=project_id,
                    area_id=area_id,
                )

                # Limit results
                total_results = len(results)
                results = results[:limit]
                if not results:
                    out = "No matching notes found."
                    if query:
                        out += (
                            "\nBefore concluding the vault has nothing on this: "
                            "try pzk_find_similar_to_text with the full claim "
                            "(semantic recall catches what keywords miss)."
                        )
                    return out

                # Format results
                output = f"Found {len(results)} matching notes:\n\n"
                for i, result in enumerate(results, 1):
                    note = result.note
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    # Surface the relevance score so the caller can tell a strong
                    # match from a weak one (only meaningful for text queries).
                    # Guard on a real number so non-numeric scores are skipped
                    # rather than raising on the format spec.
                    if query and isinstance(result.score, (int, float)) and result.score:
                        output += f"   Relevance: {result.score:.3f}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    output += f"   Created: {note.created_at.strftime('%Y-%m-%d')}\n"
                    # Prefer the matched context (why this note matched) when present,
                    # otherwise fall back to a leading content snippet.
                    if result.matched_context:
                        match_line = result.matched_context.replace("\n", " ")
                        if len(match_line) > 200:
                            match_line = match_line[:200] + "..."
                        output += f"   Match: {match_line}\n"
                    else:
                        content_preview = note.content[:150].replace("\n", " ")
                        if len(note.content) > 150:
                            content_preview += "..."
                        output += f"   Preview: {content_preview}\n"
                    output += "\n"
                output += self._truncation_notice(
                    len(results), total_results, "refine the query or raise limit"
                )
                if query:
                    output += self._lexical_verdict(results) + "\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Get linked notes
        @self.mcp.tool(name="pzk_get_linked_notes")
        def pzk_get_linked_notes(note_id: str, direction: str = "both") -> str:
            """Get notes linked to/from a note.
            Args:
                note_id: ID of the note
                direction: Direction of links (outgoing, incoming, both)
            """
            try:
                if direction not in ["outgoing", "incoming", "both"]:
                    return f"Invalid direction: {direction}. Use 'outgoing', 'incoming', or 'both'."
                # Get linked notes
                linked_notes = self.zettel_service.get_linked_notes(
                    str(note_id), direction
                )
                if not linked_notes:
                    return f"No {direction} links found for note {note_id}."
                source_note = None
                if direction in ["outgoing", "both"]:
                    source_note = self.zettel_service.get_note(str(note_id))
                # Format results
                output = f"Found {len(linked_notes)} {direction} linked notes for {note_id}:\n\n"
                for i, note in enumerate(linked_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Try to determine link type
                    if direction in ["outgoing", "both"]:
                        # Check source note's outgoing links
                        if source_note:
                            for link in source_note.links:
                                if str(link.target_id) == str(
                                    note.id
                                ):  # Explicit string conversion for comparison
                                    output += f"   Link type: {link.link_type.value}\n"
                                    if link.description:
                                        output += (
                                            f"   Description: {link.description}\n"
                                        )
                                    break
                            else:
                                if note.id in getattr(
                                    source_note, "inline_refs", []
                                ):
                                    output += (
                                        "   Link type: inline (from a "
                                        "[[wiki-link]] in prose)\n"
                                    )
                    if direction in ["incoming", "both"]:
                        # Check target note's outgoing links
                        for link in note.links:
                            if str(link.target_id) == str(
                                note_id
                            ):  # Explicit string conversion for comparison
                                output += (
                                    f"   Incoming link type: {link.link_type.value}\n"
                                )
                                if link.description:
                                    output += f"   Description: {link.description}\n"
                                break
                    output += "\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        self.pzk_get_linked_notes = pzk_get_linked_notes

        # Get all tags
        @self.mcp.tool(name="pzk_get_all_tags")
        def pzk_get_all_tags() -> str:
            """Get all tags in the Zettelkasten.

            Call this BEFORE tagging new notes and reuse the closest existing
            tag; only mint a new tag when the concept is genuinely absent.
            New tags are normalized to lowercase-hyphenated form on write
            (a leading @ for GTD contexts is preserved). For a meaning-based
            shortlist instead of the full alphabetical list, use pzk_suggest_tags.
            """
            try:
                tags = self.zettel_service.get_all_tags()
                if not tags:
                    return "No tags found in the Zettelkasten."

                # Format results
                output = f"Found {len(tags)} tags:\n\n"
                # Sort alphabetically
                tags.sort(key=lambda t: t.name.lower())
                for i, tag in enumerate(tags, 1):
                    output += f"{i}. {tag.name}\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_suggest_tags")
        def pzk_suggest_tags(text: str, limit: int = 10) -> str:
            """Semantic tag search: the existing tags most related to *text*.

            A free-text counterpart to pzk_get_all_tags — instead of scanning the
            whole alphabetical list, pass a draft note's title/body (or a concept)
            and get the closest existing tags by MEANING, so you reuse an existing
            tag rather than minting a near-duplicate. Requires embeddings; falls
            back to pzk_get_all_tags when they are off.

            Args:
                text: Text to match tags against (a claim, draft body, or concept).
                limit: Maximum tags to return (default 10).
            """
            try:
                text = str(text or "").strip()
                if not text:
                    return (
                        "Provide text (a draft note's title/body, or a concept) "
                        "to find related tags."
                    )
                results = self.zettel_service.suggest_tags(
                    text, limit=max(1, int(limit))
                )
                if not results:
                    if not config.embedding_enabled:
                        return (
                            "Semantic tag search needs embeddings enabled. Use "
                            "pzk_get_all_tags and pick the closest tag by eye."
                        )
                    return (
                        "No tags to suggest yet — the vault has no tags. Mint a "
                        "concise lowercase-hyphenated tag when you create the note."
                    )
                lines = [
                    f"Tags most related to your text ({len(results)} shown) — "
                    "reuse the closest; mint a new tag only if the concept is "
                    "genuinely absent:",
                    "",
                ]
                for i, (name, sim) in enumerate(results, 1):
                    lines.append(f"{i}. {name}  (similarity {sim:.2f})")
                return "\n".join(lines)
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_suggest_areas")
        def pzk_suggest_areas(text: str, limit: int = 5) -> str:
            """Semantic area search: the PARA areas most related to *text* (routing).

            Pass a draft note's title/body (or a concept) to find the area it most
            likely belongs under, so a new note is routed to the closest area_id
            instead of floating unrouted. A free-text counterpart to pzk_list_areas.
            Requires embeddings; falls back to pzk_list_areas when they are off.

            Args:
                text: Text to match areas against (a claim, draft body, or concept).
                limit: Maximum areas to return (default 5).
            """
            try:
                text = str(text or "").strip()
                if not text:
                    return (
                        "Provide text to find the most relevant area(s) to route "
                        "a note under."
                    )
                results = self.zettel_service.suggest_areas(
                    text, limit=max(1, int(limit))
                )
                if not results:
                    if not config.embedding_enabled:
                        return (
                            "Semantic area search needs embeddings enabled. Use "
                            "pzk_list_areas and pick the area by topic."
                        )
                    return (
                        "No areas to suggest. Create one with pzk_create_area, or "
                        "see pzk_list_areas."
                    )
                lines = [
                    "Areas most related to your text — route the note to the "
                    "closest (pass its ID as area_id):",
                    "",
                ]
                for i, (note, sim) in enumerate(results, 1):
                    lines.append(
                        f"{i}. {note.title} (ID: {note.id})  similarity {sim:.2f}"
                    )
                return "\n".join(lines)
            except Exception as e:
                return self.format_error_response(e)

        # Find similar notes
        @self.mcp.tool(name="pzk_find_similar_notes")
        def pzk_find_similar_notes(
            note_id: str, threshold: float = 0.3, limit: int = 5
        ) -> str:
            """Find notes similar to a given note.
            Args:
                note_id: ID of the reference note
                threshold: Similarity threshold (0.0-1.0)
                limit: Maximum number of results to return
            """
            try:
                # Get similar notes
                similar_notes = self.zettel_service.find_similar_notes(
                    str(note_id), threshold
                )
                # Limit results
                similar_notes = similar_notes[:limit]
                if not similar_notes:
                    return f"No similar notes found for {note_id} with threshold {threshold}."

                # Format results
                output = f"Found {len(similar_notes)} similar notes for {note_id}:\n\n"
                for i, (note, similarity) in enumerate(similar_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   Similarity: {similarity:.2f}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                output += self._semantic_verdict(similar_notes[0][1]) + "\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find notes similar to arbitrary text (pre-create semantic check)
        @self.mcp.tool(name="pzk_find_similar_to_text")
        def pzk_find_similar_to_text(
            text: str, threshold: float = 0.5, limit: int = 10
        ) -> str:
            """Find existing notes semantically similar to raw text.

            Unlike pzk_find_similar_notes (which needs an existing note ID), this
            embeds arbitrary text — use it to check a DRAFT note for
            cross-vocabulary duplicates/overlaps BEFORE creating it (catching
            near-duplicates that keyword search misses, exactly when it matters).
            Returns calibrated cosine similarities (0.0-1.0), highest first.
            Requires embeddings enabled; otherwise fall back to pzk_search_notes.

            Args:
                text: The draft claim (title and/or body) to check — semantic,
                    not keyword.
                threshold: Minimum cosine similarity to return (0.0-1.0).
                limit: Maximum number of results to return.
            """
            try:
                if not text or not text.strip():
                    return "Error: text is required."
                if limit <= 0:
                    return "Limit must be greater than 0."
                similar = self.zettel_service.find_similar_to_text(
                    text, threshold, limit
                )
                if not similar:
                    return (
                        f"No semantically similar notes found (threshold {threshold}). "
                        "This content is likely novel to the vault — but recall is "
                        "imperfect, so an empty result does not PROVE novelty; "
                        "if a related note comes to mind, link it by hand. "
                        "If embeddings are disabled, use pzk_search_notes instead."
                    )
                output = f"Found {len(similar)} semantically similar notes:\n\n"
                for i, (note, similarity) in enumerate(similar, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   Similarity: {similarity:.2f}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                output += self._semantic_verdict(similar[0][1]) + "\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find central notes
        @self.mcp.tool(name="pzk_find_central_notes")
        def pzk_find_central_notes(limit: int = 10) -> str:
            """Find notes with the most connections (incoming + outgoing links).
            Notes are ranked by their total number of connections, determining
            their centrality in the knowledge network. Due to database constraints,
            only one link of each type is counted between any pair of notes.

            Args:
                limit: Maximum number of results to return (default: 10)
            """
            try:
                # Get central notes
                central_notes = self.search_service.find_central_notes(limit)
                if not central_notes:
                    return "No notes found with connections."

                # Format results
                output = "Central notes in the Zettelkasten (most connected):\n\n"
                for i, (note, connection_count) in enumerate(central_notes, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   Connections: {connection_count}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # Find orphaned notes
        @self.mcp.tool(name="pzk_find_orphaned_notes")
        def pzk_find_orphaned_notes() -> str:
            """Find notes with no connections to other notes."""
            try:
                # Get orphaned notes
                orphans = self.search_service.find_orphaned_notes()
                if not orphans:
                    return "No orphaned notes found."

                # Format results
                output = f"Found {len(orphans)} orphaned notes:\n\n"
                for i, note in enumerate(orphans, 1):
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except Exception as e:
                return self.format_error_response(e)

        # List notes by date range
        @self.mcp.tool(name="pzk_list_notes_by_date")
        def pzk_list_notes_by_date(
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
            use_updated: bool = False,
            limit: int = 10,
        ) -> str:
            """List notes created or updated within a date range.
            Args:
                start_date: Start date in ISO format (YYYY-MM-DD)
                end_date: End date in ISO format (YYYY-MM-DD)
                use_updated: Whether to use updated_at instead of created_at
                limit: Maximum number of results to return
            """
            try:
                # Parse dates
                start_datetime = None
                if start_date:
                    start_datetime = datetime.fromisoformat(f"{start_date}T00:00:00")
                end_datetime = None
                if end_date:
                    end_datetime = datetime.fromisoformat(f"{end_date}T23:59:59")

                # Get notes
                notes = self.search_service.find_notes_by_date_range(
                    start_date=start_datetime,
                    end_date=end_datetime,
                    use_updated=use_updated,
                )

                # Limit results
                notes = notes[:limit]
                if not notes:
                    date_type = "updated" if use_updated else "created"
                    date_range = ""
                    if start_date and end_date:
                        date_range = f" between {start_date} and {end_date}"
                    elif start_date:
                        date_range = f" after {start_date}"
                    elif end_date:
                        date_range = f" before {end_date}"
                    return f"No notes found {date_type}{date_range}."

                # Format results
                date_type = "updated" if use_updated else "created"
                output = f"Notes {date_type}"
                if start_date or end_date:
                    if start_date and end_date:
                        output += f" between {start_date} and {end_date}"
                    elif start_date:
                        output += f" after {start_date}"
                    elif end_date:
                        output += f" before {end_date}"
                output += f" (showing {len(notes)} results):\n\n"
                for i, note in enumerate(notes, 1):
                    date = note.updated_at if use_updated else note.created_at
                    output += f"{i}. {note.title} (ID: {note.id})\n"
                    output += f"   {date_type.capitalize()}: {date.strftime('%Y-%m-%d %H:%M')}\n"
                    if note.tags:
                        output += (
                            f"   Tags: {', '.join(tag.name for tag in note.tags)}\n"
                        )
                    # Add a snippet of content (first 100 chars)
                    content_preview = note.content[:100].replace("\n", " ")
                    if len(note.content) > 100:
                        content_preview += "..."
                    output += f"   Preview: {content_preview}\n\n"
                return output
            except ValueError as e:
                # Special handling for date parsing errors
                logger.error(f"Date parsing error: {str(e)}")
                return f"Error parsing date: {str(e)}"
            except Exception as e:
                return self.format_error_response(e)

        # Rebuild the index
        @self.mcp.tool(name="pzk_rebuild_index")
        def pzk_rebuild_index() -> str:
            """Rebuild the database index from files."""
            try:
                # Get count before rebuild
                note_count_before = len(self.zettel_service.get_all_notes())

                # Perform the rebuild
                backup_path = self.zettel_service.rebuild_index()

                # Get count after rebuild
                note_count_after = len(self.zettel_service.get_all_notes())
                backup_message = (
                    f"Backup created: {backup_path}\n"
                    if backup_path
                    else "Backup created: none (database file did not exist)\n"
                )

                # Surface any markdown files that failed to parse so a shrinking
                # corpus is visible to the caller, not silently dropped. Only the
                # in-process direct backend exposes this; the daemon logs skips
                # itself, so accept only a real list and ignore anything else
                # (e.g. a proxy attribute or a test mock).
                skipped = getattr(
                    self.zettel_service.repository, "last_rebuild_skipped", []
                )
                if not isinstance(skipped, (list, tuple)):
                    skipped = []
                skipped_message = ""
                if skipped:
                    preview = ", ".join(skipped[:10])
                    if len(skipped) > 10:
                        preview += f", … (+{len(skipped) - 10} more)"
                    skipped_message = (
                        f"WARNING: {len(skipped)} file(s) failed to parse and were "
                        f"skipped: {preview}\n"
                    )

                # Return a detailed success message
                return (
                    f"Database index rebuilt successfully.\n"
                    f"{backup_message}"
                    f"{skipped_message}"
                    f"Notes processed: {note_count_after}\n"
                    f"Change in note count: {note_count_after - note_count_before}"
                )
            except Exception as e:
                # Provide a detailed error message
                logger.error(f"Failed to rebuild index: {e}", exc_info=True)
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_check_consistency")
        def pzk_check_consistency() -> str:
            """Report drift between the markdown files and the graph index.

            Read-only: this does not modify anything. It surfaces notes that exist
            on disk but not in the index, notes indexed without a file, and notes
            whose file content has diverged from the index. Run pzk_rebuild_index
            to reconcile any drift it finds.
            """
            try:
                report = self.zettel_service.check_consistency()

                total_files = report.get("total_files", 0)
                total_indexed = report.get("total_indexed", 0)
                in_sync = report.get("in_sync", 0)
                missing_from_index = report.get("missing_from_index", []) or []
                missing_from_files = report.get("missing_from_files", []) or []
                content_drift = report.get("content_drift", []) or []
                unreadable = report.get("unreadable_files", []) or []
                dangling_refs = report.get("dangling_refs", []) or []

                def _section(label: str, ids: List[str]) -> str:
                    if not ids:
                        return ""
                    preview = ", ".join(ids[:20])
                    if len(ids) > 20:
                        preview += f", … (+{len(ids) - 20} more)"
                    return f"{label} ({len(ids)}): {preview}\n"

                dangling_section = ""
                if dangling_refs:
                    dangling_section = "\n" + _section(
                        "Dangling wiki references (target note no longer "
                        "exists; fix by editing the source note)",
                        dangling_refs,
                    )

                if report.get("consistent"):
                    return (
                        "Files and index are consistent.\n"
                        f"Files: {total_files}, Indexed: {total_indexed}, "
                        f"In sync: {in_sync}" + (
                            "\n" + dangling_section if dangling_section else ""
                        )
                    )

                out = "Drift detected between files and index.\n"
                out += (
                    f"Files: {total_files}, Indexed: {total_indexed}, "
                    f"In sync: {in_sync}\n\n"
                )
                out += _section("Files missing from index", missing_from_index)
                out += _section("Indexed notes with no file", missing_from_files)
                out += _section("Content drift (file != index)", content_drift)
                out += _section("Unreadable files", unreadable)
                out += dangling_section
                out += "\nRun pzk_rebuild_index to reconcile."
                return out
            except Exception as e:
                return self.format_error_response(e)

        # ----------------------------------------------------------------
        # Action-item tools (PARA / GTD)
        # ----------------------------------------------------------------

        @self.mcp.tool(name="pzk_create_task")
        def pzk_create_task(
            title: str,
            content: str,
            project_id: str = "",
            status: str = "inbox",
            tags: Optional[str] = None,
            area_id: Optional[str] = None,
            due_date: Optional[str] = None,
            remind_at: Optional[str] = None,
            priority: Optional[int] = None,
            recurrence_rule: Optional[str] = None,
            estimated_minutes: Optional[int] = None,
            source: str = "manual",
            context: Optional[str] = None,
            energy_level: Optional[str] = None,
        ) -> str:
            """Create a task note. Tasks must belong to a project.
            Args:
                title: Task title
                content: Task description
                project_id: ID of the project this task belongs to (required)
                status: inbox, ready, scheduled, active, waiting, someday, done, cancelled
                tags: Comma-separated tags
                area_id: Override area (auto-filled from project if omitted)
                due_date: Due date YYYY-MM-DD
                remind_at: Reminder date YYYY-MM-DD
                priority: 1 (low) to 4 (critical)
                recurrence_rule: daily, weekly, monthly, quarterly, yearly
                estimated_minutes: Estimated effort in minutes
                source: manual, inbox, email, meeting, voice, transcript, book, article, chat, web, pdf, recurring
                context: GTD context — auto-applies @{context} tag (e.g. 'home' → '@home')
                energy_level: high, medium, or low — auto-applies {level}-energy tag
            """
            try:
                import datetime as _dt

                if not project_id:
                    return "project_id is required. Tasks must belong to a project."
                try:
                    task_status = NoteStatus(status.lower())
                except ValueError:
                    return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return f"Invalid source: {source}. Valid: {', '.join(s.value for s in NoteSource)}"
                if priority is not None and priority not in {1, 2, 3, 4}:
                    return "Invalid priority: use 1 (low) to 4 (critical)."
                parsed_due = None
                if due_date:
                    try:
                        parsed_due = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                parsed_remind = None
                if remind_at:
                    try:
                        parsed_remind = _dt.date.fromisoformat(remind_at)
                    except ValueError:
                        return f"Invalid remind_at: {remind_at}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                # Auto-apply @context tag
                if context:
                    ctx = context.lstrip("@").strip()
                    if ctx:
                        tag_list.append(f"@{ctx}")
                # Auto-apply energy tag
                _energy_tags = {
                    "high": "high-energy",
                    "medium": "mid-energy",
                    "low": "low-energy",
                }
                if energy_level:
                    el = energy_level.lower()
                    if el not in _energy_tags:
                        return f"Invalid energy_level: {energy_level}. Valid: high, medium, low"
                    tag_list.append(_energy_tags[el])
                task = self.zettel_service.create_task(
                    title=title,
                    content=content,
                    status=task_status,
                    tags=tag_list,
                    project_id=project_id,
                    area_id=area_id,
                    due_date=parsed_due,
                    remind_at=parsed_remind,
                    priority=priority,
                    recurrence_rule=recurrence_rule,
                    estimated_minutes=estimated_minutes,
                    source=note_source,
                )
                return f"Task created successfully: {task.title} (ID: {task.id})"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_update_task")
        def pzk_update_task(
            task_id: str,
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            due_date: Optional[str] = None,
            remind_at: Optional[str] = None,
            priority: Optional[int] = None,
            estimated_minutes: Optional[int] = None,
            recurrence_rule: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Update any fields on an existing task.

            This is the only task-update tool. Use it for both ordinary field edits and
            status transitions. When a recurring task is marked done, the next instance
            is spawned automatically after non-status edits are persisted. Passing tags
            replaces the task's existing tag list.

            Args:
                task_id: ID of the task note
                status: inbox, ready, scheduled, active, waiting, someday, done, cancelled
                project_id: ID of the project this task belongs to
                parent_project_id: Alternate name for the project this task belongs to
                due_date: Due date YYYY-MM-DD
                remind_at: Reminder date YYYY-MM-DD
                priority: 1 (low) to 4 (critical)
                estimated_minutes: Estimated effort in minutes
                recurrence_rule: daily, weekly, monthly, quarterly, yearly
                tags: Comma-separated tags (replaces existing tags)
            """
            try:
                import datetime as _dt

                task = self.zettel_service.get_note(task_id)
                if not task:
                    return f"Task not found: {task_id}"
                if task.note_type != NoteType.TASK:
                    return f"Note {task_id} is not a task (type: {task.note_type.value})"

                # Validate all inputs before applying any changes
                new_status = None
                if status is not None:
                    try:
                        new_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                if priority is not None and priority not in {1, 2, 3, 4}:
                    return "Invalid priority: use 1 (low) to 4 (critical)."
                parsed_due = None
                if due_date is not None:
                    try:
                        parsed_due = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                parsed_remind = None
                if remind_at is not None:
                    try:
                        parsed_remind = _dt.date.fromisoformat(remind_at)
                    except ValueError:
                        return f"Invalid remind_at: {remind_at}. Use YYYY-MM-DD."
                update_kwargs = {}
                if new_status is not None:
                    update_kwargs["status"] = new_status
                if parent_project_id is not None:
                    normalized_parent_project_id = parent_project_id.strip()
                    if project_id is not None:
                        normalized_project_id = project_id.strip()
                        if normalized_project_id != normalized_parent_project_id:
                            return (
                                "project_id and parent_project_id must match when "
                                "both are provided."
                            )
                    if not normalized_parent_project_id:
                        return (
                            "parent_project_id is required. Tasks must belong to a project."
                        )
                    update_kwargs["project_id"] = normalized_parent_project_id
                if project_id is not None:
                    normalized_project_id = project_id.strip()
                    if not normalized_project_id:
                        return "project_id is required. Tasks must belong to a project."
                    update_kwargs.setdefault("project_id", normalized_project_id)
                if due_date is not None:
                    update_kwargs["due_date"] = parsed_due
                if remind_at is not None:
                    update_kwargs["remind_at"] = parsed_remind
                if priority is not None:
                    update_kwargs["priority"] = priority
                if estimated_minutes is not None:
                    update_kwargs["estimated_minutes"] = estimated_minutes
                if recurrence_rule is not None:
                    update_kwargs["recurrence_rule"] = recurrence_rule
                if tags is not None:
                    update_kwargs["tags"] = [
                        t.strip() for t in tags.split(",") if t.strip()
                    ]

                msg = f"Task {task_id} updated successfully."
                if update_kwargs:
                    updated = self.zettel_service.update_task(task_id, **update_kwargs)
                else:
                    updated = task
                if new_status is not None:
                    msg += f" Status set to '{new_status.value}'."
                    if new_status == NoteStatus.DONE and updated.recurrence_rule:
                        msg += " New recurring instance created."

                return msg
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_tasks")
        def pzk_get_tasks(
            status: Optional[str] = None,
            project_id: Optional[str] = None,
            due_date: Optional[str] = None,
            overdue_only: bool = False,
            priority: Optional[int] = None,
            limit: int = 20,
        ) -> str:
            """Query tasks with optional filters.
            Args:
                status: Filter by status. If omitted, done and archived tasks are hidden by default.
                project_id: Filter to tasks linked to this project
                due_date: Filter to tasks due on or before this date (YYYY-MM-DD)
                overdue_only: Only return tasks with due_date before today
                priority: Filter by priority (1-4)
                limit: Maximum results
            """
            try:
                import datetime as _dt

                task_status = None
                if status:
                    try:
                        task_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}. Valid: {', '.join(s.value for s in NoteStatus)}"
                due_before = None
                if overdue_only:
                    due_before = _dt.date.today() - _dt.timedelta(days=1)
                elif due_date:
                    try:
                        due_before = _dt.date.fromisoformat(due_date)
                    except ValueError:
                        return f"Invalid due_date: {due_date}. Use YYYY-MM-DD."
                tasks = self.zettel_service.get_tasks(
                    status=task_status,
                    project_id=project_id,
                    due_date_before=due_before,
                    priority=priority,
                    limit=limit,
                )
                if not tasks:
                    return "No matching tasks found."
                out = f"Found {len(tasks)} task(s):\n\n"
                for i, t in enumerate(tasks, 1):
                    out += f"{i}. {t.title} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}"
                    if t.due_date:
                        out += f"  Due: {t.due_date}"
                    if t.priority:
                        out += f"  Priority: {t.priority}"
                    out += "\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_todays_tasks")
        def pzk_get_todays_tasks(include_overdue: bool = True) -> str:
            """Return tasks due today and optionally overdue tasks.
            Args:
                include_overdue: Include tasks with past due dates (default: True)
            """
            try:
                tasks = self.zettel_service.get_todays_tasks(include_overdue)
                if not tasks:
                    return "No tasks due today."
                out = f"Today's tasks ({len(tasks)}):\n\n"
                for i, t in enumerate(tasks, 1):
                    priority_str = f" [P{t.priority}]" if t.priority else ""
                    due_str = f" — due {t.due_date}" if t.due_date else ""
                    out += f"{i}.{priority_str} {t.title}{due_str} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_project")
        def pzk_create_project(
            title: str,
            content: str,
            source: str,
            area_id: Optional[str] = None,
            parent_project_id: Optional[str] = None,
            outcome: Optional[str] = None,
            deadline: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create a project note linked to an area or parent project.
            Args:
                title: Project title
                content: Project description
                source: Origin of the project note
                area_id: ID of the area this project belongs to
                parent_project_id: Optional parent project ID for subprojects
                outcome: The desired outcome/goal
                deadline: Target completion date (YYYY-MM-DD)
                tags: Comma-separated tags
            """
            try:
                import datetime as _dt

                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return (
                        f"Invalid source: {source}. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )
                normalized_parent_project_id = (
                    parent_project_id.strip() if parent_project_id is not None else None
                )
                parent_project_id = normalized_parent_project_id or None
                resolved_area_id = area_id
                if parent_project_id:
                    parent_project = self.zettel_service.get_note(parent_project_id)
                    if not parent_project or parent_project.note_type != NoteType.PROJECT:
                        return (
                            f"parent_project_id {parent_project_id} "
                            "is not a valid project note."
                        )
                    if not parent_project.area_id:
                        return (
                            f"parent_project_id {parent_project_id} "
                            "does not have an area_id."
                        )
                    if (
                        resolved_area_id
                        and resolved_area_id != parent_project.area_id
                    ):
                        return (
                            f"area_id {resolved_area_id} does not match project "
                            f"{parent_project_id} area_id {parent_project.area_id}"
                        )
                    resolved_area_id = parent_project.area_id
                elif not resolved_area_id:
                    return "area_id is required for top-level projects."
                area = self.zettel_service.get_note(resolved_area_id)
                if not area or area.note_type != NoteType.AREA:
                    return f"area_id {resolved_area_id} is not a valid area note."
                parsed_deadline = None
                if deadline:
                    try:
                        parsed_deadline = _dt.date.fromisoformat(deadline)
                    except ValueError:
                        return f"Invalid deadline: {deadline}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                project = self.zettel_service.create_project_note(
                    title=title,
                    content=content,
                    outcome=outcome,
                    deadline=parsed_deadline,
                    area_id=resolved_area_id,
                    project_id=parent_project_id,
                    tags=tag_list,
                    source=note_source,
                )
                return f"Project created successfully with ID: {project.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_subproject")
        def pzk_create_subproject(
            parent_project_id: str,
            title: str,
            content: str,
            source: str,
            outcome: Optional[str] = None,
            deadline: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create a subproject under an existing parent project.
            Args:
                parent_project_id: ID of the parent project
                title: Subproject title
                content: Subproject description
                source: Origin of the subproject note
                outcome: The desired outcome/goal
                deadline: Target completion date (YYYY-MM-DD)
                tags: Comma-separated tags
            """
            try:
                import datetime as _dt

                try:
                    note_source = NoteSource(source.lower())
                except ValueError:
                    return (
                        f"Invalid source: {source}. "
                        f"Valid: {', '.join(s.value for s in NoteSource)}"
                    )
                normalized_parent_project_id = parent_project_id.strip()
                if not normalized_parent_project_id:
                    return "parent_project_id is required."
                parent_project_id = normalized_parent_project_id
                parent_project = self.zettel_service.get_note(parent_project_id)
                if not parent_project or parent_project.note_type != NoteType.PROJECT:
                    return (
                        f"parent_project_id {parent_project_id} "
                        "is not a valid project note."
                    )
                if not parent_project.area_id:
                    return (
                        f"parent_project_id {parent_project_id} "
                        "does not have an area_id."
                    )
                parsed_deadline = None
                if deadline:
                    try:
                        parsed_deadline = _dt.date.fromisoformat(deadline)
                    except ValueError:
                        return f"Invalid deadline: {deadline}. Use YYYY-MM-DD."
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                project = self.zettel_service.create_project_note(
                    title=title,
                    content=content,
                    outcome=outcome,
                    deadline=parsed_deadline,
                    area_id=parent_project.area_id,
                    project_id=parent_project_id,
                    tags=tag_list,
                    source=note_source,
                )
                return f"Subproject created successfully with ID: {project.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project")
        def pzk_get_project(project_id: str) -> str:
            """Get a project note with task, note, and subproject context.
            Args:
                project_id: ID of the project note
            """
            try:
                project = self.zettel_service.get_note(project_id)
                if not project:
                    return f"Project not found: {project_id}"
                if project.note_type != NoteType.PROJECT:
                    return f"Note {project_id} is not a project (type: {project.note_type.value})"
                tasks = self.zettel_service.get_project_tasks(project_id)
                project_notes = self.zettel_service.get_project_notes(project_id)
                parent_project = self.zettel_service.get_parent_project(project_id)
                subprojects = self.zettel_service.get_subprojects(project_id)
                preview_tasks = self._get_project_preview_tasks(tasks)
                counts: dict = {}
                for t in tasks:
                    s = t.status.value if t.status else "none"
                    counts[s] = counts.get(s, 0) + 1
                outcome = project.metadata.get("outcome", "")
                out = f"ID: {project.id}\n"
                if project.area_id:
                    out += f"Area ID: {project.area_id}\n"
                if outcome:
                    out += f"Outcome: {outcome}\n"
                out += f"Tasks: {len(tasks)} total"
                if counts:
                    out += " (" + ", ".join(f"{v} {k}" for k, v in counts.items()) + ")"
                out += "\n\nNext Tasks:\n"
                if preview_tasks:
                    for task in preview_tasks:
                        out += self._format_project_preview_task(task) + "\n"
                else:
                    out += "- None\n"
                if parent_project:
                    out += "\nParent Project:\n"
                    out += f"- {parent_project.title} (ID: {parent_project.id})\n"
                out += "\nSubprojects:\n"
                if subprojects:
                    for subproject in subprojects:
                        out += f"- {subproject.title} (ID: {subproject.id})\n"
                else:
                    out += "- None\n"
                out += "\nNotes:\n"
                if project_notes:
                    for note in project_notes:
                        out += (
                            f"- {note.title} (ID: {note.id}, type: "
                            f"{note.note_type.value})\n"
                        )
                else:
                    out += "- None\n"
                out += f"\n\n{project.content}\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project_notes")
        def pzk_get_project_notes(
            project_id: str, limit: int = 50, detail: str = "full"
        ) -> str:
            """Get all non-task notes routed to a project.
            Args:
                project_id: ID of the project note
                limit: Maximum results
                detail: Output detail — 'full' (default), 'summary', or 'ids'.
                    Prefer 'summary' when surveying a large project to save context.
            """
            try:
                detail = str(detail).strip().lower()
                if detail not in {"full", "summary", "ids"}:
                    return "Invalid detail: use 'full', 'summary', or 'ids'."
                all_notes = self.zettel_service.get_project_notes(project_id)
                notes = all_notes[:limit]
                if not notes:
                    return f"No project notes found for project {project_id}."

                if detail == "full":
                    self.zettel_service.record_retrieval([n.id for n in notes])
                out = f"Project notes for {project_id} ({len(notes)}):\n\n"
                out += self._render_notes_with_detail(notes, detail)
                out += self._truncation_notice(
                    len(notes), len(all_notes), "raise limit to see the rest"
                )
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_project_tasks")
        def pzk_get_project_tasks(
            project_id: str,
            status: Optional[str] = None,
            limit: int = 50,
        ) -> str:
            """Get all tasks linked to a project.
            Args:
                project_id: ID of the project note
                status: Filter by status
                limit: Maximum results
            """
            try:
                task_status = None
                if status:
                    try:
                        task_status = NoteStatus(status.lower())
                    except ValueError:
                        return f"Invalid status: {status}."
                tasks = self.zettel_service.get_project_tasks(project_id, task_status)
                tasks = tasks[:limit]
                if not tasks:
                    return f"No tasks found for project {project_id}."
                out = f"Tasks for project {project_id} ({len(tasks)}):\n\n"
                for i, t in enumerate(tasks, 1):
                    out += f"{i}. {t.title} (ID: {t.id})\n"
                    out += f"   Status: {t.status.value if t.status else 'none'}"
                    if t.due_date:
                        out += f"  Due: {t.due_date}"
                    out += "\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_create_area")
        def pzk_create_area(
            title: str,
            content: str,
            cadence: Optional[str] = None,
            tags: Optional[str] = None,
        ) -> str:
            """Create an area note (ongoing responsibility with no end date).
            Args:
                title: Area title
                content: Area description
                cadence: Review cadence (e.g. 'weekly review', 'monthly check-in')
                tags: Comma-separated tags
            """
            try:
                tag_list = (
                    [t.strip() for t in tags.split(",") if t.strip()] if tags else []
                )
                area = self.zettel_service.create_area_note(
                    title=title, content=content, cadence=cadence, tags=tag_list
                )
                return f"Area created successfully with ID: {area.id}"
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_list_projects")
        def pzk_list_projects(include_done: bool = False, limit: int = 20) -> str:
            """List all project notes, sorted by due date.
            Args:
                include_done: Include completed/cancelled projects (default: False)
                limit: Maximum results
            """
            try:
                import datetime as _dt

                projects = self.zettel_service.search_notes(note_type=NoteType.PROJECT)
                if not include_done:
                    projects = [
                        p
                        for p in projects
                        if p.status not in (NoteStatus.DONE, NoteStatus.CANCELLED)
                    ]
                projects.sort(key=lambda p: (p.due_date or _dt.date.max))
                projects = projects[:limit]
                if not projects:
                    return "No active projects found."
                out = f"Projects ({len(projects)}):\n\n"
                for i, p in enumerate(projects, 1):
                    out += f"{i}. {p.title} (ID: {p.id})\n"
                    if p.due_date:
                        out += f"   Deadline: {p.due_date}\n"
                    outcome = p.metadata.get("outcome", "")
                    if outcome:
                        out += f"   Outcome: {outcome}\n"
                    out += "\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_list_areas")
        def pzk_list_areas(limit: int = 20) -> str:
            """List all area notes.
            Args:
                limit: Maximum results
            """
            try:
                areas = self.zettel_service.search_notes(note_type=NoteType.AREA)
                areas = areas[:limit]
                if not areas:
                    return "No areas found."
                out = f"Areas ({len(areas)}):\n\n"
                for i, a in enumerate(areas, 1):
                    out += f"{i}. {a.title} (ID: {a.id})\n"
                    cadence = a.metadata.get("cadence", "")
                    if cadence:
                        out += f"   Cadence: {cadence}\n"
                    out += "\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_area")
        def pzk_get_area(area_id: str) -> str:
            """Get an area note with its linked projects and open task counts.
            Args:
                area_id: ID of the area note
            """
            try:
                area = self.zettel_service.get_note(area_id)
                if not area:
                    return f"Area not found: {area_id}"
                if area.note_type != NoteType.AREA:
                    return f"Note {area_id} is not an area (type: {area.note_type.value})"
                projects = self.zettel_service.search_notes(
                    note_type=NoteType.PROJECT, area_id=area_id
                )
                cadence = area.metadata.get("cadence", "")
                out = f"ID: {area.id}\n"
                if cadence:
                    out += f"Cadence: {cadence}\n"
                out += f"Projects: {len(projects)}\n"
                out += f"\n{area.content}\n"
                if projects:
                    out += "\n## Projects\n"
                    for p in projects:
                        task_count = len(self.zettel_service.get_project_tasks(p.id))
                        out += f"- {p.title} (ID: {p.id}) — {task_count} task(s)\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_get_reminders")
        def pzk_get_reminders(limit: int = 20) -> str:
            """Return notes and tasks with remind_at <= today, sorted by remind_at.
            Args:
                limit: Maximum results (default 20)
            """
            try:
                notes = self.zettel_service.get_reminders(limit)
                if not notes:
                    return "No reminders due today."
                out = f"Reminders due ({len(notes)}):\n\n"
                for i, n in enumerate(notes, 1):
                    out += f"{i}. {n.title} (ID: {n.id})\n"
                    out += f"   Type: {n.note_type.value}  Remind: {n.remind_at}\n\n"
                return out
            except Exception as e:
                return self.format_error_response(e)

        # ----------------------------------------------------------------
        # AI-memory ergonomics tools
        # ----------------------------------------------------------------

        @self.mcp.tool(name="pzk_briefing")
        def pzk_briefing() -> str:
            """One-call session orientation: active projects, today's + overdue
            tasks, due reminders, and recently touched notes.

            Call this at the start of a work session (instead of separate
            pzk_list_projects / pzk_get_todays_tasks / pzk_get_reminders /
            pzk_list_notes_by_date calls) so the vault is consulted before new
            work begins.
            """
            import datetime as _dt

            today = _dt.date.today()
            sections: List[str] = [f"Vault briefing — {today.isoformat()}"]

            try:
                projects = self.zettel_service.search_notes(
                    note_type=NoteType.PROJECT
                )
                projects = [
                    p
                    for p in projects
                    if p.status not in (NoteStatus.DONE, NoteStatus.CANCELLED)
                ]
                projects.sort(key=lambda p: (p.due_date or _dt.date.max))
                lines = [f"Active projects ({len(projects)}):"]
                for p in projects[:10]:
                    line = f"- {p.title} (ID: {p.id})"
                    if p.due_date:
                        line += f" — deadline {p.due_date}"
                    lines.append(line)
                if len(projects) > 10:
                    lines.append(f"  (+{len(projects) - 10} more)")
                sections.append("\n".join(lines))
            except Exception as e:
                sections.append(f"Active projects: unavailable ({e})")

            try:
                tasks = self.zettel_service.get_todays_tasks(True)
                lines = [f"Tasks due today / overdue ({len(tasks)}):"]
                for t in tasks[:15]:
                    priority_str = f" [P{t.priority}]" if t.priority else ""
                    due_str = f" — due {t.due_date}" if t.due_date else ""
                    status_str = t.status.value if t.status else "none"
                    lines.append(
                        f"-{priority_str} {t.title}{due_str} "
                        f"({status_str}, ID: {t.id})"
                    )
                if len(tasks) > 15:
                    lines.append(f"  (+{len(tasks) - 15} more)")
                if len(tasks) == 0:
                    lines = ["Tasks due today / overdue: none"]
                sections.append("\n".join(lines))
            except Exception as e:
                sections.append(f"Tasks: unavailable ({e})")

            try:
                reminders = self.zettel_service.get_reminders(10)
                if reminders:
                    lines = [f"Reminders due ({len(reminders)}):"]
                    for n in reminders:
                        lines.append(
                            f"- {n.title} (remind {n.remind_at}, ID: {n.id})"
                        )
                    sections.append("\n".join(lines))
                else:
                    sections.append("Reminders due: none")
            except Exception as e:
                sections.append(f"Reminders: unavailable ({e})")

            try:
                start = datetime.now() - timedelta(days=7)
                recent = self.search_service.find_notes_by_date_range(
                    start_date=start, use_updated=True
                )[:10]
                if recent:
                    lines = ["Recently touched notes (last 7 days):"]
                    for n in recent:
                        lines.append(
                            f"- {n.title} ({n.note_type.value}, "
                            f"updated {n.updated_at.strftime('%Y-%m-%d')}, "
                            f"ID: {n.id})"
                        )
                    sections.append("\n".join(lines))
                else:
                    sections.append("Recently touched notes: none in 7 days")
            except Exception as e:
                sections.append(f"Recent notes: unavailable ({e})")

            sections.append(
                "Before starting new work, check the vault for relevant prior "
                "knowledge: pzk_find_similar_to_text with the task described "
                "as a full sentence."
            )
            return "\n\n".join(sections)

        @self.mcp.tool(name="pzk_get_neighborhood")
        def pzk_get_neighborhood(
            note_id: str, depth: int = 2, max_nodes: int = 25
        ) -> str:
            """Map the linked neighborhood around a note, grouped by hop distance.

            Use for synthesis ('what do I know around X?') instead of chaining
            pzk_get_linked_notes calls. Includes inline prose references.

            Args:
                note_id: ID or title of the center note
                depth: How many hops to traverse (1-3)
                max_nodes: Cap on total notes returned (default 25)
            """
            try:
                if not 1 <= depth <= 3:
                    return "depth must be between 1 and 3."
                if max_nodes <= 0:
                    return "max_nodes must be greater than 0."
                center = self._resolve_note_identifier(note_id)
                if not center:
                    return self._not_found_message(note_id)

                visited: Dict[str, Note] = {center.id: center}
                hop_of: Dict[str, int] = {center.id: 0}
                relation: Dict[str, str] = {}
                frontier: List[Note] = [center]
                for hop in range(1, depth + 1):
                    next_frontier: List[Note] = []
                    for node in frontier:
                        if len(visited) >= max_nodes:
                            break
                        try:
                            neighbors = self.zettel_service.get_linked_notes(
                                node.id, "both"
                            )
                        except Exception as exc:
                            logger.debug(
                                "Could not read neighbors for %s: %s", node.id, exc
                            )
                            continue
                        for neighbor in neighbors:
                            if neighbor.id in visited:
                                continue
                            if len(visited) >= max_nodes:
                                break
                            visited[neighbor.id] = neighbor
                            hop_of[neighbor.id] = hop
                            relation[neighbor.id] = self._describe_relation(
                                node, neighbor
                            )
                            next_frontier.append(neighbor)
                    frontier = next_frontier
                    if not frontier:
                        break

                out = (
                    f"Neighborhood of '{center.title}' (ID: {center.id}) — "
                    f"{len(visited) - 1} connected note(s) within {depth} hop(s):\n"
                )
                for hop in range(1, depth + 1):
                    at_hop = [
                        note
                        for nid, note in visited.items()
                        if hop_of[nid] == hop
                    ]
                    if not at_hop:
                        continue
                    out += f"\nHop {hop}:\n"
                    for note in at_hop:
                        preview = note.content[:120].replace("\n", " ").strip()
                        if len(preview) > 100:
                            preview = preview[:100] + "..."
                        out += (
                            f"- {note.title} (ID: {note.id}, "
                            f"{note.note_type.value}) [{relation.get(note.id, 'linked')}]\n"
                        )
                        if preview:
                            out += f"  {preview}\n"
                if len(visited) - 1 == 0:
                    out += "\n(No links yet — consider pzk_find_similar_notes to find candidates.)"
                elif len(visited) >= max_nodes:
                    out += (
                        f"\n(Capped at {max_nodes} notes; raise max_nodes for "
                        "a wider map.)"
                    )
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_ingest_batch")
        def pzk_ingest_batch(
            notes: Optional[List[Dict[str, Any]]] = None,
            links: Optional[List[Dict[str, Any]]] = None,
            tasks: Optional[List[Dict[str, Any]]] = None,
            default_project_id: Optional[str] = None,
            default_area_id: Optional[str] = None,
            default_source: str = "chat",
            check_duplicates: bool = True,
            on_duplicate: str = "flag",
        ) -> str:
            """Create many notes, links, and tasks in ONE call (batch ingestion).

            Use this instead of dozens of individual create calls when capturing
            a transcript, meeting, or document. Notes are created first, then
            tasks, then links. Each item succeeds or fails independently.

            Duplicate handling: every note passes the same dedup probe as
            pzk_create_note. By default (on_duplicate='flag') a likely duplicate
            is still CREATED and flagged for your review — the probe compares
            only a content lead, and a high match means same TOPIC, not
            necessarily same CLAIM, so the fold decision stays with you (fold
            with pzk_update_note + pzk_delete_note, or keep and link). With
            on_duplicate='skip' the item is not created and link references to
            it attach to the existing note instead (for unattended pipelines
            where accreting near-duplicates is worse than a rare wrong fold).
            Because flagging is non-destructive, leave check_duplicates on even
            for pre-vetted drafts — it is a free safety net.

            Reference syntax in links: "#0" refers to the first entry in notes,
            "#1" the second, etc.; "#t0" refers to the first entry in tasks.
            Real note IDs are also accepted.

            Args:
                notes: List of note objects: {title, content, note_type?
                    (fleeting/literature/permanent/structure/hub, default
                    permanent), tags? (list or comma-string), source?, status?,
                    project_id?, area_id?, origin?, check_duplicates?}
                links: List of link objects: {source, target, type? (default
                    reference), description?, bidirectional?} — source/target
                    take real IDs or "#N"/"#tN" references
                tasks: List of task objects: {title, content, project_id?,
                    status?, due_date? (YYYY-MM-DD), priority? (1-4), tags?,
                    remind_at?}
                default_project_id: Project routing applied to items that don't
                    specify their own
                default_area_id: Area routing applied to notes that don't
                    specify their own
                default_source: Source for items that don't specify one
                    (default 'chat')
                check_duplicates: Default dedup behavior for notes (per-item
                    check_duplicates overrides)
                on_duplicate: 'flag' (default) creates the note anyway and
                    reports the duplicate candidate for review; 'skip' does not
                    create it and redirects its "#N" references to the existing
                    note
            """
            import datetime as _dt

            def _coerce_items(value: Any, label: str) -> List[Dict[str, Any]]:
                """Coerce a notes/links/tasks arg into a list of dicts (parsing JSON strings)."""
                if value is None:
                    return []
                if isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, list):
                    raise ValueError(f"{label} must be a JSON array of objects")
                return value

            try:
                try:
                    note_items = _coerce_items(notes, "notes")
                    link_items = _coerce_items(links, "links")
                    task_items = _coerce_items(tasks, "tasks")
                except (ValueError, json.JSONDecodeError) as exc:
                    return f"Error: {exc}"
                if not (note_items or link_items or task_items):
                    return "Provide at least one of notes, links, or tasks."
                on_duplicate = str(on_duplicate).strip().lower()
                if on_duplicate not in {"flag", "skip"}:
                    return "Invalid on_duplicate: use 'flag' or 'skip'."

                refs: Dict[str, str] = {}
                lines: List[str] = []
                review_lines: List[str] = []
                created_notes = skipped_notes = flagged_notes = 0
                created_tasks = created_links = 0
                errors = 0

                for idx, item in enumerate(note_items):
                    ref = f"#{idx}"
                    try:
                        if not isinstance(item, dict):
                            raise ValueError("each notes entry must be an object")
                        title = str(item.get("title") or "").strip()
                        body = str(item.get("content") or "")
                        if not title or not body:
                            raise ValueError("title and content are required")
                        nt = NoteType(
                            str(item.get("note_type", "permanent")).lower()
                        )
                        if nt not in _DEDUP_NOTE_TYPES:
                            raise ValueError(
                                "note_type must be a knowledge type (fleeting, "
                                "literature, permanent, structure, hub); use "
                                "the tasks list for action items"
                            )
                        src = NoteSource(
                            str(item.get("source") or default_source).lower()
                        )
                        st = None
                        if item.get("status"):
                            st = NoteStatus(str(item["status"]).lower())
                        tag_list = item.get("tags")
                        if isinstance(tag_list, str):
                            tag_list = [
                                t.strip() for t in tag_list.split(",") if t.strip()
                            ]
                        do_dedup = bool(item.get("check_duplicates", check_duplicates))
                        duplicates = (
                            self._find_duplicate_candidates(title, body)
                            if do_dedup
                            else []
                        )
                        if duplicates and on_duplicate == "skip":
                            existing, score = duplicates[0]
                            refs[ref] = existing.id
                            skipped_notes += 1
                            lines.append(
                                f"{ref} SKIPPED — near-duplicate of "
                                f"'{existing.title}' (ID: {existing.id}, "
                                f"score {score:.2f}); links to {ref} attach "
                                "to the existing note"
                            )
                            continue
                        note = self.zettel_service.create_note(
                            title=title,
                            content=body,
                            note_type=nt,
                            tags=tag_list,
                            source=src,
                            status=st,
                            project_id=item.get("project_id") or default_project_id,
                            area_id=item.get("area_id") or default_area_id,
                            origin=item.get("origin"),
                        )
                        refs[ref] = note.id
                        created_notes += 1
                        if duplicates:
                            # Flag (default): the probe sees only a content
                            # lead, and a strong match means same TOPIC, not
                            # necessarily same CLAIM — so create it and hand
                            # the fold/link decision to the caller.
                            existing, score = duplicates[0]
                            flagged_notes += 1
                            lines.append(
                                f"{ref} created WITH DUPLICATE FLAG: {title} "
                                f"(ID: {note.id})"
                            )
                            review_lines.append(
                                f"- {note.id} ('{title}') vs existing "
                                f"{existing.id} ('{existing.title}'), score "
                                f"{score:.2f} — open both: same atomic claim "
                                "-> fold (merge anything new into the existing "
                                f"note, then pzk_delete_note {note.id}); same "
                                "topic only -> keep and link them"
                            )
                        else:
                            lines.append(f"{ref} created: {title} (ID: {note.id})")
                    except Exception as exc:
                        errors += 1
                        lines.append(f"{ref} ERROR: {exc}")

                for idx, item in enumerate(task_items):
                    ref = f"#t{idx}"
                    try:
                        if not isinstance(item, dict):
                            raise ValueError("each tasks entry must be an object")
                        title = str(item.get("title") or "").strip()
                        body = str(item.get("content") or "")
                        if not title or not body:
                            raise ValueError("title and content are required")
                        project = item.get("project_id") or default_project_id
                        if not project:
                            raise ValueError(
                                "project_id (or default_project_id) is required "
                                "for tasks"
                            )
                        st = NoteStatus(str(item.get("status", "inbox")).lower())
                        src = NoteSource(
                            str(item.get("source") or default_source).lower()
                        )
                        due = None
                        if item.get("due_date"):
                            due = _dt.date.fromisoformat(str(item["due_date"]))
                        remind = None
                        if item.get("remind_at"):
                            remind = _dt.date.fromisoformat(str(item["remind_at"]))
                        priority = item.get("priority")
                        if priority is not None and priority not in {1, 2, 3, 4}:
                            raise ValueError("priority must be 1-4")
                        tag_list = item.get("tags")
                        if isinstance(tag_list, str):
                            tag_list = [
                                t.strip() for t in tag_list.split(",") if t.strip()
                            ]
                        task = self.zettel_service.create_task(
                            title=title,
                            content=body,
                            status=st,
                            tags=tag_list,
                            project_id=project,
                            due_date=due,
                            remind_at=remind,
                            priority=priority,
                            source=src,
                        )
                        refs[ref] = task.id
                        created_tasks += 1
                        lines.append(f"{ref} task created: {title} (ID: {task.id})")
                    except Exception as exc:
                        errors += 1
                        lines.append(f"{ref} ERROR: {exc}")

                def _resolve_ref(value: Any) -> str:
                    """Resolve a link endpoint: a "#N"/"#tN" batch ref to its created id, else the id verbatim."""
                    text = str(value).strip()
                    if text.startswith("#"):
                        resolved = refs.get(text)
                        if not resolved:
                            raise ValueError(
                                f"reference {text} does not resolve (its item "
                                "errored or does not exist)"
                            )
                        return resolved
                    return text

                for idx, item in enumerate(link_items):
                    try:
                        if not isinstance(item, dict):
                            raise ValueError("each links entry must be an object")
                        source_id = _resolve_ref(item.get("source"))
                        target_id = _resolve_ref(item.get("target"))
                        lt = LinkType(str(item.get("type", "reference")).lower())
                        if lt == LinkType.INLINE:
                            raise ValueError(
                                "'inline' links are derived from prose and "
                                "cannot be created directly"
                            )
                        self.zettel_service.create_link(
                            source_id=source_id,
                            target_id=target_id,
                            link_type=lt,
                            description=item.get("description"),
                            bidirectional=bool(item.get("bidirectional", False)),
                        )
                        created_links += 1
                        lines.append(
                            f"link {idx}: {source_id} -{lt.value}-> {target_id}"
                        )
                    except Exception as exc:
                        errors += 1
                        lines.append(f"link {idx} ERROR: {exc}")

                summary = (
                    f"Batch ingest complete: {created_notes} note(s) created, "
                    f"{skipped_notes} skipped as duplicates, {created_tasks} "
                    f"task(s), {created_links} link(s)"
                )
                if flagged_notes:
                    summary += (
                        f", {flagged_notes} flagged as possible duplicates "
                        "— REVIEW REQUIRED below"
                    )
                if errors:
                    summary += f", {errors} ERROR(s) — review below"
                out = summary + ".\n\n" + "\n".join(lines)
                if review_lines:
                    out += (
                        "\n\nDuplicate review (judge each — the probe matches "
                        "topic, only you can confirm the claim):\n"
                        + "\n".join(review_lines)
                    )
                return out
            except Exception as e:
                return self.format_error_response(e)

        @self.mcp.tool(name="pzk_find_tensions")
        def pzk_find_tensions(note_id: str, limit: int = 8) -> str:
            """Surface same-topic notes NOT yet linked to a note, framed for a
            tension/agreement judgment.

            Similarity finds same-TOPIC, not same-stance: a duplicate and a
            counter-claim look identical to embeddings. This tool retrieves the
            unlinked semantic neighbors; YOU then judge each one and act:
            - same atomic claim  -> fold (update the existing note, delete the dup)
            - compatible/adds to -> link (related / supports / extends)
            - tension/conflict   -> link contradicts or refines, and consider
              reconciling the two notes' text
            Run after creating or substantially updating a note.

            Args:
                note_id: ID or title of the note to check
                limit: Maximum candidates to return (default 8)
            """
            try:
                if limit <= 0:
                    return "Limit must be greater than 0."
                note = self._resolve_note_identifier(note_id)
                if not note:
                    return self._not_found_message(note_id)

                similar = self.zettel_service.find_similar_notes(note.id, 0.3)
                linked_ids = {link.target_id for link in note.links}
                linked_ids.update(getattr(note, "inline_refs", []))
                try:
                    for neighbor in self.zettel_service.get_linked_notes(
                        note.id, "incoming"
                    ):
                        linked_ids.add(neighbor.id)
                except Exception as exc:
                    logger.debug(
                        "Could not read incoming links for tensions on %s: %s",
                        note.id,
                        exc,
                    )
                candidates = [
                    (other, score)
                    for other, score in similar
                    if other.id != note.id and other.id not in linked_ids
                ][:limit]
                if not candidates:
                    return (
                        f"No unlinked same-topic notes found for '{note.title}'. "
                        "Its semantic neighborhood is either empty or already "
                        "linked."
                    )

                out = (
                    f"Unlinked same-topic notes for '{note.title}' "
                    f"(ID: {note.id}) — judge each: same claim (fold), "
                    "compatible (link related/supports/extends), or tension "
                    "(link contradicts/refines):\n\n"
                )
                for i, (other, score) in enumerate(candidates, 1):
                    preview = other.content[:200].replace("\n", " ").strip()
                    if len(preview) > 180:
                        preview = preview[:180] + "..."
                    out += f"{i}. {other.title} (ID: {other.id}, similarity {score:.2f})\n"
                    out += f"   {preview}\n\n"
                out += (
                    "Act with pzk_create_link (judged relation), pzk_update_note "
                    "(fold/reconcile), or no action if genuinely unrelated."
                )
                return out
            except Exception as e:
                return self.format_error_response(e)

    def _register_resources(self) -> None:
        """Register MCP resources."""
        # Currently, we don't define resources for the Zettelkasten server
        pass

    def _register_prompts(self) -> None:
        """Register MCP prompts."""
        # Currently, we don't define prompts for the Zettelkasten server
        pass

    def run(self, transport: str = "stdio") -> None:
        """Run the MCP server."""
        self.mcp.run(transport=transport)
