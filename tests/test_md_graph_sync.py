"""File <-> graph synchronization contract tests.

Three guarantees are pinned here, each verified at BOTH layers (the markdown
file on disk and the Kuzu LINKS_TO edges), because their historical failure
mode is exactly one layer silently disagreeing with the other:

1. Every kind of manual .md edit (## Links lines, inline prose refs,
   frontmatter routing/tags, new/deleted files) propagates into graph edges
   via rebuild — and check_consistency flags the drift before the rebuild.
2. MCP-level operations that imply routing links (areas, projects, tasks)
   write those links into the markdown AND the graph, on create and re-route.
3. Bidirectional link creation writes the inverse ("counter") link into the
   TARGET note's markdown and creates both graph edges with the correct
   inverse types — and bidirectional removal cleans both layers on both notes.
"""

import datetime
import os

import pytest

from parazettel_mcp.models.schema import LinkType, NoteType


# ---------------------------------------------------------------------------
# Helpers — read both layers directly
# ---------------------------------------------------------------------------


def md_text(service, note_id: str) -> str:
    """Raw markdown on disk for a note (bypasses every cache)."""
    return (service.repository.notes_dir / f"{note_id}.md").read_text(
        encoding="utf-8"
    )


def write_md(service, note_id: str, text: str) -> None:
    """Hand-edit a note file on disk, as an external editor would."""
    (service.repository.notes_dir / f"{note_id}.md").write_text(
        text, encoding="utf-8"
    )


def edge_types(service, source_id: str, target_id: str) -> set:
    """LINKS_TO edge types from source to target, read straight from Kuzu."""
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


def links_section(service, note_id: str) -> str:
    """The ## Links section of a note's file ('' when absent)."""
    text = md_text(service, note_id)
    if "## Links" not in text:
        return ""
    return text.split("## Links", 1)[1]


# ---------------------------------------------------------------------------
# 1. Manual .md edits propagate into graph edges
# ---------------------------------------------------------------------------


def test_hand_added_links_line_becomes_edge_after_rebuild(zettel_service):
    """A hand-added ## Links line becomes a graph edge after a rebuild."""
    a = zettel_service.create_note(title="Hand Edit A", content="a")
    b = zettel_service.create_note(title="Hand Edit B", content="b")
    assert edge_types(zettel_service, a.id, b.id) == set()

    text = md_text(zettel_service, a.id)
    if "## Links" in text:
        text += f"- extends [[{b.id}]]\n"
    else:
        text += f"\n## Links\n- extends [[{b.id}]]\n"
    write_md(zettel_service, a.id, text)

    # Same-ID content edits are invisible to rebuild_index_if_needed (ID set
    # unchanged) — the drift must be visible to check_consistency first.
    report = zettel_service.check_consistency()
    assert a.id in report["content_drift"]

    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == {"extends"}
    # And the parsed note agrees with the graph.
    parsed = zettel_service.get_note(a.id)
    assert {(link.target_id, link.link_type) for link in parsed.links} == {
        (b.id, LinkType.EXTENDS)
    }


def test_hand_removed_links_line_drops_edge_after_rebuild(zettel_service):
    """Removing a ## Links line by hand drops its graph edge after a rebuild."""
    a = zettel_service.create_note(title="Remover A", content="a")
    b = zettel_service.create_note(title="Remover B", content="b")
    zettel_service.create_link(a.id, b.id, LinkType.SUPPORTS)
    assert edge_types(zettel_service, a.id, b.id) == {"supports"}

    # Delete the link line from the file, as an external editor would.
    lines = [
        line
        for line in md_text(zettel_service, a.id).splitlines()
        if not (line.lstrip().startswith("- ") and b.id in line)
    ]
    write_md(zettel_service, a.id, "\n".join(lines) + "\n")

    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == set()
    assert zettel_service.get_note(a.id).links == []


def test_hand_changed_link_type_updates_edge_after_rebuild(zettel_service):
    """Editing a link's type in the file updates the edge type after a rebuild."""
    a = zettel_service.create_note(title="Retyper A", content="a")
    b = zettel_service.create_note(title="Retyper B", content="b")
    zettel_service.create_link(a.id, b.id, LinkType.REFERENCE)

    text = md_text(zettel_service, a.id).replace(
        f"- reference [[{b.id}", f"- contradicts [[{b.id}"
    )
    write_md(zettel_service, a.id, text)

    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == {"contradicts"}


def test_hand_added_inline_prose_ref_becomes_inline_edge(zettel_service):
    """An inline [[id]] added in prose becomes an inline edge, and is dropped when removed."""
    a = zettel_service.create_note(title="Prose A", content="No refs yet.")
    b = zettel_service.create_note(title="Prose B", content="b")

    text = md_text(zettel_service, a.id).replace(
        "No refs yet.", f"No refs yet. But see [[{b.id}]] for context."
    )
    write_md(zettel_service, a.id, text)
    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == {"inline"}

    # Removing the mention removes the edge again.
    text = md_text(zettel_service, a.id).replace(f" But see [[{b.id}]] for context.", "")
    write_md(zettel_service, a.id, text)
    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == set()


@pytest.mark.parametrize(
    "link_line_template, expected_type",
    [
        ("- reference [[{tid}|Some Alias]]", "reference"),  # piped alias
        ("- supports [[{tid}#section]]", "supports"),  # heading fragment
        ("- related [[{tid}.md]]", "related"),  # .md suffix
        ("- banana [[{tid}]]", "reference"),  # unknown type -> reference
    ],
)
def test_hand_written_link_target_forms_normalize(
    zettel_service, link_line_template, expected_type
):
    """Hand-written target forms (alias, #fragment, .md, unknown type) normalize to the right edge."""
    a = zettel_service.create_note(title="Form A", content="a")
    b = zettel_service.create_note(title="Form B", content="b")
    write_md(
        zettel_service,
        a.id,
        md_text(zettel_service, a.id)
        + "\n## Links\n"
        + link_line_template.format(tid=b.id)
        + "\n",
    )
    zettel_service.rebuild_index()
    assert edge_types(zettel_service, a.id, b.id) == {expected_type}


def test_hand_written_duplicate_link_lines_dedupe_to_one_edge(zettel_service):
    """Duplicate ## Links lines for the same target collapse to a single edge."""
    a = zettel_service.create_note(title="Dup A", content="a")
    b = zettel_service.create_note(title="Dup B", content="b")
    write_md(
        zettel_service,
        a.id,
        md_text(zettel_service, a.id)
        + f"\n## Links\n- reference [[{b.id}]]\n- reference [[{b.id}]]\n",
    )
    zettel_service.rebuild_index()
    with zettel_service.repository._connection() as conn:
        result = conn.execute(
            "MATCH (s:Note {id: $s})-[r:LINKS_TO]->(t:Note {id: $t}) "
            "RETURN count(r)",
            {"s": a.id, "t": b.id},
        )
        assert result.get_next()[0] == 1


def test_hand_written_created_comment_sets_edge_timestamp(zettel_service):
    """A hand-written <!-- created --> comment sets the edge's created_at."""
    a = zettel_service.create_note(title="Stamp A", content="a")
    b = zettel_service.create_note(title="Stamp B", content="b")
    stamp = "2025-01-02T03:04:05"
    write_md(
        zettel_service,
        a.id,
        md_text(zettel_service, a.id)
        + f"\n## Links\n- refines [[{b.id}]] <!-- created: {stamp} -->\n",
    )
    zettel_service.rebuild_index()
    link = next(
        lk for lk in zettel_service.get_note(a.id).links if lk.target_id == b.id
    )
    assert link.created_at == datetime.datetime.fromisoformat(stamp)
    with zettel_service.repository._connection() as conn:
        result = conn.execute(
            "MATCH (s:Note {id: $s})-[r:LINKS_TO]->(t:Note {id: $t}) "
            "RETURN r.created_at",
            {"s": a.id, "t": b.id},
        )
        assert result.get_next()[0] == datetime.datetime.fromisoformat(stamp)


def test_hand_edited_frontmatter_routing_propagates(zettel_service):
    """Editing area_id in frontmatter re-routes the note in the graph after a rebuild."""
    area1 = zettel_service.create_area_note(title="Area One", content="a1")
    area2 = zettel_service.create_area_note(title="Area Two", content="a2")
    note = zettel_service.create_note(
        title="Routed Note", content="n", area_id=area1.id
    )

    write_md(
        zettel_service,
        note.id,
        md_text(zettel_service, note.id).replace(
            f"area_id: {area1.id}", f"area_id: {area2.id}"
        ),
    )
    zettel_service.rebuild_index()
    # The graph property reflects the file, so routed search finds it.
    assert note.id in {
        n.id for n in zettel_service.search_notes(area_id=area2.id)
    }
    assert note.id not in {
        n.id for n in zettel_service.search_notes(area_id=area1.id)
    }


def test_new_hand_dropped_file_is_indexed_with_its_links(zettel_service):
    """A note file dropped on disk is indexed with its links by rebuild_index_if_needed."""
    b = zettel_service.create_note(title="Existing Target", content="b")
    new_id = "20260101T000000000000001"
    write_md(
        zettel_service,
        new_id,
        (
            "---\n"
            f"id: {new_id}\n"
            "title: Dropped In\n"
            "type: permanent\n"
            "tags: [dropped, manual]\n"
            "---\n\n"
            "# Dropped In\n\n"
            f"Body mentions [[{b.id}]] inline.\n\n"
            "## Links\n"
            f"- supports [[{b.id}]]\n"
        ),
    )

    # A new ID on disk IS visible to the cheap check — no full rebuild call.
    zettel_service.repository.rebuild_index_if_needed()

    assert edge_types(zettel_service, new_id, b.id) == {"supports"}
    found = zettel_service.search_notes(tag="dropped")
    assert [n.id for n in found] == [new_id]


def test_hand_deleted_file_drops_node_and_edges(zettel_service):
    """Deleting a note file on disk drops its node and edges and surfaces a dangling ref."""
    a = zettel_service.create_note(title="Survivor", content="a")
    doomed = zettel_service.create_note(title="Hand Deleted", content="d")
    zettel_service.create_link(a.id, doomed.id, LinkType.REFERENCE)

    os.remove(zettel_service.repository.notes_dir / f"{doomed.id}.md")
    zettel_service.repository.rebuild_index_if_needed()

    assert edge_types(zettel_service, a.id, doomed.id) == set()
    with zettel_service.repository._connection() as conn:
        result = conn.execute(
            "MATCH (n:Note {id: $id}) RETURN count(n)", {"id": doomed.id}
        )
        assert result.get_next()[0] == 0
    # The survivor's stale ## Links line is now a dangling ref the
    # consistency check reports (file scrub only happens on API deletes).
    report = zettel_service.check_consistency()
    assert f"{a.id} -> {doomed.id}" in report["dangling_refs"]


def test_hand_edited_tags_propagate_to_tag_edges(zettel_service):
    """Editing tags in frontmatter propagates to HAS_TAG edges after a rebuild."""
    note = zettel_service.create_note(
        title="Tag Edit", content="t", tags=["keep-me", "drop-me"]
    )
    text = md_text(zettel_service, note.id).replace("drop-me", "added-by-hand")
    write_md(zettel_service, note.id, text)
    zettel_service.rebuild_index()

    names = {t.name for t in zettel_service.get_all_tags()}
    assert "added-by-hand" in names
    assert "drop-me" not in names
    assert note.id in {n.id for n in zettel_service.get_notes_by_tag("added-by-hand")}


# ---------------------------------------------------------------------------
# 2. Tool-level operations wire routing links in md AND graph
# ---------------------------------------------------------------------------


@pytest.fixture
def para(zettel_service):
    """An area with a project routed to it."""
    area = zettel_service.create_area_note(title="PARA Area", content="area")
    project = zettel_service.create_project_note(
        title="PARA Project", content="project", area_id=area.id
    )
    return zettel_service, area, project


def test_create_project_wires_area_links_both_layers(para):
    """Creating a project wires its area links and the area's has_part in both layers."""
    service, area, project = para
    # Project file: part_of + reference to its area.
    section = links_section(service, project.id)
    assert f"part_of [[{area.id}" in section
    assert f"reference [[{area.id}" in section
    assert edge_types(service, project.id, area.id) == {"part_of", "reference"}
    # Area file: has_part counter link back to the project.
    assert f"has_part [[{project.id}" in links_section(service, area.id)
    assert edge_types(service, area.id, project.id) == {"has_part"}


def test_create_note_under_project_wires_routing_both_layers(para):
    """A note created under a project gets its routing links in markdown and graph."""
    service, area, project = para
    note = service.create_note(
        title="Project Knowledge", content="k", project_id=project.id
    )
    # Note inherits the project's area and links both.
    assert note.area_id == area.id
    section = links_section(service, note.id)
    assert f"part_of [[{project.id}" in section
    assert f"reference [[{area.id}" in section
    assert edge_types(service, note.id, project.id) == {"part_of"}
    assert edge_types(service, note.id, area.id) == {"reference"}
    # Project file gains the has_part counter link.
    assert f"has_part [[{note.id}" in links_section(service, project.id)
    assert edge_types(service, project.id, note.id) == {"has_part"}


def test_create_task_wires_routing_both_layers(para):
    """A task created under a project gets its routing links in markdown and graph."""
    service, area, project = para
    task = service.create_task(
        title="Routed Task", content="do it", project_id=project.id
    )
    section = links_section(service, task.id)
    assert f"part_of [[{project.id}" in section
    assert f"reference [[{area.id}" in section
    assert edge_types(service, task.id, project.id) == {"part_of"}
    assert f"has_part [[{task.id}" in links_section(service, project.id)
    assert edge_types(service, project.id, task.id) == {"has_part"}


def test_task_project_reassignment_moves_links_both_layers(para):
    """Reassigning a task's project moves its routing links in both layers."""
    service, area, project1 = para
    project2 = service.create_project_note(
        title="Second Project", content="p2", area_id=area.id
    )
    task = service.create_task(
        title="Mover Task", content="move me", project_id=project1.id
    )

    service.update_task(task.id, project_id=project2.id)

    # Task file/edges point at the new project only.
    section = links_section(service, task.id)
    assert f"part_of [[{project2.id}" in section
    assert f"part_of [[{project1.id}" not in section
    assert edge_types(service, task.id, project2.id) == {"part_of"}
    assert edge_types(service, task.id, project1.id) == set()
    # Counter links move too: old project loses has_part, new one gains it.
    assert f"has_part [[{task.id}" not in links_section(service, project1.id)
    assert f"has_part [[{task.id}" in links_section(service, project2.id)
    assert edge_types(service, project1.id, task.id) == set()
    assert edge_types(service, project2.id, task.id) == {"has_part"}


def test_note_area_reroute_moves_reference_both_layers(zettel_service):
    """Re-routing a note to a new area moves its reference/part_of links in both layers."""
    area1 = zettel_service.create_area_note(title="From Area", content="a1")
    area2 = zettel_service.create_area_note(title="To Area", content="a2")
    note = zettel_service.create_note(
        title="Rerouted", content="r", area_id=area1.id
    )

    zettel_service.update_note(note.id, area_id=area2.id)

    section = links_section(zettel_service, note.id)
    assert f"reference [[{area2.id}" in section
    assert f"part_of [[{area2.id}" in section
    assert area1.id not in section
    assert edge_types(zettel_service, note.id, area2.id) == {
        "reference",
        "part_of",
    }
    assert edge_types(zettel_service, note.id, area1.id) == set()
    # The derived has_part counter edge follows the re-route too.
    assert edge_types(zettel_service, area2.id, note.id) == {"has_part"}
    assert edge_types(zettel_service, area1.id, note.id) == set()


def test_subproject_wires_parent_and_area_both_layers(para):
    """A subproject is wired to both its parent project and inherited area in both layers."""
    service, area, parent = para
    sub = service.create_project_note(
        title="Subproject", content="s", project_id=parent.id
    )
    assert sub.area_id == area.id
    section = links_section(service, sub.id)
    assert f"part_of [[{parent.id}" in section
    assert f"part_of [[{area.id}" in section
    assert edge_types(service, sub.id, parent.id) == {"part_of"}
    assert "part_of" in edge_types(service, sub.id, area.id)
    # Both parents carry the has_part counter link.
    assert f"has_part [[{sub.id}" in links_section(service, parent.id)
    assert f"has_part [[{sub.id}" in links_section(service, area.id)
    assert edge_types(service, parent.id, sub.id) == {"has_part"}
    assert edge_types(service, area.id, sub.id) == {"has_part"}


# ---------------------------------------------------------------------------
# 2b. Direct area membership is bidirectional (part_of + materialized has_part)
# ---------------------------------------------------------------------------


def test_create_note_project_under_parent_wires_area_membership(para):
    """A project created via create_note (note_type=project) under a parent
    project must be a full member of its inherited area — part_of + has_part on
    both the parent project and the area — not just reference the area."""
    service, area, parent = para
    sub = service.create_note(
        title="Sub via create_note",
        content="s",
        note_type=NoteType.PROJECT,
        project_id=parent.id,
    )
    assert sub.area_id == area.id
    section = links_section(service, sub.id)
    assert f"part_of [[{parent.id}" in section
    assert f"part_of [[{area.id}" in section
    assert edge_types(service, sub.id, parent.id) == {"part_of"}
    assert "part_of" in edge_types(service, sub.id, area.id)
    # has_part counter link on both containers, both layers.
    assert edge_types(service, parent.id, sub.id) == {"has_part"}
    assert edge_types(service, area.id, sub.id) == {"has_part"}


def test_links_section_edit_cannot_deroute_note(zettel_service):
    """A hand-edited ## Links that omits routing links must not de-route the
    note: area reference + part_of are regenerated from frontmatter."""
    area = zettel_service.create_area_note(title="Routing Area", content="a")
    note = zettel_service.create_note(
        title="Routed", content="n", area_id=area.id
    )
    other = zettel_service.create_note(
        title="Other Note", content="o", area_id=area.id
    )

    # Hand-edit content with a ## Links section that drops BOTH area links and
    # keeps only an unrelated reference.
    new_content = f"# Routed\n\nBody.\n\n## Links\n- reference [[{other.id}]]\n"
    zettel_service.update_note(note.id, content=new_content)

    parsed = zettel_service.get_note(note.id)
    targets = {(link.target_id, link.link_type) for link in parsed.links}
    # Routing links survived the omission...
    assert (area.id, LinkType.REFERENCE) in targets
    assert (area.id, LinkType.PART_OF) in targets
    # ...and the hand-added link is present too.
    assert (other.id, LinkType.REFERENCE) in targets
    # Graph agrees with the file.
    assert {"reference", "part_of"} <= edge_types(zettel_service, note.id, area.id)


def test_area_direct_note_is_bidirectional_member(zettel_service):
    """A note routed directly to an area is a bidirectional member (part_of + materialized has_part)."""
    area = zettel_service.create_area_note(title="Member Area", content="a")
    note = zettel_service.create_note(
        title="Direct Member", content="m", area_id=area.id
    )

    # Member side: explicit part_of + reference in the file AND the graph.
    section = links_section(zettel_service, note.id)
    assert f"part_of [[{area.id}" in section
    assert f"reference [[{area.id}" in section
    assert edge_types(zettel_service, note.id, area.id) == {
        "part_of",
        "reference",
    }
    # Area side: the has_part counter link is materialized in the area's
    # markdown — reading the area file shows its members — and the edge exists.
    assert f"has_part [[{note.id}" in links_section(zettel_service, area.id)
    assert edge_types(zettel_service, area.id, note.id) == {"has_part"}
    # Both-ways traversal works.
    assert note.id in {
        n.id for n in zettel_service.get_linked_notes(area.id, "outgoing")
    }


def test_area_has_part_removed_when_note_joins_project(para):
    """An area's has_part moves to the project when a member note joins a project."""
    service, area, project = para
    note = service.create_note(
        title="Promoted Member", content="m", area_id=area.id
    )
    assert edge_types(service, area.id, note.id) == {"has_part"}
    assert f"has_part [[{note.id}" in links_section(service, area.id)

    service.update_note(note.id, project_id=project.id)

    # The project is the container now: the area's has_part moves to the
    # project in BOTH layers, part_of points at the project.
    assert edge_types(service, area.id, note.id) == set()
    assert f"has_part [[{note.id}" not in links_section(service, area.id)
    assert edge_types(service, project.id, note.id) == {"has_part"}
    assert f"has_part [[{note.id}" in links_section(service, project.id)
    section = links_section(service, note.id)
    assert f"part_of [[{project.id}" in section
    assert f"part_of [[{area.id}" not in section


def test_area_membership_survives_rebuild_and_area_update(zettel_service):
    """Area membership links survive a rebuild and an area-body update."""
    area = zettel_service.create_area_note(title="Stable Area", content="a")
    notes = [
        zettel_service.create_note(
            title=f"Member {i}", content="m", area_id=area.id
        )
        for i in range(3)
    ]

    # Membership lines live in the area's file, so a rebuild (which trusts
    # files only) reproduces every has_part edge.
    zettel_service.rebuild_index()
    for note in notes:
        assert edge_types(zettel_service, area.id, note.id) == {"has_part"}

    # Updating the area body must not disturb the membership lines.
    zettel_service.update_note(area.id, content="Updated area body.")
    for note in notes:
        assert edge_types(zettel_service, area.id, note.id) == {"has_part"}
        assert f"has_part [[{note.id}" in links_section(zettel_service, area.id)


def test_area_member_counts_via_graph(para):
    """Areas materialize has_part for projects AND direct members — all
    visible in the area's file and to graph traversal."""
    service, area, project = para
    direct = service.create_note(title="Direct", content="d", area_id=area.id)
    outgoing = {n.id for n in service.get_linked_notes(area.id, "outgoing")}
    assert {project.id, direct.id} <= outgoing
    assert f"has_part [[{project.id}" in links_section(service, area.id)
    assert f"has_part [[{direct.id}" in links_section(service, area.id)


# ---------------------------------------------------------------------------
# 3. Bidirectional links: counter link in the target's md + both edges
# ---------------------------------------------------------------------------

INVERSE_PAIRS = [
    (LinkType.EXTENDS, LinkType.EXTENDED_BY),
    (LinkType.EXTENDED_BY, LinkType.EXTENDS),
    (LinkType.REFINES, LinkType.REFINED_BY),
    (LinkType.CONTRADICTS, LinkType.CONTRADICTED_BY),
    (LinkType.QUESTIONS, LinkType.QUESTIONED_BY),
    (LinkType.SUPPORTS, LinkType.SUPPORTED_BY),
    (LinkType.REFERENCE, LinkType.REFERENCE),  # symmetric
    (LinkType.RELATED, LinkType.RELATED),  # symmetric
    (LinkType.PART_OF, LinkType.HAS_PART),
    (LinkType.BLOCKS, LinkType.BLOCKED_BY),
]


@pytest.mark.parametrize("forward, inverse", INVERSE_PAIRS)
def test_bidirectional_create_writes_counter_link_md_and_edges(
    zettel_service, forward, inverse
):
    """A bidirectional link writes the inverse counter link into the target's markdown and both edges."""
    src = zettel_service.create_note(title=f"Src {forward.value}", content="s")
    tgt = zettel_service.create_note(title=f"Tgt {forward.value}", content="t")

    zettel_service.create_link(src.id, tgt.id, forward, bidirectional=True)

    # Forward: source file line + edge.
    assert f"- {forward.value} [[{tgt.id}" in links_section(zettel_service, src.id)
    assert edge_types(zettel_service, src.id, tgt.id) == {forward.value}
    # Counter: TARGET file carries the inverse line, and the reverse edge
    # exists with the inverse type.
    assert f"- {inverse.value} [[{src.id}" in links_section(zettel_service, tgt.id)
    assert edge_types(zettel_service, tgt.id, src.id) == {inverse.value}


@pytest.mark.parametrize(
    "forward, inverse",
    [
        (LinkType.EXTENDS, LinkType.EXTENDED_BY),
        (LinkType.REFERENCE, LinkType.REFERENCE),
    ],
)
def test_bidirectional_remove_cleans_both_layers(zettel_service, forward, inverse):
    """Bidirectional link removal cleans the markdown and the graph on both notes."""
    src = zettel_service.create_note(title="Unlink Src", content="s")
    tgt = zettel_service.create_note(title="Unlink Tgt", content="t")
    zettel_service.create_link(src.id, tgt.id, forward, bidirectional=True)

    zettel_service.remove_link(src.id, tgt.id, link_type=forward, bidirectional=True)

    assert edge_types(zettel_service, src.id, tgt.id) == set()
    assert edge_types(zettel_service, tgt.id, src.id) == set()
    assert tgt.id not in links_section(zettel_service, src.id)
    assert src.id not in links_section(zettel_service, tgt.id)


def test_counter_links_survive_rebuild(zettel_service):
    """The inverse link lives in the target's FILE, so a rebuild (which trusts
    files only) must reproduce both edges — pinning that counter links are
    durably persisted, not graph-only artifacts."""
    src = zettel_service.create_note(title="Durable Src", content="s")
    tgt = zettel_service.create_note(title="Durable Tgt", content="t")
    zettel_service.create_link(src.id, tgt.id, LinkType.SUPPORTS, bidirectional=True)

    zettel_service.rebuild_index()

    assert edge_types(zettel_service, src.id, tgt.id) == {"supports"}
    assert edge_types(zettel_service, tgt.id, src.id) == {"supported_by"}
