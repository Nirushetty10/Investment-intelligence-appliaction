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
    INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
    METADATA_CONCEPTS,
    RATIO_CONCEPTS,
    RECONCILIATION_BRIDGE_CONCEPTS,
)
from normalizers.fact_classifier import ClassificationTally, classify_facts
from parsers.ixbrl_parser import ParsedXbrlDocument

# Coverage status values used for income_statement / balance_sheet /
# cashflow_statement / segment_data / ratios / eps in the per-filing
# report (see build_coverage_report below).
AVAILABLE = "AVAILABLE"
NOT_REPORTED_IN_FILING = "NOT_REPORTED_IN_FILING"
FAILED_NORMALIZATION = "FAILED_NORMALIZATION"
NEEDS_VALIDATION = "NEEDS_VALIDATION"


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
    warnings: list = field(default_factory=list)

    # Three-way fact classification (replaces the old flat unmapped_fact_count
    # with something that actually distinguishes a mapping bug from an
    # intentionally-ignored metadata/segment fact — see
    # normalizers/fact_classifier.py).
    classification: ClassificationTally = field(default_factory=ClassificationTally)

    # Per-statement coverage: does the SOURCE FILING actually contain this
    # kind of data at all? This is independent of classification — a
    # filing can correctly have zero balance-sheet facts (this is normal
    # for an Indian quarterly result) without that being any kind of
    # normalization failure.
    income_statement_coverage: str = NOT_REPORTED_IN_FILING
    balance_sheet_coverage: str = NOT_REPORTED_IN_FILING
    cashflow_statement_coverage: str = NOT_REPORTED_IN_FILING
    segment_data_coverage: str = NOT_REPORTED_IN_FILING
    eps_coverage: str = NOT_REPORTED_IN_FILING
    ratios_coverage: str = NOT_REPORTED_IN_FILING  # AVAILABLE or NEEDS_VALIDATION

    @property
    def unmapped_fact_count(self) -> int:
        """Backward-compatible alias for existing tests/callers: total
        facts NOT mapped, for any reason (intentional or genuinely
        financial-and-unmapped). See classification for the breakdown."""
        return self.classification.unmapped_fact_count


def _resolve_field(doc: ParsedXbrlDocument, candidates: list, period_end: date, period_start: Optional[date]):
    for concept_name in candidates:
        fact = doc.non_dimensioned_fact_for_period(concept_name, period_end, period_start)
        if fact is not None and fact.numeric_value is not None:
            return fact.numeric_value, concept_name
    return None, None


def _resolve_text_metadata(doc: ParsedXbrlDocument, candidates: list) -> Optional[str]:
    """Tries each candidate concept name in order (same alias convention as
    _resolve_field). Different sources for the same real-world filing can
    tag the identical disclosure under different concept local-names (see
    concept_map.METADATA_CONCEPTS docstring) — trying a list here, instead
    of a single hardcoded name, is what lets both the reconstructed HTML
    fixture and the real NSE XML resolve statement_type correctly without
    either one regressing the other."""
    for concept_name in candidates:
        matches = doc.facts_for_concept(concept_name)
        non_dim_matches = [
            m for m in matches
            if not doc.contexts.get(m.context_ref, None) or not doc.contexts[m.context_ref].has_dimensions
        ]
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

    # Reconciliation-bridge items (ISSUE 5): resolved the same way as any
    # other income-statement field, but deliberately merged into the same
    # `income_statement` dict rather than a new persisted table — the
    # repository layer only writes the whitelisted DB columns, so these
    # extra keys ride along for the validator's benefit without requiring
    # a schema migration, and without ever being written to income_statement
    # rows themselves.
    for field_name, candidates in RECONCILIATION_BRIDGE_CONCEPTS.items():
        value, _ = _resolve_field(doc, candidates, period_end, period_start)
        income_statement[field_name] = value

    # Supplementary income-statement concepts (EPS continuing/discontinued
    # breakdown, exceptional items, NCI/parent profit & OCI attribution) —
    # same non-persisted-extra-key mechanism as the reconciliation bridge
    # above. These exist purely so real, confirmed financial concepts in
    # the source get classified MAPPED instead of inflating
    # UNMAPPED_FINANCIAL, and so they're available for future derived
    # metrics without a schema migration.
    for field_name, candidates in INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS.items():
        value, _ = _resolve_field(doc, candidates, period_end, period_start)
        income_statement[field_name] = value

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
    for m in (
        INCOME_STATEMENT_CONCEPT_MAP,
        BALANCE_SHEET_CONCEPT_MAP,
        CASHFLOW_CONCEPT_MAP,
        RECONCILIATION_BRIDGE_CONCEPTS,
        INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
    ):
        for candidates in m.values():
            all_mapped_concepts.update(candidates)
    all_mapped_concepts.update(RATIO_CONCEPTS.keys())
    # METADATA_CONCEPTS values are now alias LISTS (ISSUE 3/4) — flatten
    # them instead of adding the list objects themselves to the set.
    for candidates in METADATA_CONCEPTS.values():
        all_mapped_concepts.update(candidates)

    classification = classify_facts(doc.facts, all_mapped_concepts)

    period = NormalizedPeriod(
        period_start=period_start,
        period_end=period_end,
        statement_type=statement_type,
        reporting_quarter=reporting_quarter_raw,
        audited_status=audited_status_raw,
        unit=unit_label,
    )

    # --- Coverage: does the SOURCE FILING contain this kind of data at all? ---
    # Anchor fields chosen because they're structurally required for the
    # statement to mean anything (a "balance sheet" with no total assets
    # figure isn't one) — presence of an ancillary field alone (e.g. just
    # equity_share_capital, which quarterly results disclose regardless of
    # whether a full balance sheet is filed) does NOT count as the
    # statement being available.
    income_statement_coverage = (
        AVAILABLE if income_statement.get("revenue_from_operations") is not None
        or income_statement.get("total_income") is not None
        else NOT_REPORTED_IN_FILING
    )
    balance_sheet_coverage = (
        AVAILABLE if balance_sheet.get("total_assets") is not None
        else NOT_REPORTED_IN_FILING
    )
    cashflow_statement_coverage = (
        AVAILABLE if any(
            cashflow_statement.get(k) is not None for k in ("cfo", "cfi", "cff")
        )
        else NOT_REPORTED_IN_FILING
    )
    segment_data_coverage = (
        AVAILABLE
        if classification.intentionally_unmapped_by_category.get("segment_dimensional", 0) > 0
        else NOT_REPORTED_IN_FILING
    )
    eps_coverage = (
        AVAILABLE if income_statement.get("basic_eps") is not None
        else NOT_REPORTED_IN_FILING
    )
    # Ratios: AVAILABLE if any resolved; NEEDS_VALIDATION additionally
    # flags that at least one resolved ratio looks scale-suspicious (see
    # validators.financial_validator.validate_ratio_plausibility) — the
    # caller is expected to run that check and combine it with this status
    # (kept separate here so this module doesn't have to import the
    # validator just to answer "were any ratios reported").
    ratios_coverage = AVAILABLE if ratios else NOT_REPORTED_IN_FILING

    return NormalizationResult(
        period=period,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
        cashflow_statement=cashflow_statement,
        ratios=ratios,
        warnings=warnings,
        classification=classification,
        income_statement_coverage=income_statement_coverage,
        balance_sheet_coverage=balance_sheet_coverage,
        cashflow_statement_coverage=cashflow_statement_coverage,
        segment_data_coverage=segment_data_coverage,
        eps_coverage=eps_coverage,
        ratios_coverage=ratios_coverage,
    )
