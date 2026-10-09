from types import SimpleNamespace

from validators.trust_gate import build_taxonomy_provenance


def _identity(version="2025-01-31", enabled=True):
    return SimpleNamespace(
        taxonomy_id="ind_as_other_than_banks",
        version=version,
        namespace=f"http://www.sebi.gov.in/xbrl/{version}/in-capmkt",
        source_package_version="2025-01-31",
        canonical_mapping_enabled=enabled,
    )


def _result(identity, unmapped=0):
    return SimpleNamespace(
        resolved_taxonomy=identity,
        classification=SimpleNamespace(unmapped_financial_count=unmapped),
    )


def _issue(name, severity, message):
    return SimpleNamespace(check_name=name, severity=severity, message=message)


def test_exact_taxonomy_provenance_records_identity_and_schema_ref():
    schema_ref = "in-capmkt-ent-2025-01-31.xsd"
    p = build_taxonomy_provenance(_result(_identity()), [], [schema_ref])
    assert p["taxonomy_id"] == "ind_as_other_than_banks"
    assert p["taxonomy_version"] == "2025-01-31"
    assert p["taxonomy_source_package_version"] == "2025-01-31"
    assert p["taxonomy_catalog_status"] == "EXACT_VERSION_AVAILABLE"
    assert p["canonical_mapping_enabled"] is True
    assert p["source_schema_refs"] == [schema_ref]
    assert p["trust_reasons"] == []


def test_version_mismatch_is_persisted_as_reason_not_hidden():
    p = build_taxonomy_provenance(
        _result(_identity("2026-01-31")), [], ["in-capmkt-ent-2026-01-31.xsd"]
    )
    assert p["taxonomy_version"] == "2026-01-31"
    assert p["taxonomy_source_package_version"] == "2025-01-31"
    assert p["taxonomy_catalog_status"] == "VERSION_UNAVAILABLE"
    assert any(r["check_name"] == "taxonomy_catalog_exact_version_unavailable" for r in p["trust_reasons"])


def test_unresolved_identity_is_recorded_as_unresolved_and_mapping_disabled():
    p = build_taxonomy_provenance(_result(None), [], ["unknown-entry.xsd"])
    assert p["taxonomy_id"] is None
    assert p["taxonomy_catalog_status"] == "TAXONOMY_UNRESOLVED"
    assert p["canonical_mapping_enabled"] is False
    assert any(r["check_name"] == "taxonomy_identity_unresolved" for r in p["trust_reasons"])


def test_warning_and_unmapped_count_are_saved_as_auditable_reasons():
    issues = [_issue("ratio_scale_plausibility", "WARNING", "ratio may be scaled incorrectly")]
    p = build_taxonomy_provenance(_result(_identity(), unmapped=2), issues, [])
    names = {r["check_name"] for r in p["trust_reasons"]}
    assert "ratio_scale_plausibility" in names
    assert "unmapped_financial_facts" in names


def test_info_only_issue_does_not_become_a_trust_blocking_reason():
    p = build_taxonomy_provenance(
        _result(_identity()), [_issue("informational_check", "INFO", "nothing wrong")], []
    )
    assert p["trust_reasons"] == []
