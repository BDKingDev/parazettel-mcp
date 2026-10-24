"""Safety checks for scripts/search_eval.py (it wipes its --work-dir)."""

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_search_eval():
    script = Path(__file__).resolve().parents[1] / "scripts" / "search_eval.py"
    spec = importlib.util.spec_from_file_location("search_eval", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "work_rel, src_rel",
    [("vault", "vault"), ("vault", "vault/notes"), ("vault/notes/eval", "vault/notes")],
    ids=["same-dir", "work-contains-notes", "work-inside-notes"],
)
def test_overlapping_work_dir_is_refused_and_notes_survive(tmp_path, work_rel, src_rel):
    search_eval = _load_search_eval()
    notes = tmp_path / src_rel
    notes.mkdir(parents=True)
    (notes / "keep.md").write_text("precious")

    with pytest.raises(SystemExit, match="overlaps"):
        search_eval._prepare_environment(tmp_path / work_rel, notes)
    assert (notes / "keep.md").read_text() == "precious"


def test_seed_index_inside_work_dir_is_refused(tmp_path):
    search_eval = _load_search_eval()
    notes = tmp_path / "notes"
    notes.mkdir()
    work = tmp_path / "work"
    seed = work / "db" / "index.sqlite3"
    seed.parent.mkdir(parents=True)
    seed.write_text("seed")

    with pytest.raises(SystemExit, match="inside --work-dir"):
        search_eval._prepare_environment(work, notes, seed)
    assert seed.read_text() == "seed"
