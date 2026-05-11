"""Parazettel MCP package metadata."""

from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib  # type: ignore[no-redef]

PACKAGE_NAME = "parazettel-mcp"


def _load_version_from_pyproject() -> str:
    """Fallback to the local pyproject version for raw source checkouts."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        project_config = tomllib.load(pyproject_file)
    return project_config["project"]["version"]


def get_version() -> str:
    """Return the repo version, else installed metadata, else ``0.0.0``."""
    try:
        return _load_version_from_pyproject()
    except FileNotFoundError:
        try:
            return package_version(PACKAGE_NAME)
        except PackageNotFoundError:
            return "0.0.0"


__version__ = get_version()
