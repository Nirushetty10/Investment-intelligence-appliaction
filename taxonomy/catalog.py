"""Read-only, version-aware queries over the generated taxonomy catalog."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("taxonomy_catalog.json")


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    with CATALOG_PATH.open("r", encoding="utf-8") as stream:
        catalog = json.load(stream)
    if catalog.get("catalog_format_version") != 1 or not isinstance(catalog.get("taxonomies"), dict):
        raise ValueError(f"Unsupported or malformed taxonomy catalog: {CATALOG_PATH}")
    return catalog


def list_taxonomy_versions(taxonomy_id: str) -> list[str]:
    return sorted(load_catalog().get("taxonomies", {}).get(taxonomy_id, {}).keys())


def get_taxonomy_entry(taxonomy_id: str | None, version: str | None) -> dict[str, Any] | None:
    if not taxonomy_id or not version:
        return None
    return load_catalog().get("taxonomies", {}).get(taxonomy_id, {}).get(version)


def taxonomy_version_status(taxonomy_id: str | None, version: str | None) -> str:
    """Return EXACT_VERSION_AVAILABLE, VERSION_UNAVAILABLE, or TAXONOMY_UNSUPPORTED."""
    if not taxonomy_id or taxonomy_id not in load_catalog().get("taxonomies", {}):
        return "TAXONOMY_UNSUPPORTED"
    if version and get_taxonomy_entry(taxonomy_id, version) is not None:
        return "EXACT_VERSION_AVAILABLE"
    return "VERSION_UNAVAILABLE"


def get_concept_metadata(taxonomy_id: str | None, version: str | None, concept: str | None) -> dict[str, Any] | None:
    entry = get_taxonomy_entry(taxonomy_id, version)
    if entry is None or not concept:
        return None
    return entry.get("concepts", {}).get(concept)


def concept_status(taxonomy_id: str | None, version: str | None, concept: str | None) -> str:
    status = taxonomy_version_status(taxonomy_id, version)
    if status != "EXACT_VERSION_AVAILABLE":
        return "TAXONOMY_VERSION_UNAVAILABLE" if status == "VERSION_UNAVAILABLE" else "TAXONOMY_UNSUPPORTED"
    return "CONFIRMED" if get_concept_metadata(taxonomy_id, version, concept) is not None else "NOT_IN_APPLICABLE_TAXONOMY"


def get_versioned_period_type(taxonomy_id: str | None, version: str | None, concept: str | None) -> str | None:
    metadata = get_concept_metadata(taxonomy_id, version, concept)
    period_type = metadata.get("period_type") if metadata else None
    return period_type if period_type in {"instant", "duration"} else None
