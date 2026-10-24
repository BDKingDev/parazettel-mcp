"""Tests for config defaults and the index-path derivation."""

from pathlib import Path

from parazettel_mcp.config import (
    DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS,
    DEFAULT_DEDUP_MIN_BM25,
    DEFAULT_INDEX_DB_NAME,
    ZettelkastenConfig,
)


def test_daemon_idle_timeout_field_uses_constant():
    """The idle-timeout field defaults to the code constant."""
    assert (
        ZettelkastenConfig().daemon_idle_timeout_seconds
        == DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS
    )


def test_daemon_idle_timeout_constant_is_one_hour():
    """Guard the chosen idle-timeout so an accidental edit is caught."""
    assert DEFAULT_DAEMON_IDLE_TIMEOUT_SECONDS == 3600.0


def test_dedup_min_bm25_field_uses_constant(monkeypatch):
    monkeypatch.delenv("PARAZETTEL_DEDUP_MIN_BM25", raising=False)
    assert ZettelkastenConfig().dedup_min_bm25 == DEFAULT_DEDUP_MIN_BM25


def test_legacy_kuzu_path_maps_to_sibling_index(tmp_path, monkeypatch):
    """An old config that only names graph.kuzu gets an index beside it —
    never the Kuzu file itself, never the pre-Kuzu parazettel.db."""
    monkeypatch.delenv("PARAZETTEL_INDEX_DB_PATH", raising=False)
    cfg = ZettelkastenConfig(graph_db_path=tmp_path / "db" / "graph.kuzu")
    assert cfg.get_index_db_path() == tmp_path / "db" / DEFAULT_INDEX_DB_NAME
    assert DEFAULT_INDEX_DB_NAME != "parazettel.db"


def test_explicit_index_path_wins(tmp_path):
    cfg = ZettelkastenConfig(
        index_db_path=tmp_path / "custom.sqlite3",
        graph_db_path=tmp_path / "graph.kuzu",
    )
    assert cfg.get_index_db_path() == tmp_path / "custom.sqlite3"
    assert cfg.get_graph_db_path() == cfg.get_index_db_path()  # deprecated alias


def test_non_kuzu_legacy_path_is_used_as_is(tmp_path, monkeypatch):
    monkeypatch.delenv("PARAZETTEL_INDEX_DB_PATH", raising=False)
    cfg = ZettelkastenConfig(graph_db_path=tmp_path / "index.db")
    assert cfg.get_index_db_path() == tmp_path / "index.db"


def test_relative_index_path_resolves_against_base_dir(tmp_path):
    cfg = ZettelkastenConfig(base_dir=tmp_path, index_db_path=Path("db/i.sqlite3"))
    assert cfg.get_index_db_path() == tmp_path / "db" / "i.sqlite3"
