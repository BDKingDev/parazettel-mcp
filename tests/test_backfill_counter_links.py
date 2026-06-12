"""Tests for scripts/backfill_counter_links.py — the vault counter-link backfill."""

import importlib.util
import sys
from pathlib import Path

import pytest

from parazettel_mcp.models.schema import LinkType


def _load_backfill_module():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "backfill_counter_links.py"
    )
    spec = importlib.util.spec_from_file_location("backfill_counter_links", script)
    module = importlib.util.module_from_spec(spec)
    # Dataclass machinery resolves cls.__module__ through sys.modules; register
    # the module before exec so @dataclass definitions inside it work.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_backfill_module()


def _strip_links_lines(service, note_id: str, *link_types: str) -> None:
    """Hand-edit a note file to remove ## Links lines of the given types,
    simulating a legacy vault created before the counter-link contract."""
    path = service.repository.notes_dir / f"{note_id}.md"
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not any(line.lstrip().startswith(f"- {lt} ") for lt in link_types)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def edge_types(service, source_id: str, target_id: str) -> set:
    with service.repository._connection() as conn:
        result = conn.execute(
            "MATCH (s:Note {id: $s})-[r:LINKS_TO]->(t:Note {id: $t}) "
            "RETURN r.link_type",
            {"s": source_id, "t": target_id},
        )
        types = set()
        while result.has_next():
            types.add(result.get_next()[0])
        return types


@pytest.fixture
def legacy_vault(zettel_service):
    """A vault in the pre-contract state: area members lack part_of, a project
    member lacks part_of/has_part, and a directional semantic link lacks its
    inverse."""
    area = zettel_service.create_area_note(title="Legacy Area", content="a")
    project = zettel_service.create_project_note(
        title="Legacy Project", content="p", area_id=area.id
    )
    member = zettel_service.create_note(
        title="Legacy Member", content="m", area_id=area.id
    )
    proj_note = zettel_service.create_note(
        title="Legacy Project Note", content="pn", project_id=project.id
    )
    src = zettel_service.create_note(title="Claim", content="c", area_id=area.id)
    tgt = zettel_service.create_note(title="Base", content="b", area_id=area.id)
    zettel_service.create_link(src.id, tgt.id, LinkType.EXTENDS)  # one-way

    # Regress the files to the legacy state by hand, then rebuild so the
    # graph matches the legacy files exactly.
    _strip_links_lines(zettel_service, member.id, "part_of")
    _strip_links_lines(zettel_service, src.id, "part_of")
    _strip_links_lines(zettel_service, tgt.id, "part_of")
    _strip_links_lines(zettel_service, proj_note.id, "part_of")
    _strip_links_lines(zettel_service, project.id, "has_part")
    zettel_service.rebuild_index()

    assert edge_types(zettel_service, member.id, area.id) == {"reference"}
    assert edge_types(zettel_service, proj_note.id, project.id) == set()
    return zettel_service, area, project, member, proj_note, src, tgt


def test_plan_finds_all_missing_counter_links(legacy_vault):
    service, area, project, member, proj_note, src, tgt = legacy_vault
    plan = backfill.plan_backfill(service)

    planned = {(a.source_id, a.target_id, a.link_type) for a in plan.actions}
    # Area members regain part_of (reference already present, not re-planned).
    assert (member.id, area.id, "part_of") in planned
    assert (src.id, area.id, "part_of") in planned
    assert (tgt.id, area.id, "part_of") in planned
    assert (member.id, area.id, "reference") not in planned
    # Project membership is restored bidirectionally.
    assert (proj_note.id, project.id, "part_of") in planned
    # No semantic inverses unless requested.
    assert not any(a.link_type == "extended_by" for a in plan.actions)
    # The dry-run summary reports area membership scale.
    assert "Legacy Area" in plan.summary()


def test_plan_with_semantic_inverses(legacy_vault):
    service, area, project, member, proj_note, src, tgt = legacy_vault
    plan = backfill.plan_backfill(service, semantic_inverses=True)
    planned = {(a.source_id, a.target_id, a.link_type) for a in plan.actions}
    assert (tgt.id, src.id, "extended_by") in planned


def test_apply_plan_restores_contract_both_layers(legacy_vault):
    service, area, project, member, proj_note, src, tgt = legacy_vault
    plan = backfill.plan_backfill(service)

    results = backfill.apply_plan(service, plan)
    assert results.get("failed", 0) == 0
    service.rebuild_index()  # derives the area has_part membership edges

    # Member side: part_of + reference in file and graph.
    member_md = (service.repository.notes_dir / f"{member.id}.md").read_text(
        encoding="utf-8"
    )
    assert f"part_of [[{area.id}" in member_md
    assert edge_types(service, member.id, area.id) == {"reference", "part_of"}
    # Area side: derived has_part edge, nothing materialized in the area file.
    assert edge_types(service, area.id, member.id) == {"has_part"}
    area_md = (service.repository.notes_dir / f"{area.id}.md").read_text(
        encoding="utf-8"
    )
    assert member.id not in area_md
    # Project membership restored bidirectionally in file and graph.
    assert edge_types(service, proj_note.id, project.id) == {"part_of"}
    assert edge_types(service, project.id, proj_note.id) == {"has_part"}
    project_md = (service.repository.notes_dir / f"{project.id}.md").read_text(
        encoding="utf-8"
    )
    assert f"has_part [[{proj_note.id}" in project_md

    # Idempotent: a second plan finds nothing left to do.
    assert backfill.plan_backfill(service).actions == []


def test_backup_copies_notes_dir(legacy_vault, tmp_path):
    service = legacy_vault[0]
    backup = backfill.backup_notes_dir(service.repository.notes_dir)
    try:
        original = sorted(
            p.name for p in service.repository.notes_dir.glob("*.md")
        )
        copied = sorted(p.name for p in backup.glob("*.md"))
        assert original == copied
    finally:
        import shutil

        shutil.rmtree(backup, ignore_errors=True)
