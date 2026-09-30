"""
tests/test_real_nse_xml.py

ISSUE 2: dedicated regression test for the REAL, machine-readable NSE XBRL
XML — as opposed to tests/test_ixbrl_parser.py, which tests the generic
IXBRL_HTML parser path against a reconstructed fixture. This file is
intentionally separate and does not modify or depend on the 12 existing
HTML tests.

Source of the fixture:
    RELIANCE INDUSTRIES LIMITED, Integrated Filing, quarter ended
    30-Jun-2026 (Q1 FY2026-27), Consolidated, Unaudited.
    https://nsearchives.nseindia.com/corporate/xbrl/
        INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml

This is the ACTUAL raw XML byte content as downloaded from NSE (not a
reconstruction) — unlike the HTML fixture, this one is a genuine
byte-for-byte regression fixture.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import parse_document
from validators.financial_validator import (
    overall_status,
    validate_income_statement,
    validate_ratio_plausibility,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2026, 6, 30)


@pytest.fixture(scope="module")
def parsed_doc():
    raw_bytes = FIXTURE_PATH.read_bytes()
    return parse_document(raw_bytes)


@pytest.fixture(scope="module")
def normalized(parsed_doc):
    return normalize(parsed_doc, period_end=PERIOD_END, period_start=PERIOD_START)


# --------------------------------------------------------------------------
# ISSUE 2: baseline structural counts
# --------------------------------------------------------------------------

def test_source_format_is_plain_xbrl_xml_not_ixbrl_html(parsed_doc):
    """The live 'iXBRL' HTML NSE serves for this filing contains zero
    ix:header / ix:nonFraction / ix:nonNumeric / xbrli:context elements —
    it is a plain presentation page, not inline XBRL. The machine-readable
    XML is a separate, distinct artifact. This must resolve to XBRL_XML,
    never IXBRL_HTML, and the parser must never be pointed at the
    presentation HTML for financial extraction."""
    assert parsed_doc.source_format == "XBRL_XML"


def test_baseline_context_fact_unit_counts(parsed_doc):
    assert len(parsed_doc.contexts) == 38
    assert len(parsed_doc.facts) == 142
    assert len(parsed_doc.units) == 3


# --------------------------------------------------------------------------
# ISSUE 3: statement type resolved from the REAL concept name
# --------------------------------------------------------------------------

def test_statement_type_resolves_to_consolidated(normalized):
    """The real taxonomy uses NatureOfReportStandaloneConsolidated (no
    'Or'), distinct from the reconstructed HTML fixture's
    NatureOfReportStandaloneOrConsolidated. Both must resolve correctly —
    see normalizers/concept_map.py METADATA_CONCEPTS."""
    assert normalized.period.statement_type == "consolidated"


def test_reporting_quarter_and_audited_status_from_real_filing(normalized):
    assert normalized.period.reporting_quarter == "First quarter"
    assert normalized.period.audited_status == "Unaudited"


def test_unit_is_inr_no_scale_assumption(normalized):
    # This real filing's facts declare unitRef="INR" with NO scale
    # attribute at all — the raw fact values are already absolute rupees.
    # (Contrast with the synthetic HTML fixture, which demonstrates scale
    # handling explicitly.) Confirms the parser doesn't invent a scale
    # factor when the source doesn't declare one.
    assert normalized.period.unit == "INR"


# --------------------------------------------------------------------------
# Income statement values, straight from the real filing
# --------------------------------------------------------------------------

def test_income_statement_matches_real_filing(normalized):
    stmt = normalized.income_statement
    assert stmt["revenue_from_operations"] == Decimal("3118500000000")
    assert stmt["profit_before_tax"] == Decimal("306300000000")
    assert stmt["total_tax"] == Decimal("76290000000")
    assert stmt["profit_for_period"] == Decimal("231960000000")


def test_continuing_operations_and_bridge_items_resolved(normalized):
    """ISSUE 5: the real filing separately tags profit from continuing
    operations, the associates/JV share, AND explicit (zero) discontinued
    operations — all three must resolve to their real reported values,
    not be silently merged or dropped."""
    stmt = normalized.income_statement
    assert stmt["profit_after_tax"] == Decimal("230010000000")  # continuing ops
    assert stmt["share_of_profit_associates_jv"] == Decimal("1950000000")
    # Explicitly reported as 0 by the filer — a real disclosed zero, not an
    # invented default (see tests/fixtures raw XML: the concept IS present
    # with value "0", unlike a genuinely absent concept which must be None).
    assert stmt["profit_discontinued_operations_after_tax"] == Decimal("0")


def test_profit_bridge_reconciles_exactly(normalized):
    """306.30B - 76.29B = 230.01B (continuing ops PAT); + 1.95B associates/JV
    + 0 discontinued = 231.96B, matching the reported profit for period
    exactly (spec ISSUE 5 worked example)."""
    stmt = normalized.income_statement
    continuing = stmt["profit_before_tax"] - stmt["total_tax"]
    assert continuing == stmt["profit_after_tax"] == Decimal("230010000000")

    total = stmt["profit_after_tax"] + stmt["share_of_profit_associates_jv"] + stmt["profit_discontinued_operations_after_tax"]
    assert total == stmt["profit_for_period"] == Decimal("231960000000")


def test_profit_reconciliation_validator_reports_no_errors(normalized):
    """With the bridge items present, both the continuing-ops check and the
    total-profit-with-bridge check must reconcile exactly and produce no
    ERROR/WARNING — a strong signal the improved reconciliation logic
    (ISSUE 5) is actually using the bridge items, not just getting lucky."""
    issues = validate_income_statement(normalized.income_statement)
    non_info = [i for i in issues if i.severity != "INFO"]
    assert not non_info, f"Unexpected reconciliation issues: {non_info}"
    assert overall_status(issues) == "VALIDATED"


# --------------------------------------------------------------------------
# ISSUE 6: ratios preserved exactly as declared, discrepancy flagged not fixed
# --------------------------------------------------------------------------

def test_ratios_preserved_raw_not_rescaled(normalized):
    """The real XBRL declares these three ratios at face value with no
    `scale` attribute — 0.004 / 0.0312 / 0.0467 — even though NSE's own
    presentation HTML shows values ~100x larger for the same filing. The
    normalizer must preserve the raw XBRL value exactly; ONLY the
    validator may flag the discrepancy, and only as an informational,
    auditable note — never a silent transformation."""
    ratios_by_name = {r["ratio_name"]: r for r in normalized.ratios}

    debt_equity = ratios_by_name["Debt Equity Ratio"]
    assert debt_equity["value"] == Decimal("0.004")
    assert debt_equity["source_concept"] == "DebtEquityRatio"
    assert debt_equity["unit"] == "pure"

    dscr = ratios_by_name["Debt Service Coverage Ratio"]
    assert dscr["value"] == Decimal("0.0312")

    iscr = ratios_by_name["Interest Service Coverage Ratio"]
    assert iscr["value"] == Decimal("0.0467")


def test_ratio_scale_discrepancy_flagged_as_info_only(normalized):
    issues = validate_ratio_plausibility(normalized.ratios)
    flagged_names = {i.check_name for i in issues}
    assert "ratio_scale_plausibility" in flagged_names
    assert all(i.severity == "INFO" for i in issues), "must never escalate to ERROR/WARNING on its own"
    # and, critically, the raw values are untouched by having been flagged
    ratios_by_name = {r["ratio_name"]: r for r in normalized.ratios}
    assert ratios_by_name["Debt Equity Ratio"]["value"] == Decimal("0.004")


# --------------------------------------------------------------------------
# Never silently drop unmapped facts (this filing has many — segments, OCI
# breakdown, disclosure text blocks — none of which are in scope for the
# normalized income/balance/cashflow schema)
# --------------------------------------------------------------------------

def test_unmapped_facts_counted_not_dropped(normalized, parsed_doc):
    # Every fact is still in parsed_doc.facts regardless of whether the
    # normalizer understood it — this is what makes raw xbrl_facts storage
    # (spec §26) complete even for facts we don't yet normalize.
    assert normalized.unmapped_fact_count > 0
    assert normalized.unmapped_fact_count < len(parsed_doc.facts)


# --------------------------------------------------------------------------
# Regression: live run against filing_id=7 hit two schema/derivation bugs
# in nse_financials_pipeline.process_filing() — NSE's discovery catalog did
# not supply period_start_date at all, and the raw ReportingQuarter text
# ("First quarter") overflowed financial_periods.financial_quarter
# VARCHAR(4). Both are fixed by deriving from the document's own disclosed
# dates rather than the catalog metadata or free text. These tests pin
# that behavior directly against the real fixture so it can't regress.
# --------------------------------------------------------------------------

def test_period_start_derivable_from_real_document_when_registry_lacks_it(parsed_doc):
    from nse_financials_pipeline import _derive_period_start_from_document

    derived = _derive_period_start_from_document(parsed_doc, PERIOD_END)
    assert derived == PERIOD_START


def test_quarter_code_derived_from_dates_fits_db_column():
    from nse_financials_pipeline import _quarter_code_from_dates

    code = _quarter_code_from_dates("quarterly", PERIOD_START, PERIOD_END)
    assert code == "Q1"
    assert len(code) <= 4  # must fit financial_periods.financial_quarter VARCHAR(4)

    # sanity-check the other three quarters of an Apr-Mar Indian fiscal year
    assert _quarter_code_from_dates("quarterly", date(2026, 7, 1), date(2026, 9, 30)) == "Q2"
    assert _quarter_code_from_dates("quarterly", date(2026, 10, 1), date(2026, 12, 31)) == "Q3"
    assert _quarter_code_from_dates("quarterly", date(2027, 1, 1), date(2027, 3, 31)) == "Q4"
    # annual periods must never get a quarter code
    assert _quarter_code_from_dates("annual", date(2025, 4, 1), date(2026, 3, 31)) is None
