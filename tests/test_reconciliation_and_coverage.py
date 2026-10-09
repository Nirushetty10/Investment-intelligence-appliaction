"""
tests/test_reconciliation_and_coverage.py

Covers the mapping-coverage review follow-up:
  - balance-sheet three-way reconciliation (Assets = Equity + Liabilities)
  - cash-flow CFO+CFI+CFF(+FX) = net change in cash reconciliation
  - detail_component classification (exact-name table AND the
    AdjustmentsFor*/OtherAdjustments* pattern fallback)
  - the total_liabilities conflation bugfix (EquityAndLiabilities vs bare
    Liabilities are NOT the same figure)
  - provenance: a normalized value must trace back to a real raw fact
"""
from datetime import date
from decimal import Decimal

from normalizers.fact_classifier import INTENTIONALLY_UNMAPPED, UNMAPPED_FINANCIAL, classify_concept
from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import ParsedXbrlDocument, XbrlContext, XbrlFact, XbrlUnit
from validators.financial_validator import validate_balance_sheet, validate_cashflow


def _fact(context_ref, concept, value, unit_ref="INR"):
    return XbrlFact(
        context_ref=context_ref, namespace="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", concept=concept,
        raw_tag=f"in-capmkt:{concept}", raw_value=str(value),
        numeric_value=Decimal(str(value)), unit_ref=unit_ref, decimals="-5", scale=None, sign=None,
    )


# --------------------------------------------------------------------------
# total_liabilities bugfix: EquityAndLiabilities and bare Liabilities are
# DIFFERENT figures and must never be treated as interchangeable aliases.
# --------------------------------------------------------------------------

def test_bare_liabilities_concept_does_not_resolve_total_liabilities_field():
    """If a filing tags ONLY bare 'Liabilities' (not 'EquityAndLiabilities'),
    total_liabilities must stay None — it must NOT silently pick up the
    smaller bare-Liabilities figure, which would make the
    Assets-vs-total_liabilities check compare mismatched quantities."""
    period_end = date(2026, 3, 31)
    ctx = "I1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[
            _fact(ctx, "Assets", "1000000000"),
            _fact(ctx, "Liabilities", "400000000"),  # bare liabilities only — NOT the balancing total
        ],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )
    result = normalize(doc, period_end=period_end, period_start=None)
    assert result.balance_sheet["total_liabilities"] is None  # correctly NOT populated from bare "Liabilities"
    assert result.balance_sheet["total_liabilities_excl_equity"] == Decimal("400000000")  # correctly captured separately


def test_equity_and_liabilities_concept_resolves_total_liabilities_field():
    period_end = date(2026, 3, 31)
    ctx = "I1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[_fact(ctx, "EquityAndLiabilities", "1000000000")],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )
    result = normalize(doc, period_end=period_end, period_start=None)
    assert result.balance_sheet["total_liabilities"] == Decimal("1000000000")


# --------------------------------------------------------------------------
# Balance-sheet three-way reconciliation: Assets = Equity + Liabilities
# --------------------------------------------------------------------------

def _balance_sheet_doc(assets, equity, liabilities, period_end=date(2026, 3, 31)):
    ctx = "I1"
    return ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[
            _fact(ctx, "Assets", assets),
            _fact(ctx, "Equity", equity),
            _fact(ctx, "Liabilities", liabilities),
        ],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )


def test_three_way_reconciliation_passes_when_balanced():
    doc = _balance_sheet_doc(assets="1000000000", equity="600000000", liabilities="400000000")
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=None)
    issues = validate_balance_sheet(result.balance_sheet)
    errors = [i for i in issues if i.severity == "ERROR"]
    assert not errors, f"Unexpected errors: {errors}"
    three_way = [i for i in issues if i.check_name == "balance_sheet_three_way_reconciliation"]
    assert len(three_way) == 1 and three_way[0].severity == "INFO" or three_way == []
    # (passing case produces no issue at all for that check — only a
    # mismatch produces an entry; absence of an ERROR is the pass signal)


def test_three_way_reconciliation_flags_discrepancy_as_error():
    """ISSUE 7: 'Flag discrepancies rather than silently accepting them.'"""
    doc = _balance_sheet_doc(assets="1000000000", equity="600000000", liabilities="350000000")  # off by 50M
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=None)
    issues = validate_balance_sheet(result.balance_sheet)
    three_way_errors = [
        i for i in issues
        if i.check_name == "balance_sheet_three_way_reconciliation" and i.severity == "ERROR"
    ]
    assert len(three_way_errors) == 1
    assert "950000000" in three_way_errors[0].message or "350000000" in three_way_errors[0].message


def test_three_way_reconciliation_skipped_info_when_subtotals_absent():
    """A filing that only reports Assets and EquityAndLiabilities (no bare
    Equity/Liabilities subtotals) must not be flagged — skip, not fail."""
    period_end = date(2026, 3, 31)
    ctx = "I1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[_fact(ctx, "Assets", "1000000000"), _fact(ctx, "EquityAndLiabilities", "1000000000")],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )
    result = normalize(doc, period_end=period_end, period_start=None)
    issues = validate_balance_sheet(result.balance_sheet)
    three_way = [i for i in issues if i.check_name == "balance_sheet_three_way_reconciliation"]
    assert len(three_way) == 1
    assert three_way[0].severity == "INFO"
    errors = [i for i in issues if i.severity == "ERROR"]
    assert not errors


# --------------------------------------------------------------------------
# Cash-flow CFO + CFI + CFF (+ FX) = net change in cash
# --------------------------------------------------------------------------

def _cashflow_doc(cfo, cfi, cff, net_change, fx=None, period_start=date(2025, 4, 1), period_end=date(2026, 3, 31)):
    ctx = "D1"
    facts = [
        _fact(ctx, "CashFlowsFromUsedInOperatingActivities", cfo),
        _fact(ctx, "CashFlowsFromUsedInInvestingActivities", cfi),
        _fact(ctx, "CashFlowsFromUsedInFinancingActivities", cff),
        _fact(ctx, "IncreaseDecreaseInCashAndCashEquivalents", net_change),
    ]
    if fx is not None:
        facts.append(_fact(ctx, "EffectOfExchangeRateChangesOnCashAndCashEquivalents", fx))
    return ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, period_start=period_start, period_end=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=facts,
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )


def test_cashflow_components_reconcile_with_fx_no_error():
    # 100 (CFO) - 40 (CFI) - 30 (CFF) + 5 (FX) = 35 net change
    doc = _cashflow_doc(cfo="100", cfi="-40", cff="-30", fx="5", net_change="35")
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2025, 4, 1))
    issues = validate_cashflow(result.cashflow_statement)
    errors = [i for i in issues if i.severity == "ERROR"]
    assert not errors


def test_cashflow_components_mismatch_with_fx_is_error():
    """ISSUE 6: with FX disclosed, a mismatch is full-evidence — ERROR."""
    doc = _cashflow_doc(cfo="100", cfi="-40", cff="-30", fx="5", net_change="999")  # deliberately wrong
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2025, 4, 1))
    issues = validate_cashflow(result.cashflow_statement)
    component_errors = [
        i for i in issues
        if i.check_name == "cashflow_components_reconciliation" and i.severity == "ERROR"
    ]
    assert len(component_errors) == 1


def test_cashflow_components_mismatch_without_fx_is_warning_not_error():
    """Without FX disclosed, a gap is legitimately explainable (undisclosed
    FX effect) — WARNING, not ERROR."""
    doc = _cashflow_doc(cfo="100", cfi="-40", cff="-30", net_change="999", fx=None)
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2025, 4, 1))
    issues = validate_cashflow(result.cashflow_statement)
    component_errors = [
        i for i in issues
        if i.check_name == "cashflow_components_reconciliation" and i.severity == "ERROR"
    ]
    component_warnings = [
        i for i in issues
        if i.check_name == "cashflow_components_reconciliation" and i.severity == "WARNING"
    ]
    assert not component_errors
    assert len(component_warnings) == 1


def test_fx_effect_on_cash_mapped_correctly():
    doc = _cashflow_doc(cfo="100", cfi="-40", cff="-30", fx="5", net_change="35")
    result = normalize(doc, period_end=date(2026, 3, 31), period_start=date(2025, 4, 1))
    assert result.cashflow_statement["fx_effect_on_cash"] == Decimal("5")


# --------------------------------------------------------------------------
# detail_component classification
# --------------------------------------------------------------------------

def test_adjustments_for_prefix_pattern_classified_detail_component():
    """ISSUE 1/2/5: the many individual 'AdjustmentsFor<Item>' cash-flow
    reconciliation lines cannot be exhaustively enumerated by exact name —
    the prefix pattern must catch them."""
    for concept in (
        "AdjustmentsForFinanceCosts",
        "AdjustmentsForInterestIncome",
        "AdjustmentsForUnrealisedForeignExchangeLossesGains",
        "AdjustmentsForProvisions",
        "AdjustmentsForDecreaseIncreaseInInventories",
        "AdjustmentForDepreciationAndAmortisationExpenseNotYetMapped",  # singular variant
        "OtherAdjustmentsToReconcileProfitLoss",
    ):
        result = classify_concept(concept, mapped_concepts=set())
        assert result.classification == INTENTIONALLY_UNMAPPED, f"{concept} should be detail_component"
        assert result.category == "detail_component"


def test_adjustments_pattern_does_not_swallow_mapped_concepts():
    """A concept that happens to start with 'AdjustmentsFor' but IS
    explicitly mapped must still classify as MAPPED — the mapped-concepts
    check runs first."""
    result = classify_concept("AdjustmentsForFinanceCosts", mapped_concepts={"AdjustmentsForFinanceCosts"})
    from normalizers.fact_classifier import MAPPED
    assert result.classification == MAPPED


def test_unrelated_concept_not_caught_by_adjustments_pattern():
    """Sanity: the pattern must not accidentally match unrelated concepts."""
    result = classify_concept("RevenueFromOperations", mapped_concepts=set())
    assert result.classification == UNMAPPED_FINANCIAL  # not in any map in this isolated call — correctly surfaces, not swallowed by the pattern


# --------------------------------------------------------------------------
# Provenance: a normalized value must trace back to a real raw fact
# --------------------------------------------------------------------------

def test_normalized_value_traces_back_to_raw_fact():
    period_end = date(2026, 3, 31)
    ctx = "I1"
    doc = ParsedXbrlDocument(
        contexts={ctx: XbrlContext(context_ref=ctx, instant_date=period_end)},
        units={"INR": XbrlUnit(unit_ref="INR", measure="INR")},
        facts=[_fact(ctx, "Assets", "1234500000")],
        source_format="XBRL_XML",
        schema_refs=("in-capmkt-ent-2026-01-31.xsd",), namespace_map={"in-capmkt": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt", "in-capmkt-ent": "http://www.sebi.gov.in/xbrl/IntegratedFinance_IndAS/2026-01-31/in-capmkt/in-capmkt-ent"},
    )
    result = normalize(doc, period_end=period_end, period_start=None)
    normalized_value = result.balance_sheet["total_assets"]

    # trace back: find the raw fact this value came from
    matching_facts = [
        f for f in doc.facts
        if f.concept == "Assets" and f.numeric_value == normalized_value
    ]
    assert len(matching_facts) == 1
    raw_fact = matching_facts[0]
    # context
    ctx_obj = doc.contexts[raw_fact.context_ref]
    assert ctx_obj.instant_date == period_end
    # the raw_tag (fully qualified concept) is preserved for filing-level traceability
    assert raw_fact.raw_tag == "in-capmkt:Assets"
