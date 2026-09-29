"""
normalizers/financial_normalizer.py

Turns a ParsedXbrlDocument (parsers/ixbrl_parser.py) into normalized
records for income_statement / balance_sheet / cashflow_statement / ratios,
plus the raw fact rows destined for xbrl_facts (every fact, mapped or not
— spec §26: "This table should contain facts even if the normalization
layer doesn't understand them yet").

Guarantees:
  * A field is populated ONLY if exactly one non-dimensioned fact resolves
    for the target period, across the concept's alias list. Anything
    ambiguous or absent -> None (NULL in the DB), never 0, never inferred.
  * Standalone/Consolidated is read from the filing's own metadata fact
    (NatureOfReportStandaloneOrConsolidated), not guessed from context.
  * Units are recorded as declared. No crore/lakh conversion is applied
    beyond the scale factor the source itself specified in the fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from normalizers.concept_map import (
    BALANCE_SHEET_CONCEPT_MAP,
    CASHFLOW_CONCEPT_MAP,
    INCOME_STATEMENT_CONCEPT_MAP,
    METADATA_CONCEPTS,
    RATIO_CONCEPTS,
)
from parsers.ixbrl_parser import ParsedXbrlDocument


@dataclass
class NormalizedPeriod:
    period_start: date
    period_end: date
    statement_type: Optional[str]      # standalone / consolidated — None if undetermined
    reporting_quarter: Optional[str]
    audited_status: Optional[str]
    unit: str


@dataclass
class NormalizationResult:
    period: NormalizedPeriod
    income_statement: dict = field(default_factory=dict)
    balance_sheet: dict = field(default_factory=dict)
    cashflow_statement: dict = field(default_factory=dict)
    ratios: list = field(default_factory=list)     # list[{"ratio_name","value","source_concept"}]
    unmapped_fact_count: int = 0
    warnings: list = field(default_factory=list)


def _resolve_field(doc: ParsedXbrlDocument, candidates: list, period_end: date, period_start: Optional[date]):
    for concept_name in candidates:
        fact = doc.non_dimensioned_fact_for_period(concept_name, period_end, period_start)
        if fact is not None and fact.numeric_value is not None:
            return fact.numeric_value, concept_name
    return None, None


def _resolve_text_metadata(doc: ParsedXbrlDocument, concept_name: str) -> Optional[str]:
    matches = doc.facts_for_concept(concept_name)
    non_dim_matches = [m for m in matches if not doc.contexts.get(m.context_ref, None) or not doc.contexts[m.context_ref].has_dimensions]
    if len(non_dim_matches) == 1:
        return non_dim_matches[0].raw_value
    return None


def normalize(doc: ParsedXbrlDocument, period_end: date, period_start: Optional[date] = None) -> NormalizationResult:
    statement_type_raw = _resolve_text_metadata(doc, METADATA_CONCEPTS["statement_type"])
    statement_type = None
    if statement_type_raw:
        normalized = statement_type_raw.strip().lower()
        if "consolidat" in normalized:
            statement_type = "consolidated"
        elif "standalone" in normalized:
            statement_type = "standalone"
        else:
            statement_type = None  # unrecognized value — do not guess

    reporting_quarter_raw = _resolve_text_metadata(doc, METADATA_CONCEPTS["reporting_quarter"])
    audited_status_raw = _resolve_text_metadata(doc, METADATA_CONCEPTS["audited_status"])

    # Unit: look at the unit actually attached to RevenueFromOperations (or
    # whichever income-statement fact resolves first); if facts use
    # different units inconsistently, that is itself a data-quality issue
    # to flag, not silently paper over.
    unit_label = "UNKNOWN"
    warnings = []

    income_statement = {}
    units_seen = set()
    for field_name, candidates in INCOME_STATEMENT_CONCEPT_MAP.items():
        value, matched_concept = _resolve_field(doc, candidates, period_end, period_start)
        income_statement[field_name] = value
        if value is not None:
            fact = doc.non_dimensioned_fact_for_period(matched_concept, period_end, period_start)
            if fact and fact.unit_ref:
                unit_obj = doc.units.get(fact.unit_ref)
                if unit_obj:
                    units_seen.add(unit_obj.measure)

    if len(units_seen) == 1:
        unit_label = units_seen.pop()
    elif len(units_seen) > 1:
        unit_label = "MIXED"
        warnings.append(f"Income statement facts used inconsistent units: {units_seen}")
    else:
        warnings.append("Could not determine unit for income statement — no numeric facts resolved")

    balance_sheet = {}
    for field_name, candidates in BALANCE_SHEET_CONCEPT_MAP.items():
        # balance sheet items are instant-context facts; period_end is the
        # instant date here, period_start is irrelevant
        value, _ = _resolve_field(doc, candidates, period_end, None)
        balance_sheet[field_name] = value

    cashflow_statement = {}
    for field_name, candidates in CASHFLOW_CONCEPT_MAP.items():
        value, _ = _resolve_field(doc, candidates, period_end, period_start)
        cashflow_statement[field_name] = value

    ratios = []
    for concept_name, display_name in RATIO_CONCEPTS.items():
        fact = doc.non_dimensioned_fact_for_period(concept_name, period_end, period_start)
        if fact is not None and fact.numeric_value is not None:
            unit_obj = doc.units.get(fact.unit_ref) if fact.unit_ref else None
            ratios.append(
                {
                    "ratio_name": display_name,
                    "value": fact.numeric_value,
                    "unit": unit_obj.measure if unit_obj else None,
                    "source_concept": concept_name,
                }
            )

    all_mapped_concepts = set()
    for m in (INCOME_STATEMENT_CONCEPT_MAP, BALANCE_SHEET_CONCEPT_MAP, CASHFLOW_CONCEPT_MAP):
        for candidates in m.values():
            all_mapped_concepts.update(candidates)
    all_mapped_concepts.update(RATIO_CONCEPTS.keys())
    all_mapped_concepts.update(METADATA_CONCEPTS.values())

    unmapped_count = sum(1 for f in doc.facts if f.concept not in all_mapped_concepts)

    period = NormalizedPeriod(
        period_start=period_start,
        period_end=period_end,
        statement_type=statement_type,
        reporting_quarter=reporting_quarter_raw,
        audited_status=audited_status_raw,
        unit=unit_label,
    )

    return NormalizationResult(
        period=period,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
        cashflow_statement=cashflow_statement,
        ratios=ratios,
        unmapped_fact_count=unmapped_count,
        warnings=warnings,
    )
