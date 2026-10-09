"""Version-aware NSE taxonomy identity resolution.

Sector resolution uses filing schemaRef + namespace declaration evidence.
The common ``.../xbrl/<date>/in-capmkt`` namespace is shared by multiple
sector packages and is therefore not sufficient to infer the sector by itself.
Concept catalog availability is tracked separately from filing identity: an
identified 2026 filing may still need an exact 2026 taxonomy catalog before
all mappings can be trusted.
"""
from dataclasses import dataclass
import re
from typing import Optional

from taxonomy.catalog import concept_status as _catalog_concept_status, get_concept_metadata as _get_catalog_concept_metadata, taxonomy_version_status as _taxonomy_catalog_version_status
from taxonomy.manifests import (
    IND_AS_OTHER_THAN_BANKS_CONFIRMED_CONCEPTS,
    KNOWN_INVALID_CONCEPTS,
)

_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
_CORE_NAMESPACE_RE = re.compile(
    r"^https?://www\.sebi\.gov\.in/xbrl/(?P<version>\d{4}-\d{2}-\d{2})/in-capmkt/?$"
)


@dataclass(frozen=True)
class TaxonomyIdentity:
    taxonomy_id: str
    label: str
    version: str
    namespace: str
    standard_namespace_prefixes: tuple
    # Version of the supplied package catalog available to this repo. This
    # does not imply the catalog has been imported as a complete runtime map.
    source_package_version: Optional[str] = None
    # A taxonomy family can be identified before its full canonical mapping
    # has been validated. Only explicitly enabled families may populate
    # canonical financial columns.
    canonical_mapping_enabled: bool = False


# These markers are sourced from the entry-point namespace/schema names in
# the taxonomy packages provided with this project. They are not inferred
# from a company's name or from financial concept labels.
_TAXONOMY_PROFILES = (
    {
        "taxonomy_id": "ind_as_other_than_banks",
        "label": "SEBI Integrated Filing — Ind AS (Other than Banks)",
        "marker": "IntegratedFinance_IndAS",
        "source_package_version": "2025-01-31",
        "canonical_mapping_enabled": True,
    },
    {
        "taxonomy_id": "other_than_banks",
        "label": "SEBI Integrated Filing — Other than Banks",
        "marker": "IntegratedFinance_OtherThanBank",
        "canonical_mapping_enabled": False,
        "source_package_version": "2025-01-31",
    },
    {
        "taxonomy_id": "banking",
        "label": "SEBI Integrated Filing — Banking",
        "marker": "IntegratedFinance_Banking",
        "canonical_mapping_enabled": False,
        "source_package_version": "2025-01-31",
    },
    {
        "taxonomy_id": "nbfc",
        "label": "SEBI Integrated Filing — NBFC",
        "marker": "IntegratedFinance_NBFC",
        "canonical_mapping_enabled": False,
        "source_package_version": "2025-01-31",
    },
    {
        "taxonomy_id": "general_insurance",
        "label": "SEBI Integrated Filing — General Insurance",
        "marker": "IntegratedFinance_GI",
        "canonical_mapping_enabled": False,
        "source_package_version": "2025-01-31",
    },
    {
        "taxonomy_id": "life_insurance",
        "label": "SEBI Integrated Filing — Life Insurance",
        "marker": "IntegratedFinance_LI",
        "canonical_mapping_enabled": False,
        "source_package_version": "2025-01-31",
    },
    {
        "taxonomy_id": "reits_invit",
        "label": "SEBI Financial Results — REITs / InvITs",
        "marker": "Financial_Results_REITs_InvITs",
        "canonical_mapping_enabled": False,
        "source_package_version": "2026-01-31",
    },
)


def _profile_from_namespace_uri(namespace_uri: str) -> Optional[dict]:
    """Match an official SEBI taxonomy-family namespace, not arbitrary substrings."""
    if not namespace_uri:
        return None
    matched = []
    for profile in _TAXONOMY_PROFILES:
        pattern = (
            r"^https?://www\.sebi\.gov\.in/xbrl/"
            + re.escape(profile["marker"])
            + r"/(?P<date>\d{4}-\d{2}-\d{2})(?:/|$)"
        )
        if re.match(pattern, namespace_uri):
            matched.append(profile)
    return matched[0] if len(matched) == 1 else None


def _profiles_from_schema_ref(schema_ref: str) -> list:
    """Recognize family names only when a schemaRef path explicitly names one."""
    if not schema_ref:
        return []
    matches = []
    for profile in _TAXONOMY_PROFILES:
        marker = re.escape(profile["marker"])
        if re.search(r"(?:^|[/\\])" + marker + r"(?:[/\\]|$)", schema_ref):
            matches.append(profile)
    return matches


def _version_for_profile(profile: dict, signals) -> Optional[str]:
    marker = profile["marker"]
    for signal in signals:
        if _profile_from_namespace_uri(signal) != profile:
            continue
        # Prefer the date immediately following the family marker in the
        # official namespace, e.g. IntegratedFinance_IndAS/2026-01-31/...
        match = re.search(re.escape(marker) + r"/(?P<date>\d{4}-\d{2}-\d{2})", signal)
        if match:
            return match.group("date")
    # The shared core namespace's date is still useful once family identity
    # has independently been established by an entry-point namespace.
    for signal in signals:
        match = _CORE_NAMESPACE_RE.match(signal)
        if match:
            return match.group("version")
    for signal in signals:
        match = _DATE_RE.search(signal)
        if match:
            return match.group("date")
    return profile["source_package_version"]


class TaxonomyRegistry:
    """Resolve sector/version from actual filing-level structural evidence."""

    def __init__(self):
        self._profiles = _TAXONOMY_PROFILES
        # The concepts in this manifest were observed in a real 2026 Ind AS
        # filing. They are evidence of use, not a complete taxonomy manifest.
        self._confirmed_concepts = {
            "ind_as_other_than_banks": IND_AS_OTHER_THAN_BANKS_CONFIRMED_CONCEPTS,
        }

    def resolve_by_namespace(self, namespace_uri: Optional[str]) -> Optional[TaxonomyIdentity]:
        """Resolve only family-specific namespaces; shared core namespaces are ambiguous."""
        if not namespace_uri:
            return None
        profile = _profile_from_namespace_uri(namespace_uri)
        if profile is None:
            # The common core namespace appears in multiple supplied sector
            # packages. Do not use it alone to select a sector.
            return None
        version = _version_for_profile(profile, [namespace_uri])
        core_namespace = f"http://www.sebi.gov.in/xbrl/{version}/in-capmkt"
        return TaxonomyIdentity(
            taxonomy_id=profile["taxonomy_id"],
            label=profile["label"],
            version=version,
            namespace=core_namespace,
            standard_namespace_prefixes=("in-capmkt",),
            source_package_version=profile["source_package_version"],
            canonical_mapping_enabled=profile.get("canonical_mapping_enabled", False),
        )

    def resolve_by_short_prefix(self, prefix: Optional[str]) -> Optional[TaxonomyIdentity]:
        """A generic prefix such as in-capmkt is ambiguous and cannot resolve a sector."""
        if not prefix:
            return None
        # Prefixes are document-local and not reliable taxonomy identity.
        # Preserve only explicit family-bearing prefix hints when present.
        matching_profiles = [profile for profile in self._profiles if profile["marker"] == prefix]
        profile = matching_profiles[0] if len(matching_profiles) == 1 else None
        if profile is None:
            return None
        version = profile["source_package_version"]
        return TaxonomyIdentity(
            taxonomy_id=profile["taxonomy_id"],
            label=profile["label"],
            version=version,
            namespace=f"http://www.sebi.gov.in/xbrl/{version}/in-capmkt",
            standard_namespace_prefixes=("in-capmkt",),
            source_package_version=profile["source_package_version"],
            canonical_mapping_enabled=profile.get("canonical_mapping_enabled", False),
        )

    def is_standard_namespace(self, namespace_uri: Optional[str]) -> bool:
        """Whether a namespace matches a supplied NSE core or entry-point namespace family."""
        if not namespace_uri:
            return False
        if _CORE_NAMESPACE_RE.match(namespace_uri):
            return True
        return _profile_from_namespace_uri(namespace_uri) is not None

    def confirmation_status(self, taxonomy_id: str, concept: str, version: Optional[str] = None) -> str:
        """Report concept status, optionally against an exact taxonomy version.

        Without ``version`` retain the legacy test/report contract. With a
        version, a concept is confirmed only when present in the imported
        taxonomy catalog for that exact family/version. Version-unavailable is
        distinct from a concept absent from a known version.
        """
        if concept in KNOWN_INVALID_CONCEPTS:
            return "known_invalid"
        if version is not None:
            status = _catalog_concept_status(taxonomy_id, version, concept)
            return {
                "CONFIRMED": "confirmed",
                "NOT_IN_APPLICABLE_TAXONOMY": "not_in_taxonomy",
                "TAXONOMY_VERSION_UNAVAILABLE": "taxonomy_version_unavailable",
                "TAXONOMY_UNSUPPORTED": "taxonomy_unsupported",
            }.get(status, "unverified")
        confirmed = self._confirmed_concepts.get(taxonomy_id, frozenset())
        if concept in confirmed:
            return "confirmed"
        return "unverified"

    def taxonomy_catalog_status(self, taxonomy_id: str, version: str) -> str:
        """Return exact-version availability without confusing identity with mapping approval."""
        return _taxonomy_catalog_version_status(taxonomy_id, version)

    def concept_metadata(self, taxonomy_id: str, version: str, concept: str):
        """Return authoritative metadata only for an exact imported version."""
        return _get_catalog_concept_metadata(taxonomy_id, version, concept)

    def known_invalid_reason(self, concept: str) -> Optional[str]:
        return KNOWN_INVALID_CONCEPTS.get(concept)

    def resolve_document(self, doc) -> Optional[TaxonomyIdentity]:
        """Resolve using schemaRef + declared namespaces, rejecting ambiguity.

        A bare schema filename such as ``in-capmkt-ent-2025-01-31.xsd`` is
        not enough to identify a sector. The family-bearing extension namespace
        normally supplies the independent evidence.
        """
        namespace_map = getattr(doc, "namespace_map", {}) or {}
        namespace_uris = {uri for uri in namespace_map.values() if uri}
        namespace_uris.update(f.namespace for f in doc.facts if f.namespace)
        schema_refs = set(getattr(doc, "schema_refs", ()) or ())
        signals = namespace_uris | schema_refs

        namespace_profiles = {
            profile["taxonomy_id"]: profile
            for profile in self._profiles
            for namespace_uri in namespace_uris
            if _profile_from_namespace_uri(namespace_uri) == profile
        }
        schema_profiles = {
            profile["taxonomy_id"]: profile
            for schema_ref in schema_refs
            for profile in _profiles_from_schema_ref(schema_ref)
        }
        # Family-specific namespace declarations are the primary evidence.
        # A schemaRef can corroborate a family, but a generic core namespace
        # or a bare in-capmkt-ent filename cannot disambiguate sector packages.
        matched_profiles = namespace_profiles or schema_profiles
        if len(matched_profiles) != 1:
            return None
        profile = next(iter(matched_profiles.values()))
        if schema_profiles and set(schema_profiles) != {profile["taxonomy_id"]}:
            return None

        version = _version_for_profile(profile, signals)
        if not version:
            return None

        core_versions = {
            match.group("version")
            for signal in namespace_uris
            if (match := _CORE_NAMESPACE_RE.match(signal))
        }
        family_versions = set()
        marker = profile["marker"]
        for signal in signals:
            if marker in signal:
                match = re.search(re.escape(marker) + r"/(?P<date>\d{4}-\d{2}-\d{2})", signal)
                if match:
                    family_versions.add(match.group("date"))
        # Contradictory schema metadata must not be resolved by choosing the
        # first namespace or the first schemaRef encountered.
        if len(core_versions) > 1 or len(family_versions) > 1:
            return None
        if core_versions and next(iter(core_versions)) != version:
            return None
        if family_versions and next(iter(family_versions)) != version:
            return None

        core_namespace = next(
            (signal for signal in namespace_uris if _CORE_NAMESPACE_RE.match(signal) and
             _CORE_NAMESPACE_RE.match(signal).group("version") == version),
            f"http://www.sebi.gov.in/xbrl/{version}/in-capmkt",
        )
        return TaxonomyIdentity(
            taxonomy_id=profile["taxonomy_id"],
            label=profile["label"],
            version=version,
            namespace=core_namespace,
            standard_namespace_prefixes=("in-capmkt",),
            source_package_version=profile["source_package_version"],
            canonical_mapping_enabled=profile.get("canonical_mapping_enabled", False),
        )


DEFAULT_REGISTRY = TaxonomyRegistry()


def resolve_taxonomy_for_document(doc) -> Optional[TaxonomyIdentity]:
    """Return an identity only when document-level structural evidence is unambiguous."""
    return DEFAULT_REGISTRY.resolve_document(doc)
