"""Regression tests for fail-closed XBRL normalization guards.

These exercise the real ParsedXbrlDocument -> normalize() path rather than
calling guard helpers directly. Raw fact records must remain untouched when a
fact is not eligible for canonical normalization.
"""
from datetime import date
from decimal import Decimal

from normalizers.financial_normalizer import normalize
from normalizers.taxonomy_classifier import TAXONOMY_UNSUPPORTED, UNRECOGNIZED_NAMESPACE
from parsers.ixbrl_parser import ParsedXbrlDocument, XbrlContext, XbrlFact, XbrlUnit
from taxonomy.period_types import get_concept_period_type, get_period_type_catalog_version
from validators.financial_validator import ValidationIssue, overall_status


STANDARD_NS = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
EXTENSION_NS = "https://issuer.example/xbrl/extension/2026"
PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2026, 6, 30)


def _fact(context_ref, namespace, concept, value, unit_ref="INR"):
    return XbrlFact(
        context_ref=context_ref,
        namespace=namespace,
        concept=concept,
        raw_tag=f"{{{namespace}}}{concept}",
        raw_value=str(value),
        numeric_value=Decimal(str(value)),
        unit_ref=unit_ref,
        decimals="-5",
        scale=None,
        sign=None,
    )


def _document(facts, contexts=None, confirmed_extension_namespaces=None):
    has_standard_fact = any(f.namespace == STANDARD_NS for f in facts)
    return ParsedXbrlDocument(
        contexts=contexts or {
            "Q1": XbrlContext(
                context_ref="Q1",
                period_start=PERIOD_START,
                period_end=PERIOD_END,
            )
        },
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=facts,
        source_format="XBRL_XML",
        confirmed_extension_namespaces=confirmed_extension_namespaces or set(),
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",) if has_standard_fact else (),
        namespace_map=(
            {
                "in-capmkt": STANDARD_NS,
                "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent",
            }
            if has_standard_fact else {}
        ),
    )


def test_standard_revenue_with_registered_namespace_and_duration_context_is_normalized():
    fact = _fact("Q1", STANDARD_NS, "RevenueFromOperations", "1000000")
    doc = _document([fact])

    result = normalize(doc, period_end=PERIOD_END, period_start=PERIOD_START)

    assert result.income_statement["revenue_from_operations"] == Decimal("1000000")
    assert result.resolved_taxonomy is not None
    assert not any(f["check_name"] == "normalization_period_type_guard" for f in result.guard_findings)


def test_instant_revenue_fact_cannot_populate_quarterly_revenue_and_is_preserved():
    instant_context = XbrlContext(context_ref="INSTANT", instant_date=PERIOD_END)
    fact = _fact("INSTANT", STANDARD_NS, "RevenueFromOperations", "999")
    doc = _document([fact], contexts={"INSTANT": instant_context})

    result = normalize(doc, period_end=PERIOD_END, period_start=PERIOD_START)

    assert result.income_statement["revenue_from_operations"] is None
    assert any(f["check_name"] == "normalization_period_type_guard" for f in result.guard_findings)
    assert fact in doc.facts
    assert fact.raw_value == "999"
    assert fact.numeric_value == Decimal("999")


def test_extension_namespace_cannot_spoof_canonical_revenue_by_local_name():
    standard_fact = _fact("Q1", STANDARD_NS, "RevenueFromOperations", "100")
    extension_fact = _fact("Q1", EXTENSION_NS, "RevenueFromOperations", "999")
    doc = _document([standard_fact, extension_fact])

    result = normalize(doc, period_end=PERIOD_END, period_start=PERIOD_START)

    assert result.income_statement["revenue_from_operations"] == Decimal("100")
    assert result.income_statement["revenue_from_operations"] != extension_fact.numeric_value
    assert any(f["check_name"] == "normalization_namespace_guard" for f in result.guard_findings)
    assert extension_fact in doc.facts
    assert extension_fact.raw_value == "999"
    assert extension_fact.numeric_value == Decimal("999")


def test_unrecognized_namespace_only_document_fails_closed_without_losing_raw_fact():
    fact = _fact("Q1", EXTENSION_NS, "RevenueFromOperations", "999")
    doc = _document([fact])

    result = normalize(doc, period_end=PERIOD_END, period_start=PERIOD_START)

    assert result.resolved_taxonomy is None
    assert result.income_statement["revenue_from_operations"] is None
    assert result.taxonomy_classification.counts[UNRECOGNIZED_NAMESPACE] == 1
    assert any(f["check_name"] == "taxonomy_resolution" for f in result.guard_findings)
    assert fact in doc.facts and fact.raw_value == "999"


def test_validation_status_is_partial_when_warning_exists():
    assert overall_status([]) == "VALIDATED"
    assert overall_status([ValidationIssue("mapping_gap", "WARNING", "unresolved facts")]) == "PARTIAL"
    assert overall_status([ValidationIssue("arithmetic", "ERROR", "failed reconciliation")]) == "FAILED"


def test_period_type_comes_from_taxonomy_concept_not_target_statement_type():
    # The supplied Ind AS 2025 core XSD defines PaidUpValueOfEquityShareCapital
    # as duration even though consumers may project it to an equity/balance-sheet
    # output. Assets is an instant concept; revenue is duration-based.
    assert get_period_type_catalog_version() == "2025-01-31"
    assert get_concept_period_type("RevenueFromOperations") == "duration"
    assert get_concept_period_type("PaidUpValueOfEquityShareCapital") == "duration"
    assert get_concept_period_type("Assets") == "instant"


def test_recognized_banking_taxonomy_does_not_use_ind_as_canonical_mapping():
    core_ns = "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"
    banking_ns = (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_Banking/2025-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    context = XbrlContext(
        context_ref="Q1",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    fact = _fact("Q1", core_ns, "RevenueFromOperations", "999")
    doc = ParsedXbrlDocument(
        contexts={"Q1": context},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[fact],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2025-01-31.xsd",),
        namespace_map={"in-capmkt": core_ns, "in-capmkt-ent": banking_ns},
    )

    result = normalize(doc, period_end=PERIOD_END, period_start=PERIOD_START)

    assert result.resolved_taxonomy is not None
    assert result.resolved_taxonomy.taxonomy_id == "banking"
    assert result.resolved_taxonomy.canonical_mapping_enabled is False
    assert result.income_statement["revenue_from_operations"] is None
    assert any(f["check_name"] == "taxonomy_mapping_disabled" for f in result.guard_findings)
    assert result.taxonomy_classification.counts[TAXONOMY_UNSUPPORTED] == 1
    assert fact in doc.facts and fact.raw_value == "999"
