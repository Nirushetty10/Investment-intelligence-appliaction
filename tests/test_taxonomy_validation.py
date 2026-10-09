"""
tests/test_taxonomy_validation.py

Automated taxonomy validation (spec item 9). The supplied official taxonomy
packages provide sector/schema identity evidence, but the concept manifest
still contains only concepts observed in one real RELIANCE filing rather
than a complete, versioned concept registry for every sector. "Confirmed"
means "proven present in that real filing"; "unverified" means "not yet
checked against the complete applicable taxonomy version"; "known_invalid"
means "actively disproved" with an alternative identified.
"""
from datetime import date
from pathlib import Path

import pytest

from normalizers.concept_map import (
    BALANCE_SHEET_CONCEPT_MAP,
    BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS,
    CASHFLOW_CONCEPT_MAP,
    CASHFLOW_SUPPLEMENTARY_CONCEPTS,
    INCOME_STATEMENT_CONCEPT_MAP,
    INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
    RATIO_CONCEPTS,
    RECONCILIATION_BRIDGE_CONCEPTS,
)
from normalizers.financial_normalizer import normalize
from normalizers.taxonomy_classifier import (
    COMPANY_EXTENSION,
    DIMENSIONAL_SEGMENT,
    GENUINELY_UNMAPPED_FINANCIAL,
    STRUCTURAL_METADATA,
    TAXONOMY_MAPPED,
    UNRECOGNIZED_NAMESPACE,
    classify_fact_taxonomy,
    classify_facts_taxonomy,
)
from parsers.ixbrl_parser import ParsedXbrlDocument, XbrlContext, XbrlFact, XbrlUnit, parse_document
from resolvers.period_resolver import resolve_canonical_period
from taxonomy.manifests import KNOWN_INVALID_CONCEPTS
from taxonomy.registry import DEFAULT_REGISTRY, resolve_taxonomy_for_document

Q1_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"

ALL_CONCEPT_MAPS = (
    INCOME_STATEMENT_CONCEPT_MAP,
    BALANCE_SHEET_CONCEPT_MAP,
    BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS,
    CASHFLOW_CONCEPT_MAP,
    CASHFLOW_SUPPLEMENTARY_CONCEPTS,
    RECONCILIATION_BRIDGE_CONCEPTS,
    INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
)


def _every_mapped_concept():
    concepts = set()
    for m in ALL_CONCEPT_MAPS:
        for candidates in m.values():
            concepts.update(candidates)
    concepts.update(RATIO_CONCEPTS.keys())
    return concepts


# --------------------------------------------------------------------------
# THE hard-failing test (spec item 9): a concept_map.py alias must never be
# one we have actively disproved.
# --------------------------------------------------------------------------

def test_no_mapped_concept_is_known_invalid():
    """Fails if any concept_map.py alias (in ANY field, across every map)
    is in taxonomy/manifests.py's KNOWN_INVALID_CONCEPTS — i.e. a concept
    we specifically checked against real data and found to not exist. A
    regression here means someone re-added a disproved alias."""
    mapped = _every_mapped_concept()
    offenders = mapped & set(KNOWN_INVALID_CONCEPTS.keys())
    assert not offenders, (
        f"These concept_map.py aliases are KNOWN INVALID (previously disproved against "
        f"real data) and must not be re-added: "
        f"{ {c: KNOWN_INVALID_CONCEPTS[c] for c in offenders} }"
    )


def test_paid_up_equity_share_capital_specifically_stays_removed():
    """The exact regression this whole validation pass fixed — named
    explicitly so a future refactor can't silently lose this coverage
    inside the more generic test above."""
    mapped = _every_mapped_concept()
    assert "PaidUpEquityShareCapital" not in mapped
    assert "PaidUpEquityShareCapital" in KNOWN_INVALID_CONCEPTS


# --------------------------------------------------------------------------
# Confirmation-status visibility (informational — not pass/fail on
# "unverified", since that bucket legitimately contains untested-but-
# plausible concepts; this just ensures the mechanism works)
# --------------------------------------------------------------------------

def test_confirmation_status_reports_all_three_states():
    assert DEFAULT_REGISTRY.confirmation_status("ind_as_other_than_banks", "RevenueFromOperations") == "confirmed"
    assert DEFAULT_REGISTRY.confirmation_status("ind_as_other_than_banks", "PaidUpEquityShareCapital") == "known_invalid"
    assert DEFAULT_REGISTRY.confirmation_status("ind_as_other_than_banks", "SomeConceptNeverChecked") == "unverified"


def test_every_income_statement_concept_is_at_least_confirmed_or_unverified_never_invalid():
    """None of the primary (non-supplementary) income-statement aliases —
    the most heavily real-data-verified map — should be known_invalid."""
    for candidates in INCOME_STATEMENT_CONCEPT_MAP.values():
        for concept in candidates:
            status = DEFAULT_REGISTRY.confirmation_status("ind_as_other_than_banks", concept)
            assert status != "known_invalid", f"{concept} is known_invalid but still mapped"


# --------------------------------------------------------------------------
# Taxonomy resolution (spec item 2)
# --------------------------------------------------------------------------

def test_real_filing_resolves_to_ind_as_other_than_banks():
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    identity = resolve_taxonomy_for_document(doc)
    assert identity is not None
    assert identity.taxonomy_id == "ind_as_other_than_banks"
    assert identity.version == "2026-01-31"


def test_unrecognized_namespace_resolves_to_none_not_a_guess():
    """A document using a namespace this registry doesn't recognize (e.g.
    a hypothetical Banking/NBFC taxonomy — explicitly out of scope, spec
    item 12) must resolve to None, never be force-fit into Ind-AS-non-bank."""
    ctx = "D1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date(2026, 3, 31))},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[
            XbrlFact(
                context_ref=ctx, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-bank",
                concept="SomeBi", raw_tag="in-bank:SomeBi", raw_value="1",
                numeric_value=None, unit_ref=None, decimals=None, scale=None, sign=None,
            )
        ],
        source_format="XBRL_XML",
    )
    assert resolve_taxonomy_for_document(doc) is None


# --------------------------------------------------------------------------
# Company extension detection (spec item 4) — namespace-based, not
# RELIANCE-specific (spec item 8: must work for any filer's extension)
# --------------------------------------------------------------------------

def test_company_extension_detected_by_namespace_not_concept_name():
    """Only an extension namespace confirmed through schema evidence is an extension."""
    ctx = "D1"
    context = XbrlContext(context_ref=ctx, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30))
    fact = XbrlFact(
        context_ref=ctx, namespace="http://example.com/some-company-extension-2026",
        concept="RevenueFromOperations",  # same local name as a standard concept, different namespace
        raw_tag="xyzcorp:RevenueFromOperations", raw_value="123",
        numeric_value=123, unit_ref="INR", decimals=None, scale=None, sign=None,
    )
    result = classify_fact_taxonomy(
        fact,
        context,
        mapped_concepts={"RevenueFromOperations"},
        confirmed_extension_namespaces={fact.namespace},
    )
    assert result.category == COMPANY_EXTENSION


def test_unrecognized_namespace_is_not_automatically_company_extension():
    """An unfamiliar namespace is quarantined for investigation, not guessed to be an issuer extension."""
    ctx = "D1"
    context = XbrlContext(context_ref=ctx, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30))
    fact = XbrlFact(
        context_ref=ctx,
        namespace="http://example.com/unknown-taxonomy",
        concept="RevenueFromOperations",
        raw_tag="unknown:RevenueFromOperations",
        raw_value="999",
        numeric_value=999,
        unit_ref="INR",
        decimals=None,
        scale=None,
        sign=None,
    )
    result = classify_fact_taxonomy(fact, context, mapped_concepts={"RevenueFromOperations"})
    assert result.category == UNRECOGNIZED_NAMESPACE


def test_standard_namespace_concept_not_misclassified_as_extension():
    ctx = "D1"
    context = XbrlContext(context_ref=ctx, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30))
    fact = XbrlFact(
        context_ref=ctx, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt",
        concept="RevenueFromOperations", raw_tag="in-capmkt:RevenueFromOperations",
        raw_value="123", numeric_value=123, unit_ref="INR", decimals=None, scale=None, sign=None,
    )
    result = classify_fact_taxonomy(fact, context, mapped_concepts={"RevenueFromOperations"})
    assert result.category == TAXONOMY_MAPPED


# --------------------------------------------------------------------------
# Dimensional facts stay separate from company-wide facts (spec item 7)
# --------------------------------------------------------------------------

def test_dimensioned_context_always_classified_dimensional_segment():
    """Even a concept that WOULD otherwise resolve to a normalized field
    must classify as DIMENSIONAL_SEGMENT when tagged on a dimensioned
    context — dimensional facts are never silently treated as company-wide."""
    ctx = "D1_segment"
    context = XbrlContext(
        context_ref=ctx, period_start=date(2026, 4, 1), period_end=date(2026, 6, 30),
        dimensions={"in-capmkt:SegmentsAxis": "in-capmkt:OilToChemicalsMember"},
    )
    fact = XbrlFact(
        context_ref=ctx, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt",
        concept="RevenueFromOperations",  # same concept name mapped company-wide elsewhere
        raw_tag="in-capmkt:RevenueFromOperations", raw_value="999",
        numeric_value=999, unit_ref="INR", decimals=None, scale=None, sign=None,
    )
    result = classify_fact_taxonomy(fact, context, mapped_concepts={"RevenueFromOperations"})
    assert result.category == DIMENSIONAL_SEGMENT


def test_real_filing_dimensional_and_company_wide_facts_correctly_separated():
    """Cross-check against the real filing: the non-dimensioned
    RevenueFromOperations fact used for the normalized total must NOT be
    the same fact instance classified as dimensional, even though a
    dimensioned SegmentRevenue concept exists in the same document."""
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    filing = {
        "period_end_date": date(2026, 6, 30), "period_type": "quarterly",
        "period_start_date": None, "submission_type": "Unaudited",
        "statement_type": "consolidated", "financial_year": None,
    }
    resolved = resolve_canonical_period(doc, filing)
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)

    tally = result.taxonomy_classification
    assert tally.counts[DIMENSIONAL_SEGMENT] > 0
    # the company-wide revenue figure used for normalization is unaffected
    assert result.income_statement["revenue_from_operations"] is not None


# --------------------------------------------------------------------------
# Regression safety (spec item 10/11): existing passing behavior intact
# --------------------------------------------------------------------------

def test_filing7_classification_totals_reconcile_to_total_fact_count():
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    filing = {
        "period_end_date": date(2026, 6, 30), "period_type": "quarterly",
        "period_start_date": None, "submission_type": "Unaudited",
        "statement_type": "consolidated", "financial_year": None,
    }
    resolved = resolve_canonical_period(doc, filing)
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)
    assert sum(result.taxonomy_classification.counts.values()) == len(doc.facts) == 142
    assert result.resolved_taxonomy is not None
    assert result.resolved_taxonomy.taxonomy_id == "ind_as_other_than_banks"


def test_filing7_statement_type_and_period_still_correct():
    """Spec item 10: period resolution and standalone/consolidated
    resolution must remain intact after this change."""
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    filing = {
        "period_end_date": date(2026, 6, 30), "period_type": "quarterly",
        "period_start_date": None, "submission_type": "Unaudited",
        "statement_type": "consolidated", "financial_year": None,
    }
    resolved = resolve_canonical_period(doc, filing)
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)
    assert resolved.period_type == "quarterly"
    assert resolved.period_start == date(2026, 4, 1)
    assert result.period.statement_type == "consolidated"


def test_real_filing_parser_captures_schema_ref_and_sector_namespace():
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    assert "in-capmkt-ent-2026-01-31.xsd" in doc.schema_refs
    assert doc.namespace_map.get("in-capmkt-ent") == (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"
    )


def test_shared_core_namespace_is_not_enough_to_guess_taxonomy_family():
    core_ns = "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"
    ctx = "D1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date(2025, 3, 31))},
        units={},
        facts=[XbrlFact(
            context_ref=ctx, namespace=core_ns, concept="Assets", raw_tag="in-capmkt:Assets",
            raw_value="1", numeric_value=1, unit_ref=None, decimals=None, scale=None, sign=None,
        )],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2025-01-31.xsd",),
        namespace_map={"in-capmkt": core_ns},
    )
    assert resolve_taxonomy_for_document(doc) is None


def test_schema_namespace_resolves_banking_not_ind_as_when_core_namespace_is_shared():
    core_ns = "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"
    banking_ext = (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_Banking/2025-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    ctx = "D1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date(2025, 3, 31))},
        units={},
        facts=[XbrlFact(
            context_ref=ctx, namespace=core_ns, concept="Assets", raw_tag="in-capmkt:Assets",
            raw_value="1", numeric_value=1, unit_ref=None, decimals=None, scale=None, sign=None,
        )],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2025-01-31.xsd",),
        namespace_map={"in-capmkt": core_ns, "in-capmkt-ent": banking_ext},
    )
    identity = resolve_taxonomy_for_document(doc)
    assert identity is not None
    assert identity.taxonomy_id == "banking"
    assert identity.version == "2025-01-31"
    assert identity.namespace == core_ns


def test_conflicting_sector_namespace_evidence_fails_closed():
    core_ns = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
    indas_ext = (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    banking_ext = (
        "http://www.sebi.gov.in/xbrl/IntegratedFinance_Banking/2026-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    ctx = "D1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date(2026, 3, 31))},
        units={},
        facts=[XbrlFact(
            context_ref=ctx, namespace=core_ns, concept="Assets", raw_tag="in-capmkt:Assets",
            raw_value="1", numeric_value=1, unit_ref=None, decimals=None, scale=None, sign=None,
        )],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",),
        namespace_map={
            "in-capmkt": core_ns, "indas": indas_ext, "banking": banking_ext,
        },
    )
    assert resolve_taxonomy_for_document(doc) is None


@pytest.mark.parametrize(
    "taxonomy_id,marker,version,source_package_version,canonical_enabled",
    [
        ("ind_as_other_than_banks", "IntegratedFinance_IndAS", "2025-01-31", "2025-01-31", True),
        ("other_than_banks", "IntegratedFinance_OtherThanBank", "2025-01-31", "2025-01-31", False),
        ("banking", "IntegratedFinance_Banking", "2025-01-31", "2025-01-31", False),
        ("nbfc", "IntegratedFinance_NBFC", "2025-01-31", "2025-01-31", False),
        ("general_insurance", "IntegratedFinance_GI", "2025-01-31", "2025-01-31", False),
        ("life_insurance", "IntegratedFinance_LI", "2025-01-31", "2025-01-31", False),
        ("reits_invit", "Financial_Results_REITs_InvITs", "2026-01-31", "2026-01-31", False),
    ],
)
def test_supplied_sector_profiles_resolve_without_enabling_unreviewed_mappings(
    taxonomy_id, marker, version, source_package_version, canonical_enabled
):
    core_ns = f"http://www.sebi.gov.in/xbrl/{version}/in-capmkt"
    family_ns = (
        f"http://www.sebi.gov.in/xbrl/{marker}/{version}/in-capmkt/in-capmkt-ent"
    )
    ctx = "D1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date.fromisoformat(version))},
        units={},
        facts=[XbrlFact(
            context_ref=ctx, namespace=core_ns, concept="Assets", raw_tag="in-capmkt:Assets",
            raw_value="1", numeric_value=1, unit_ref=None, decimals=None, scale=None, sign=None,
        )],
        source_format="XBRL_XML",
        schema_refs=(f"in-capmkt-ent-{version}.xsd",),
        namespace_map={"in-capmkt": core_ns, "in-capmkt-ent": family_ns},
    )
    identity = resolve_taxonomy_for_document(doc)
    assert identity is not None
    assert identity.taxonomy_id == taxonomy_id
    assert identity.version == version
    assert identity.source_package_version == source_package_version
    assert identity.canonical_mapping_enabled is canonical_enabled


def test_non_sebi_namespace_cannot_spoof_a_registered_taxonomy_family():
    ctx = "D1"
    fake_family = (
        "https://issuer.example/xbrl/IntegratedFinance_IndAS/2026-01-31/"
        "in-capmkt/in-capmkt-ent"
    )
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=date(2026, 3, 31))},
        units={},
        facts=[],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",),
        namespace_map={"in-capmkt-ent": fake_family},
    )
    assert resolve_taxonomy_for_document(doc) is None
