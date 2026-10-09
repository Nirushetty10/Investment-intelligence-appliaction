from types import SimpleNamespace

from taxonomy.registry import TaxonomyIdentity
from validators.financial_validator import ValidationIssue
from validators.trust_gate import BLOCKED, PROVISIONAL, TRUSTED, evaluate_trust_status


def _result(version="2025-01-31", unmapped=0, taxonomy_counts=None, enabled=True):
    return SimpleNamespace(
        resolved_taxonomy=TaxonomyIdentity(
            taxonomy_id="ind_as_other_than_banks",
            label="test taxonomy",
            version=version,
            namespace="http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt",
            standard_namespace_prefixes=("in-capmkt",),
            source_package_version="2025-01-31",
            canonical_mapping_enabled=enabled,
        ),
        classification=SimpleNamespace(unmapped_financial_count=unmapped),
        taxonomy_classification=SimpleNamespace(counts=taxonomy_counts or {}),
    )


def test_exact_version_with_no_unresolved_facts_or_warnings_is_trusted():
    assert evaluate_trust_status(_result(), []) == TRUSTED


def test_validation_warning_keeps_period_provisional():
    issues = [ValidationIssue("source_warning", "WARNING", "review required")]
    assert evaluate_trust_status(_result(), issues) == PROVISIONAL


def test_validation_error_blocks_period_from_trusted_view():
    issues = [ValidationIssue("reconciliation", "ERROR", "material mismatch")]
    assert evaluate_trust_status(_result(), issues) == BLOCKED


def test_unmapped_financial_facts_keep_period_provisional():
    assert evaluate_trust_status(_result(unmapped=1), []) == PROVISIONAL


def test_missing_exact_catalog_or_disabled_mappings_cannot_be_trusted():
    assert evaluate_trust_status(_result(version="2026-01-31"), []) == PROVISIONAL
    assert evaluate_trust_status(_result(enabled=False), []) == PROVISIONAL


def test_unrecognized_namespace_cannot_be_trusted():
    assert evaluate_trust_status(
        _result(taxonomy_counts={"UNRECOGNIZED_NAMESPACE": 1}), []
    ) == PROVISIONAL
