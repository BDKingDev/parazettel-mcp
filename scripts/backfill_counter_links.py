#!/usr/bin/env python3
"""Backfill routing counter links across an existing parazettel vault.

Brings pre-existing notes up to the bidirectional-membership contract that new
writes now follow:

* Every project-routed note/task carries ``part_of`` -> project, and the
  project carries a materialized ``has_part`` counter link back.
* Every directly area-routed note carries ``reference`` AND ``part_of`` ->
  area. The area-side ``has_part`` counter edge is graph-derived from the
  member's ``area_id`` (never written into the area's markdown), so it is
  produced by the final index rebuild rather than by file edits.
* Optionally (``--semantic-inverses``) every directional semantic link
  (extends/refines/contradicts/questions/supports and their ``_by`` forms)
  gains its inverse on the target note.

Safety:
* Dry-run by default — prints the plan and touches nothing. Pass ``--apply``.
* On apply, the notes directory is first copied to a timestamped sibling
  backup (``<notes-dir>-backup-<ts>``).
* Writes go through ZettelService, so files and index stay in lockstep, and
  the script refuses to run when the graph is read-only (stop the daemon
  first: ``python -m parazettel_mcp.main --stop-daemon``).

Usage
-----
    python scripts/backfill_counter_links.py [--notes-dir PATH]
        [--graph-db-path PATH] [--apply] [--semantic-inverses]
        [--skip-rebuild]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("backfill_counter_links")

# Directional semantic pairs eligible for --semantic-inverses. Symmetric types
# (reference, related) are deliberately excluded: backfilling "reference back"
# would add one line to an area/hub for every note that references it — the
# exact markdown bloat the derived-membership design avoids.
_SEMANTIC_INVERSES = {
    "extends": "extended_by",
    "extended_by": "extends",
    "refines": "refined_by",
    "refined_by": "refines",
    "contradicts": "contradicted_by",
    "contradicted_by": "contradicts",
    "questions": "questioned_by",
    "questioned_by": "questions",
    "supports": "supported_by",
    "supported_by": "supports",
}


@dataclass
class LinkAction:
    source_id: str
    target_id: str
    link_type: str
    reason: str
    bidirectional: bool = False


@dataclass
class BackfillPlan:
    actions: List[LinkAction] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    area_member_counts: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        by_reason = Counter(action.reason for action in self.actions)
        lines = [f"Planned link additions: {len(self.actions)}"]
        for reason, count in by_reason.most_common():
            lines.append(f"  {count:5d}  {reason}")
        if self.area_member_counts:
            lines.append("Direct members per area (derived has_part edges "
                         "created by the final rebuild):")
            for area, count in self.area_member_counts.most_common():
                lines.append(f"  {count:5d}  {area}")
        if self.skipped:
            lines.append(f"Skipped {len(self.skipped)} note(s):")
            lines.extend(f"  - {entry}" for entry in self.skipped[:20])
            if len(self.skipped) > 20:
                lines.append(f"  ... (+{len(self.skipped) - 20} more)")
        return "\n".join(lines)


def plan_backfill(service, semantic_inverses: bool = False) -> BackfillPlan:
    """Compute the link additions needed to satisfy the counter-link contract."""
    from parazettel_mcp.models.schema import LinkType, NoteType

    repository = service.repository
    plan = BackfillPlan()
    note_cache: Dict[str, Optional[object]] = {}

    def get_note(note_id: str):
        if note_id not in note_cache:
            try:
                note_cache[note_id] = repository.get(note_id)
            except Exception:
                note_cache[note_id] = None
        return note_cache[note_id]

    def has_link(note, target_id: str, link_type: LinkType) -> bool:
        return any(
            link.target_id == target_id and link.link_type == link_type
            for link in note.links
        )

    note_ids = sorted(p.stem for p in repository.notes_dir.glob("*.md"))
    for note_id in note_ids:
        note = get_note(note_id)
        if note is None:
            plan.skipped.append(f"{note_id}: unparseable")
            continue
        if note.note_type == NoteType.AREA:
            continue

        if note.project_id:
            project = get_note(note.project_id)
            if project is None or project.note_type != NoteType.PROJECT:
                plan.skipped.append(
                    f"{note.id}: project_id {note.project_id} invalid"
                )
            else:
                if not has_link(note, project.id, LinkType.PART_OF) or not has_link(
                    project, note.id, LinkType.HAS_PART
                ):
                    plan.actions.append(
                        LinkAction(
                            source_id=note.id,
                            target_id=project.id,
                            link_type=LinkType.PART_OF.value,
                            reason="project membership (part_of + has_part)",
                            bidirectional=True,
                        )
                    )
        elif note.area_id and note.area_id != note.id:
            area = get_note(note.area_id)
            if area is None or area.note_type != NoteType.AREA:
                plan.skipped.append(f"{note.id}: area_id {note.area_id} invalid")
            else:
                plan.area_member_counts[area.title] += 1
                if not has_link(note, area.id, LinkType.REFERENCE):
                    plan.actions.append(
                        LinkAction(
                            source_id=note.id,
                            target_id=area.id,
                            link_type=LinkType.REFERENCE.value,
                            reason="area membership (reference)",
                        )
                    )
                if not has_link(note, area.id, LinkType.PART_OF):
                    plan.actions.append(
                        LinkAction(
                            source_id=note.id,
                            target_id=area.id,
                            link_type=LinkType.PART_OF.value,
                            reason="area membership (part_of)",
                        )
                    )

        if semantic_inverses:
            for link in note.links:
                inverse = _SEMANTIC_INVERSES.get(link.link_type.value)
                if not inverse:
                    continue
                # Skip routing targets — their counter links are handled above.
                if link.target_id in (note.area_id, note.project_id, note.id):
                    continue
                target = get_note(link.target_id)
                if target is None:
                    continue
                if not has_link(target, note.id, LinkType(inverse)):
                    plan.actions.append(
                        LinkAction(
                            source_id=target.id,
                            target_id=note.id,
                            link_type=inverse,
                            reason=f"semantic inverse ({link.link_type.value} -> {inverse})",
                        )
                    )

    # The same inverse can be planned twice (once from each endpoint when
    # both directions of a pair are missing) — dedupe by (src, tgt, type).
    seen = set()
    deduped: List[LinkAction] = []
    for action in plan.actions:
        key = (action.source_id, action.target_id, action.link_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    plan.actions = deduped
    return plan


def apply_plan(service, plan: BackfillPlan) -> Dict[str, int]:
    """Execute the planned link additions through the service layer."""
    from parazettel_mcp.models.schema import LinkType

    results = Counter()
    total = len(plan.actions)
    for i, action in enumerate(plan.actions, 1):
        try:
            service.create_link(
                source_id=action.source_id,
                target_id=action.target_id,
                link_type=LinkType(action.link_type),
                bidirectional=action.bidirectional,
            )
            results["applied"] += 1
        except Exception as exc:
            results["failed"] += 1
            logger.error(
                "Failed %s -%s-> %s: %s",
                action.source_id,
                action.link_type,
                action.target_id,
                exc,
            )
        if i % 100 == 0 or i == total:
            logger.info("  applied %d/%d link additions", i, total)
    return dict(results)


def backup_notes_dir(notes_dir: Path) -> Path:
    """Copy the notes directory to a timestamped sibling before mutating it."""
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = notes_dir.parent / f"{notes_dir.name}-backup-{stamp}"
    shutil.copytree(notes_dir, backup)
    return backup


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-dir", type=str, default=None)
    parser.add_argument("--graph-db-path", type=str, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the planned links (default: dry-run report only)",
    )
    parser.add_argument(
        "--semantic-inverses",
        action="store_true",
        help="Also backfill inverses for directional semantic links",
    )
    parser.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="Skip the final index rebuild (derived area has_part edges will "
        "then only appear after the next rebuild)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from parazettel_mcp.config import config

    if args.notes_dir:
        config.notes_dir = Path(args.notes_dir)
    if args.graph_db_path:
        config.graph_db_path = Path(args.graph_db_path)

    from parazettel_mcp.services.zettel_service import ZettelService

    service = ZettelService()
    try:
        if service.repository.read_only:
            logger.error(
                "Graph database is read-only (another process holds it). "
                "Stop the daemon first: python -m parazettel_mcp.main --stop-daemon"
            )
            return 1

        logger.info("Scanning vault at %s ...", service.repository.notes_dir)
        plan = plan_backfill(service, semantic_inverses=args.semantic_inverses)
        logger.info("%s", plan.summary())

        if not args.apply:
            logger.info(
                "\nDry run — nothing written. Re-run with --apply to execute."
            )
            return 0
        if not plan.actions:
            logger.info("Nothing to do.")
            return 0

        backup = backup_notes_dir(service.repository.notes_dir)
        logger.info("Backed up notes to %s", backup)

        results = apply_plan(service, plan)
        logger.info("Applied: %s", results)

        if not args.skip_rebuild:
            logger.info(
                "Rebuilding index (derives area has_part membership edges)..."
            )
            service.rebuild_index()
            logger.info("Rebuild complete.")
        return 0 if not results.get("failed") else 2
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
