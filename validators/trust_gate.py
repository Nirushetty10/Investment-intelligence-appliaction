"""Conservative filing-level gate for downstream trusted analytics and ML.

A filing can be ingested and normalized without being trusted. Raw facts are
always retained; this gate controls whether the normalized period may be
consumed through the trusted_financial_periods view.
"""
from __future__ import annotations

from taxonomy.catalog import taxonomy_version_status
from normalizers.taxonomy_classifier import (
    TAXONOMY_UNSUPPORTED,
    UNRECOGNIZED_NAMESPACE,
)

TRUSTED = "TRUSTED"
PROVISIONAL = "PROVISIONAL"
BLOCKED = "BLOCKED"



def build_taxonomy_provenance(normalization_result, issues, schema_refs=()) -> dict:
    """Build JSON-serializable taxonomy/trust evidence for persistence.

    This records what the pipeline actually knows. It never upgrades a
    missing/unknown taxonomy to a guessed identity and never borrows metadata
    from a different taxonomy version.
    """
    identity = getattr(normalization_result, "resolved_taxonomy", None)
    if identity is None:
        taxonomy_status = "TAXONOMY_UNRESOLVED"
        taxonomy_id = None
        taxonomy_version = None
        taxonomy_namespace = None
        source_package_version = None
        mapping_enabled = False
    else:
        taxonomy_id = getattr(identity, "taxonomy_id", None)
        taxonomy_version = getattr(identity, "version", None)
        taxonomy_namespace = getattr(identity, "namespace", None)
        source_package_version = getattr(identity, "source_package_version", None)
        mapping_enabled = bool(getattr(identity, "canonical_mapping_enabled", False))
        taxonomy_status = taxonomy_version_status(taxonomy_id, taxonomy_version)

    reasons = []
    seen = set()

    def add_reason(check_name, severity, message):
        key = (str(check_name), str(severity), str(message))
        if key in seen:
            return
        seen.add(key)
        reasons.append({"check_name": key[0], "severity": key[1], "message": key[2]})

    for issue in issues or []:
        severity = getattr(issue, "severity", "INFO")
        if severity in {"WARNING", "ERROR"}:
            add_reason(
                getattr(issue, "check_name", "validation_issue"),
                severity,
                getattr(issue, "message", str(issue)),
            )

    if identity is None:
        add_reason(
            "taxonomy_identity_unresolved",
            "WARNING",
            "No unambiguous standard taxonomy identity was resolved from the filing schemaRef and namespaces.",
        )
    else:
        if taxonomy_status != "EXACT_VERSION_AVAILABLE":
            add_reason(
                "taxonomy_catalog_exact_version_unavailable",
                "WARNING",
                f"No exact taxonomy catalog is available for {taxonomy_id}@{taxonomy_version}; catalog status is {taxonomy_status}.",
            )
        if not mapping_enabled:
            add_reason(
                "taxonomy_canonical_mapping_disabled",
                "WARNING",
                f"Canonical mappings are not enabled for taxonomy family {taxonomy_id!r}.",
            )

    classification = getattr(normalization_result, "classification", None)
    if classification is not None and getattr(classification, "unmapped_financial_count", 0) > 0:
        add_reason(
            "unmapped_financial_facts",
            "WARNING",
            f"{classification.unmapped_financial_count} financial fact(s) remain unmapped.",
        )

    return {
        "taxonomy_id": taxonomy_id,
        "taxonomy_version": taxonomy_version,
        "taxonomy_namespace": taxonomy_namespace,
        "taxonomy_source_package_version": source_package_version,
        "taxonomy_catalog_status": taxonomy_status,
        "canonical_mapping_enabled": mapping_enabled,
        "source_schema_refs": [str(ref) for ref in (schema_refs or ()) if ref],
        "trust_reasons": reasons,
    }

def evaluate_trust_status(normalization_result, issues) -> str:
    """Return TRUSTED, PROVISIONAL, or BLOCKED without changing source values.

    The policy is intentionally conservative:
    - ERROR validation issues block trust.
    - A missing/disabled taxonomy, missing exact version catalog, unresolved
      namespace, or unmapped financial fact keeps a period provisional.
    - Any WARNING keeps a period provisional.
    - Only a complete exact-version, enabled, warning-free normalization is
      TRUSTED. INFO-only findings do not block trust.
    """
    if any(getattr(issue, "severity", None) == "ERROR" for issue in issues):
        return BLOCKED

    identity = getattr(normalization_result, "resolved_taxonomy", None)
    if identity is None or not getattr(identity, "canonical_mapping_enabled", False):
        return PROVISIONAL

    if taxonomy_version_status(identity.taxonomy_id, identity.version) != "EXACT_VERSION_AVAILABLE":
        return PROVISIONAL

    classification = getattr(normalization_result, "classification", None)
    if classification is not None and getattr(classification, "unmapped_financial_count", 0) > 0:
        return PROVISIONAL

    taxonomy_classification = getattr(normalization_result, "taxonomy_classification", None)
    counts = getattr(taxonomy_classification, "counts", {}) or {}
    if counts.get(UNRECOGNIZED_NAMESPACE, 0) or counts.get(TAXONOMY_UNSUPPORTED, 0):
        return PROVISIONAL

    if any(getattr(issue, "severity", None) == "WARNING" for issue in issues):
        return PROVISIONAL

    return TRUSTED
