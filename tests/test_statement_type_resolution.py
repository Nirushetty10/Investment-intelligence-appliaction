"""
tests/test_statement_type_resolution.py

Explicit regression coverage for all four statement_type x period_type
combinations called out in the issue: Q1 consolidated, Q1 standalone,
Q4/Annual consolidated, Q4/Annual standalone. The real fixtures
(reliance_2026q1_consolidated.xbrl.xml and
reliance_2026q4_annual_consolidated.xbrl.xml) only cover the
"consolidated" half of each pair, so the two "standalone" cases are
built here as minimal synthetic documents using the real parser
dataclasses (not duck-typed fakes) — genuinely exercising
normalize()/_resolve_text_metadata end to end, just with a smaller fact
set than the full fixtures.
"""
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import ParsedXbrlDocument, XbrlContext, XbrlFact, XbrlUnit, parse_document
from resolvers.period_resolver import resolve_canonical_period

Q1_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q1_consolidated.xbrl.xml"
ANNUAL_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliance_2026q4_annual_consolidated.xbrl.xml"


def _minimal_doc(statement_type_text: str, period_start: date, period_end: date, reporting_quarter: str) -> ParsedXbrlDocument:
    """Builds a minimal but real ParsedXbrlDocument with just enough facts
    to exercise statement_type/period metadata resolution and one income-
    statement figure, for a given disclosed statement type text."""
    ctx_ref = "D1"
    contexts = {
        ctx_ref: XbrlContext(context_ref=ctx_ref, period_start=period_start, period_end=period_end),
    }
    units = {"INR": XbrlUnit(unit_ref="INR", measure="INR")}
    facts = [
        XbrlFact(
            context_ref=ctx_ref, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", concept="NatureOfReportStandaloneConsolidated",
            raw_tag="in-capmkt:NatureOfReportStandaloneConsolidated", raw_value=statement_type_text,
            numeric_value=None, unit_ref=None, decimals=None, scale=None, sign=None,
        ),
        XbrlFact(
            context_ref=ctx_ref, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", concept="ReportingQuarter",
            raw_tag="in-capmkt:ReportingQuarter", raw_value=reporting_quarter,
            numeric_value=None, unit_ref=None, decimals=None, scale=None, sign=None,
        ),
        XbrlFact(
            context_ref=ctx_ref, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", concept="RevenueFromOperations",
            raw_tag="in-capmkt:RevenueFromOperations", raw_value="1000000",
            numeric_value=Decimal("1000000"), unit_ref="INR", decimals="-5", scale=None, sign=None,
        ),
    ]
    return ParsedXbrlDocument(
        contexts=contexts, units=units, facts=facts, source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )


# --------------------------------------------------------------------------
# Q1 consolidated (real fixture — sanity re-confirmation)
# --------------------------------------------------------------------------

def test_q1_consolidated_from_real_fixture():
    doc = parse_document(Q1_FIXTURE_PATH.read_bytes())
    result = normalize(doc, period_end=date(2026, 6, 30), period_start=date(2026, 4, 1))
    assert result.period.statement_type == "consolidated"


# --------------------------------------------------------------------------
# Q1 standalone (synthetic — real fixture only has consolidated)
# --------------------------------------------------------------------------

def test_q1_standalone_resolves_correctly():
    doc = _minimal_doc("Standalone", date(2026, 4, 1), date(2026, 6, 30), "First quarter")
    result = normalize(doc, period_end=date(2026, 6, 30), period_start=date(2026, 4, 1))
    assert result.period.statement_type == "standalone"
    assert result.income_statement["revenue_from_operations"] == Decimal("1000000")


# --------------------------------------------------------------------------
# Q4/Annual consolidated (real fixture — sanity re-confirmation)
# --------------------------------------------------------------------------

def test_annual_consolidated_from_real_fixture():
    doc = parse_document(ANNUAL_FIXTURE_PATH.read_bytes())
    filing = {
        "period_end_date": date(2026, 3, 31), "period_type": "quarterly",
        "period_start_date": None, "submission_type": "Audited",
        "statement_type": "consolidated", "financial_year": None,
    }
    resolved = resolve_canonical_period(doc, filing)
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)
    assert resolved.period_type == "annual"
    assert result.period.statement_type == "consolidated"


# --------------------------------------------------------------------------
# Q4/Annual standalone (synthetic)
# --------------------------------------------------------------------------

def test_annual_standalone_resolves_correctly():
    doc = _minimal_doc("Standalone", date(2025, 4, 1), date(2026, 3, 31), "Fourth quarter")
    filing = {
        "period_end_date": date(2026, 3, 31), "period_type": "annual",
        "period_start_date": None, "submission_type": "Audited",
        "statement_type": "standalone", "financial_year": None,
    }
    resolved = resolve_canonical_period(doc, filing)
    result = normalize(doc, period_end=resolved.period_end, period_start=resolved.period_start)
    assert resolved.period_type == "annual"
    assert resolved.period_start == date(2025, 4, 1)
    assert result.period.statement_type == "standalone"


# --------------------------------------------------------------------------
# No cross-contamination: a standalone-tagged document must never resolve
# as consolidated (or vice versa) just because of period_type handling
# --------------------------------------------------------------------------

def test_standalone_never_misread_as_consolidated():
    doc = _minimal_doc("Standalone", date(2026, 1, 1), date(2026, 3, 31), "Fourth quarter")
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2026, 1, 1))
    assert result.period.statement_type == "standalone"
    assert result.period.statement_type != "consolidated"


def test_consolidated_never_misread_as_standalone():
    doc = _minimal_doc("Consolidated", date(2026, 1, 1), date(2026, 3, 31), "Fourth quarter")
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2026, 1, 1))
    assert result.period.statement_type == "consolidated"
    assert result.period.statement_type != "standalone"
