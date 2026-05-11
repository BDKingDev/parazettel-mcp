"""Serialization helpers for daemon RPC payloads."""

from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Type

from pydantic import BaseModel

from parazettel_mcp.models.schema import Link, LinkType, Note, NoteSource, NoteStatus, NoteType, Tag
from parazettel_mcp.services.search_service import SearchResult

MODEL_REGISTRY: Dict[str, Type[BaseModel]] = {
    "Note": Note,
    "Tag": Tag,
    "Link": Link,
}

ENUM_REGISTRY: Dict[str, Type[Enum]] = {
    "LinkType": LinkType,
    "NoteSource": NoteSource,
    "NoteStatus": NoteStatus,
    "NoteType": NoteType,
}


def encode_value(value: Any) -> Any:
    """Convert Python objects into JSON-safe tagged payloads."""
    if isinstance(value, SearchResult):
        return {
            "__pz_type__": "search_result",
            "data": {
                "note": encode_value(value.note),
                "score": value.score,
                "matched_terms": sorted(value.matched_terms),
                "matched_context": value.matched_context,
            },
        }
    if isinstance(value, BaseModel):
        return {
            "__pz_type__": "model",
            "model": value.__class__.__name__,
            "data": value.model_dump(mode="json"),
        }
    if isinstance(value, Enum):
        return {
            "__pz_type__": "enum",
            "enum": value.__class__.__name__,
            "value": value.value,
        }
    if isinstance(value, Path):
        return {"__pz_type__": "path", "value": str(value)}
    if isinstance(value, datetime.datetime):
        return {"__pz_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"__pz_type__": "date", "value": value.isoformat()}
    if isinstance(value, tuple):
        return {"__pz_type__": "tuple", "items": [encode_value(item) for item in value]}
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}
    return value


def decode_value(value: Any) -> Any:
    """Reconstruct Python objects from tagged daemon payloads."""
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    marker = value.get("__pz_type__")
    if marker == "model":
        model_name = value["model"]
        return MODEL_REGISTRY[model_name].model_validate(value["data"])
    if marker == "enum":
        enum_name = value["enum"]
        return ENUM_REGISTRY[enum_name](value["value"])
    if marker == "path":
        return Path(value["value"])
    if marker == "datetime":
        return datetime.datetime.fromisoformat(value["value"])
    if marker == "date":
        return datetime.date.fromisoformat(value["value"])
    if marker == "tuple":
        return tuple(decode_value(item) for item in value["items"])
    if marker == "search_result":
        data = value["data"]
        return SearchResult(
            note=decode_value(data["note"]),
            score=data["score"],
            matched_terms=set(data["matched_terms"]),
            matched_context=data["matched_context"],
        )
    return {key: decode_value(item) for key, item in value.items()}
