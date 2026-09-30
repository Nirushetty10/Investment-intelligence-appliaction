"""
tests/test_fact_classification.py

Covers the fix for the misleading flat `unmapped_fact_count`: every fact
must classify as exactly one of MAPPED / INTENTIONALLY_UNMAPPED /
UNMAPPED_FINANCIAL, the three counts must always sum to the total fact
count, raw facts must remain completely unchanged by classification, and
per-statement coverage (AVAILABLE / NOT_REPORTED_IN_FILING) must reflect
what the SOURCE FILING actually contains — never a normalization bug
dressed up as "not reported", and never fabricated BS/CF data because one
ancillary field happened to resolve.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from normalizers.fact_classifier import (
    INTENTIONALLY_UNMAPPED,
    MAPPED,
    UNMAPPED_FINANCIAL,
    classify_concept,
    classify_facts,
)
from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import parse_document

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"
PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2026, 6, 30)


@pytest.fixture(scope="module")
def parsed_doc():
    return parse_document(FIXTURE_PATH.read_bytes())


@pytest.fixture(scope="module")
def normalized(parsed_doc):
    return normalize(parsed_doc, period_end=PERIOD_END, period_start=PERIOD_START)


# --------------------------------------------------------------------------
# Classification correctness
# --------------------------------------------------------------------------

def test_three_buckets_sum_to_total_fact_count(normalized, parsed_doc):
    c = normalized.classification
    assert c.mapped_count + c.intentionally_unmapped_count + c.unmapped_financial_count == len(parsed_doc.facts)
    assert c.total == 142


def test_backward_compatible_unmapped_fact_count_alias(normalized):
    """Old flat counter must still exist and equal intentional + financial."""
    c = normalized.classification
    assert normalized.unmapped_fact_count == c.intentionally_unmapped_count + c.unmapped_financial_count


def test_important_financial_concepts_are_mapped(normalized):
    """Revenue, Expenses, PBT, Tax, PAT, EPS must resolve — the exact
    concepts the issue calls out by name."""
    stmt = normalized.income_statement
    for field in (
        "revenue_from_operations", "total_expenses", "profit_before_tax",
        "total_tax", "profit_for_period", "basic_eps", "diluted_eps",
    ):
        assert stmt.get(field) is not None, f"{field} should be mapped and populated"


def test_metadata_facts_classified_intentionally_unmapped_not_financial():
    mapped_concepts = set()  # deliberately empty — testing pure classification, not resolution
    for concept in ("NameOfTheCompany", "ISIN", "ScripCode", "AuditorsFirmName",
                     "DateOfEndOfBoardMeeting", "DisclosureOfNotesOnSegmentsExplanatoryTextBlock"):
        result = classify_concept(concept, mapped_concepts)
        assert result.classification == INTENTIONALLY_UNMAPPED, f"{concept} should be INTENTIONALLY_UNMAPPED"
        assert result.category is not None


def test_segment_facts_classified_intentionally_unmapped_segment_dimensional():
    mapped_concepts = set()
    for concept in ("SegmentRevenue", "SegmentAssets", "SegmentLiabilities", "SegmentProfitBeforeTax"):
        result = classify_concept(concept, mapped_concepts)
        assert result.classification == INTENTIONALLY_UNMAPPED
        assert result.category == "segment_dimensional"


def test_unrecognized_concept_defaults_to_unmapped_financial_not_hidden():
    """Fail-safe: a concept the classifier has never seen before must
    surface for review (UNMAPPED_FINANCIAL), never silently disappear as
    'intentionally ignored'."""
    result = classify_concept("SomeBrandNewConceptNeverSeenBefore", mapped_concepts=set())
    assert result.classification == UNMAPPED_FINANCIAL
    assert result.category == "unrecognized_concept_needs_review"


def test_known_financial_but_unmapped_concepts_have_specific_reasons(normalized):
    c = normalized.classification
    assert "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement" in c.unmapped_financial_concepts
    assert "AmountOfItemThatWillBeReclassifiedToProfitAndLoss" in c.unmapped_financial_concepts
    # and NONE of the segment/metadata concepts leaked into this bucket
    assert "SegmentRevenue" not in c.unmapped_financial_concepts
    assert "NameOfTheCompany" not in c.unmapped_financial_concepts


def test_unmapped_financial_count_is_small_after_fix(normalized):
    """The whole point of this fix: genuinely-unmapped financial concepts
    should be a small, reviewable number, not conflated with the ~88
    metadata/segment/admin facts that are correctly ignored."""
    c = normalized.classification
    assert c.unmapped_financial_count == 7
    assert c.intentionally_unmapped_count == 88
    assert c.mapped_count == 47


# --------------------------------------------------------------------------
# Newly-fixed mappings (found while building the classifier)
# --------------------------------------------------------------------------

def test_equity_share_capital_now_resolves_via_correct_real_concept(normalized):
    """Was silently None before — the map used a concept name
    (PaidUpEquityShareCapital) that doesn't exist in the real taxonomy;
    the real one is PaidUpValueOfEquityShareCapital."""
    assert normalized.balance_sheet["equity_share_capital"] == Decimal("135330000000")


def test_supplementary_concepts_resolved(normalized):
    stmt = normalized.income_statement
    assert stmt["exceptional_items_before_tax"] == Decimal("0")  # explicit reported zero, not invented
    assert stmt["profit_attributable_non_controlling_interests"] is not None
    assert stmt["profit_attributable_owners_of_parent"] is not None
    # owners' share + NCI share must reconcile to profit_for_period
    assert (
        stmt["profit_attributable_owners_of_parent"] + stmt["profit_attributable_non_controlling_interests"]
        == stmt["profit_for_period"]
    )


# --------------------------------------------------------------------------
# Coverage status: source-data-availability vs normalization bugs
# --------------------------------------------------------------------------

def test_income_statement_coverage_available(normalized):
    assert normalized.income_statement_coverage == "AVAILABLE"


def test_balance_sheet_not_reported_despite_one_ancillary_field_present(normalized):
    """CORE requirement from the issue: the filing DOES report one
    balance-sheet-ish field (equity_share_capital, disclosed alongside
    every quarterly result for EPS purposes) but NOT total assets/
    liabilities — this must NOT be classified as balance sheet AVAILABLE."""
    assert normalized.balance_sheet["equity_share_capital"] is not None  # the one field that IS there
    assert normalized.balance_sheet["total_assets"] is None               # the anchor field is NOT
    assert normalized.balance_sheet_coverage == "NOT_REPORTED_IN_FILING"


def test_cashflow_not_reported(normalized):
    assert all(v is None for v in normalized.cashflow_statement.values())
    assert normalized.cashflow_statement_coverage == "NOT_REPORTED_IN_FILING"


def test_segment_data_coverage_available(normalized):
    assert normalized.segment_data_coverage == "AVAILABLE"


def test_eps_coverage_available(normalized):
    assert normalized.eps_coverage == "AVAILABLE"


def test_ratios_coverage_available(normalized):
    assert normalized.ratios_coverage == "AVAILABLE"


# --------------------------------------------------------------------------
# Duplicate concepts with different contexts (OtherExpenses case from the issue)
# --------------------------------------------------------------------------

def test_other_expenses_duplicate_contexts_resolve_to_whole_company_total(parsed_doc, normalized):
    """OtherExpenses is tagged 3 times: once non-dimensioned (the true
    company-wide total) and twice under a DetailsOfOtherExpensesAxis
    dimension (a two-part breakdown that sums to the total). The
    normalizer must pick the non-dimensioned total, never a breakdown
    component, and must never silently pick 'the first' or 'the last'
    match without checking dimensions."""
    raw_facts = parsed_doc.facts_for_concept("OtherExpenses")
    assert len(raw_facts) == 3  # all three raw facts preserved, nothing discarded

    dimensioned_values = sorted(
        f.numeric_value for f in raw_facts
        if parsed_doc.contexts[f.context_ref].has_dimensions
    )
    non_dimensioned_values = [
        f.numeric_value for f in raw_facts
        if not parsed_doc.contexts[f.context_ref].has_dimensions
    ]
    assert len(non_dimensioned_values) == 1
    total = non_dimensioned_values[0]
    assert sum(dimensioned_values) == total  # breakdown sums to the total (sanity on the fixture itself)

    # the normalizer's resolved value must be the non-dimensioned TOTAL,
    # not either breakdown component
    assert normalized.income_statement["other_expenses"] == total
    assert normalized.income_statement["other_expenses"] not in dimensioned_values


# --------------------------------------------------------------------------
# Raw facts remain completely unchanged by classification
# --------------------------------------------------------------------------

def test_raw_facts_unchanged_by_classification(parsed_doc):
    """Classifying facts must be a pure read — it must never mutate,
    filter, or discard anything in the parsed document."""
    assert len(parsed_doc.facts) == 142
    assert len(parsed_doc.contexts) == 38
    assert len(parsed_doc.units) == 3


# --------------------------------------------------------------------------
# Idempotent reprocessing: normalize() is a pure function of (doc, period)
# --------------------------------------------------------------------------

def test_normalize_is_idempotent(parsed_doc):
    r1 = normalize(parsed_doc, period_end=PERIOD_END, period_start=PERIOD_START)
    r2 = normalize(parsed_doc, period_end=PERIOD_END, period_start=PERIOD_START)
    assert r1.income_statement == r2.income_statement
    assert r1.balance_sheet == r2.balance_sheet
    assert r1.classification.mapped_count == r2.classification.mapped_count
    assert r1.classification.unmapped_financial_concepts == r2.classification.unmapped_financial_concepts
