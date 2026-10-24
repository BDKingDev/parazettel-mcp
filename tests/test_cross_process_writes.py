"""Two processes writing to one vault must not lose each other's updates.

Every Claude session can open the vault directly, so concurrent writers are
separate OS processes. The index stays consistent on its own (SQLite WAL), but a
write is a markdown read-modify-write: two processes each adding a link to the
same hub note would, without the cross-process write lock, read the same file
and one would overwrite the other's link.
"""

import os
import subprocess
import sys
import textwrap

from parazettel_mcp.models.schema import Note, NoteType

_WORKER = textwrap.dedent(
    """
    import sys
    from parazettel_mcp.models.schema import LinkType
    from parazettel_mcp.services.zettel_service import ZettelService

    hub_id, go_file, *target_ids = sys.argv[1:]
    service = ZettelService()
    import os, time
    while not os.path.exists(go_file):  # start both workers together
        time.sleep(0.005)
    for target_id in target_ids:
        service.create_link(hub_id, target_id, LinkType.REFERENCE)
    service.repository.close()
    """
)


def test_concurrent_processes_keep_every_link_on_a_shared_hub(zettel_service, tmp_path):
    repo = zettel_service.repository
    hub = zettel_service.create_note(title="Hub", content="hub", note_type=NoteType.STRUCTURE)
    targets = [
        repo.create(Note(title=f"Target {i}", content=f"t{i}", note_type=NoteType.PERMANENT))
        for i in range(40)
    ]
    ids = [t.id for t in targets]
    go_file = tmp_path / "go"
    env = dict(
        os.environ,
        PARAZETTEL_NOTES_DIR=str(repo.notes_dir),
        PARAZETTEL_INDEX_DB_PATH=str(repo.index_db_path),
        PARAZETTEL_EMBEDDING_ENABLED="false",
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, hub.id, str(go_file), *chunk],
            env=env,
            stderr=subprocess.PIPE,
            text=True,
        )
        for chunk in (ids[:20], ids[20:])
    ]
    go_file.write_text("go")
    for worker in workers:
        _, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, err

    # Both layers: the hub's markdown ## Links section and the index rows.
    hub_after = repo.get(hub.id)
    assert {link.target_id for link in hub_after.links} == set(ids)
    linked = {n.id for n in repo.find_linked_notes(hub.id, "outgoing")}
    assert linked == set(ids)
    report = zettel_service.check_consistency()
    assert report["consistent"], report
