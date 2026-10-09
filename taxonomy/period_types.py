"""Version-aware XBRL ``periodType`` metadata access.

The checked-in taxonomy catalog is generated from the official XSD and
linkbase files supplied with the project. An exact family/version match is
preferred. The older single-taxonomy period catalog is retained only as a
compatibility fallback when that exact taxonomy version is not supplied; the
normalizer must surface that mismatch as a quality warning.
"""
from functools import lru_cache
import json
from pathlib import Path

from taxonomy.catalog import get_taxonomy_entry, get_versioned_period_type

CATALOG_PATH = Path(__file__).with_name("period_type_catalog.json")


@lru_cache(maxsize=1)
def _load_catalog() -> dict:
    with CATALOG_PATH.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def get_concept_period_type(concept: str, taxonomy_id: str | None = None, version: str | None = None):
    """Return the taxonomy-declared ``instant`` / ``duration`` type, if known.

    If an exact family/version entry exists, absence of a concept/type means
    unknown for that version and no cross-version fallback is attempted.
    When the exact package is unavailable, the legacy period catalog remains a
    defensive compatibility fallback, and callers must expose the mismatch.
    """
    if taxonomy_id and version:
        exact_entry = get_taxonomy_entry(taxonomy_id, version)
        if exact_entry is not None:
            return get_versioned_period_type(taxonomy_id, version, concept)
    return _load_catalog().get("concepts", {}).get(concept)


def get_period_type_catalog_version() -> str:
    """Version of the legacy fallback catalog, not the version of every package."""
    return _load_catalog()["source_taxonomy_version"]


def get_period_type_catalog_namespace() -> str:
    return _load_catalog()["source_namespace"]


def get_period_type_catalog_scope() -> str:
    return _load_catalog().get("metadata_scope", "")
