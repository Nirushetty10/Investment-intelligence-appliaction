"""Regression tests for the imported versioned SEBI taxonomy catalog."""
from datetime import date

from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import ParsedXbrlDocument, XbrlContext, XbrlFact, XbrlUnit
from taxonomy.catalog import (
    concept_status,
    get_concept_metadata,
    get_taxonomy_entry,
    list_taxonomy_versions,
    taxonomy_version_status,
)
from taxonomy.period_types import get_concept_period_type
from taxonomy.registry import DEFAULT_REGISTRY


def test_catalog_contains_all_supplied_sebi_taxonomy_families_and_versions():
    expected = {
        "ind_as_other_than_banks": ["2025-01-31"],
        "other_than_banks": ["2025-01-31"],
        "banking": ["2025-01-31"],
        "nbfc": ["2025-01-31"],
        "general_insurance": ["2025-01-31"],
        "life_insurance": ["2025-01-31"],
        "reits_invit": ["2026-01-31"],
    }
    for taxonomy_id, versions in expected.items():
        assert list_taxonomy_versions(taxonomy_id) == versions
        for version in versions:
            entry = get_taxonomy_entry(taxonomy_id, version)
            assert entry is not None
            assert entry["concept_count"] == len(entry["concepts"])
            assert entry["source_package_sha256"] and len(entry["source_package_sha256"]) == 64
            assert entry["relationship_count"] > 0


def test_authoritative_concept_metadata_contains_period_type_type_and_label():
    revenue = get_concept_metadata("ind_as_other_than_banks", "2025-01-31", "RevenueFromOperations")
    assets = get_concept_metadata("ind_as_other_than_banks", "2025-01-31", "Assets")
    text_block = get_concept_metadata(
        "ind_as_other_than_banks",
        "2025-01-31",
        "DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock",
    )

    assert revenue["period_type"] == "duration"
    assert revenue["value_kind"] == "monetary"
    assert revenue["is_numeric"] is True
    assert any(label["text"].casefold() == "revenue from operations" for label in revenue["labels"])

    assert assets["period_type"] == "instant"
    assert assets["value_kind"] == "monetary"
    assert text_block["value_kind"] == "text_block"
    assert text_block["is_numeric"] is False


def test_exact_taxonomy_version_status_does_not_treat_2025_catalog_as_2026():
    assert taxonomy_version_status("ind_as_other_than_banks", "2025-01-31") == "EXACT_VERSION_AVAILABLE"
    assert taxonomy_version_status("ind_as_other_than_banks", "2026-01-31") == "VERSION_UNAVAILABLE"
    assert concept_status("ind_as_other_than_banks", "2026-01-31", "RevenueFromOperations") == "TAXONOMY_VERSION_UNAVAILABLE"
    assert DEFAULT_REGISTRY.confirmation_status(
        "ind_as_other_than_banks", "RevenueFromOperations", "2026-01-31"
    ) == "taxonomy_version_unavailable"


def test_exact_version_concept_absence_does_not_fall_back_to_another_catalog():
    # The exact 2025 catalog is available. A typo or historical alias missing
    # from this version must remain unknown even if the legacy period catalog
    # happens to contain a similarly named entry.
    assert get_concept_period_type(
        "ConceptThatDoesNotExistInAnyTaxonomy",
        taxonomy_id="ind_as_other_than_banks",
        version="2025-01-31",
    ) is None
    assert DEFAULT_REGISTRY.confirmation_status(
        "ind_as_other_than_banks", "ConceptThatDoesNotExistInAnyTaxonomy", "2025-01-31"
    ) == "not_in_taxonomy"


def test_alias_missing_from_exact_taxonomy_is_not_normalized_to_canonical_field():
    core_ns = "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"
    entry_ns = (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2025-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    context_id = "DURATION_Q1"
    fact = XbrlFact(
        context_ref=context_id,
        namespace=core_ns,
        concept="TotalIncome",  # alias exists in older mapping code, not the 2025 XSD
        raw_tag="in-capmkt:TotalIncome",
        raw_value="100",
        numeric_value=100,
        unit_ref="INR",
        decimals="0",
        scale=None,
        sign=None,
    )
    doc = ParsedXbrlDocument(
        contexts={context_id: XbrlContext(
            context_ref=context_id,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        )},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[fact],
        source_format="XBRL_XML",
        schema_refs=(
            "http://www.sebi.gov.in/xbrl/2025-01-31/IntegratedFinance_IndAS/"
            "in-capmkt-ent-2025-01-31.xsd",
        ),
        namespace_map={"in-capmkt": core_ns, "in-capmkt-ent": entry_ns},
    )

    result = normalize(doc, period_end=date(2026, 6, 30), period_start=date(2026, 4, 1))

    assert result.resolved_taxonomy is not None
    assert result.resolved_taxonomy.taxonomy_id == "ind_as_other_than_banks"
    assert result.resolved_taxonomy.version == "2025-01-31"
    assert result.income_statement["total_income"] is None
    assert doc.facts[0].raw_value == "100"  # raw source preserved


def test_current_2026_filing_remains_provisional_without_exact_ind_as_catalog():
    # This prevents accidental promotion of the 2025 fallback metadata as if it
    # were proof of the 2026 taxonomy release.
    from pathlib import Path
    from parsers.ixbrl_parser import parse_document

    fixture = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"
    doc = parse_document(fixture.read_bytes())
    result = normalize(doc, period_end=date(2026, 6, 30), period_start=date(2026, 4, 1))
    assert result.resolved_taxonomy is not None
    assert result.resolved_taxonomy.version == "2026-01-31"
    assert any(
        item["check_name"] == "taxonomy_catalog_exact_version_unavailable"
        for item in result.guard_findings
    )
