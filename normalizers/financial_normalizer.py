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
    BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS,
    CASHFLOW_CONCEPT_MAP,
    CASHFLOW_SUPPLEMENTARY_CONCEPTS,
    DETAIL_COMPONENT_CONCEPTS,
    INCOME_STATEMENT_CONCEPT_MAP,
    INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
    METADATA_CONCEPTS,
    RATIO_CONCEPTS,
    RECONCILIATION_BRIDGE_CONCEPTS,
)
from normalizers.fact_classifier import ClassificationTally, classify_facts
from normalizers.taxonomy_classifier import TaxonomyClassificationTally, classify_facts_taxonomy
from taxonomy.registry import resolve_taxonomy_for_document
from taxonomy.catalog import get_concept_metadata, taxonomy_version_status
from taxonomy.period_types import get_concept_period_type, get_period_type_catalog_version
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

    # Complementary 5-way TAXONOMY-PROVENANCE lens (spec item 4): taxonomy
    # identity resolved for this filing, plus the
    # taxonomy_mapped/company_extension/dimensional_segment/
    # structural_metadata/genuinely_unmapped_financial breakdown — see
    # normalizers/taxonomy_classifier.py.
    resolved_taxonomy: object = None  # taxonomy.registry.TaxonomyIdentity or None
    taxonomy_classification: TaxonomyClassificationTally = field(
        default_factory=TaxonomyClassificationTally.empty
    )

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
    # Guard findings are validation-ready records describing financial facts
    # that were deliberately refused during normalization. The raw facts are
    # still retained; callers should persist these findings to data_quality_log.
    guard_findings: list = field(default_factory=list)

    @property
    def unmapped_fact_count(self) -> int:
        """Backward-compatible alias for existing tests/callers: total
        facts NOT mapped, for any reason (intentional or genuinely
        financial-and-unmapped). See classification for the breakdown."""
        return self.classification.unmapped_fact_count


def _resolve_field(
    doc: ParsedXbrlDocument,
    candidates: list,
    period_end: date,
    period_start: Optional[date],
    *,
    allowed_namespaces: set,
    resolved_taxonomy=None,
):
    for concept_name in candidates:
        # XBRL periodType belongs to the taxonomy concept, not to the
        # canonical target field. For example, the supplied Ind AS schema
        # declares PaidUpValueOfEquityShareCapital as duration.
        if resolved_taxonomy is not None and taxonomy_version_status(
            resolved_taxonomy.taxonomy_id, resolved_taxonomy.version
        ) == "EXACT_VERSION_AVAILABLE":
            # Once an exact taxonomy version is available, a local-name alias
            # absent from that version is not eligible for canonical mapping.
            if get_concept_metadata(
                resolved_taxonomy.taxonomy_id, resolved_taxonomy.version, concept_name
            ) is None:
                continue
        expected_period_type = get_concept_period_type(
            concept_name,
            taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
            version=resolved_taxonomy.version if resolved_taxonomy else None,
        )
        fact = doc.non_dimensioned_fact_for_period(
            concept_name,
            period_end,
            period_start,
            allowed_namespaces=allowed_namespaces,
            expected_period_type=expected_period_type,
        )
        if fact is not None and fact.numeric_value is not None:
            return fact.numeric_value, concept_name
    return None, None


def _context_matches_period(ctx, period_end, period_start) -> bool:
    if ctx is None:
        return False
    if ctx.is_instant:
        return ctx.instant_date == period_end
    if period_start is not None:
        return ctx.period_end == period_end and ctx.period_start == period_start
    return ctx.period_end == period_end


def _resolve_text_metadata(
    doc: ParsedXbrlDocument,
    candidates: list,
    period_end=None,
    period_start=None,
    allowed_namespaces=None,
    resolved_taxonomy=None,
) -> Optional[str]:
    """Tries each candidate concept name in order (same alias convention as
    _resolve_field). Different sources for the same real-world filing can
    tag the identical disclosure under different concept local-names (see
    concept_map.METADATA_CONCEPTS docstring) — trying a list here, instead
    of a single hardcoded name, is what lets both the reconstructed HTML
    fixture and the real NSE XML resolve statement_type correctly without
    either one regressing the other.

    ISSUE (found against real filing_id=9): a real ANNUAL filing commonly
    repeats the identical metadata disclosure (e.g.
    "NatureOfReportStandaloneConsolidated" = "Consolidated") on BOTH its
    annual context AND its Q4-only context — both non-dimensioned. The
    original version of this function required EXACTLY ONE non-dimensioned
    match across the WHOLE document, which broke the instant that
    happened, returning None (statement_type_resolved = None) even though
    there was no real ambiguity in the value itself.

    Fixed by, in order of preference:
      1. If the canonical period_end/period_start is known, prefer the
         fact(s) tagged on the context matching that exact period (ISSUE 2:
         the canonical period should drive this resolution too, not just
         the numeric fields).
      2. If multiple matches remain (within the matched period, or across
         the whole document when no period was given), and they all AGREE
         on the same value, that is not genuine ambiguity — just the same
         disclosure repeated — so return the common value.
      3. Only return None when matches are genuinely absent, or genuinely
         disagree in value.
    """
    for concept_name in candidates:
        matches = doc.facts_for_concept(concept_name)
        if allowed_namespaces is not None:
            matches = [m for m in matches if m.namespace in allowed_namespaces]
        non_dim_matches = [
            m for m in matches
            if not doc.contexts.get(m.context_ref, None) or not doc.contexts[m.context_ref].has_dimensions
        ]
        if not non_dim_matches:
            continue

        if period_end is not None:
            period_matches = [
                m for m in non_dim_matches
                if _context_matches_period(doc.contexts.get(m.context_ref), period_end, period_start)
            ]
            if period_matches:
                values = {m.raw_value for m in period_matches}
                if len(values) == 1:
                    return period_matches[0].raw_value
                continue  # genuine disagreement even within the matching period — try next alias

        if len(non_dim_matches) == 1:
            return non_dim_matches[0].raw_value

        values = {m.raw_value for m in non_dim_matches}
        if len(values) == 1:
            return non_dim_matches[0].raw_value
        # genuinely conflicting values across the document with no period
        # to disambiguate by — do not guess, try the next alias concept.
    return None


def _normalization_period_expectations(resolved_taxonomy=None) -> dict:
    """Return canonical numeric concepts with taxonomy-declared period types.

    Missing metadata is unknown, not permission to assume instant or duration
    from the name of the financial-statement field.
    """
    concepts = set()
    for mapping in (
        INCOME_STATEMENT_CONCEPT_MAP,
        BALANCE_SHEET_CONCEPT_MAP,
        BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS,
        CASHFLOW_CONCEPT_MAP,
        CASHFLOW_SUPPLEMENTARY_CONCEPTS,
        RECONCILIATION_BRIDGE_CONCEPTS,
        INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS,
    ):
        for candidates in mapping.values():
            concepts.update(candidates)
    concepts.update(RATIO_CONCEPTS)
    return {
        concept: get_concept_period_type(
            concept,
            taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
            version=resolved_taxonomy.version if resolved_taxonomy else None,
        )
        for concept in concepts
    }


def _build_guard_findings(
    doc: ParsedXbrlDocument,
    period_end: date,
    period_start: Optional[date],
    allowed_namespaces: set,
    resolved_taxonomy,
) -> list:
    findings = []
    seen = set()

    if resolved_taxonomy is None:
        findings.append({
            "check_name": "taxonomy_resolution",
            "severity": "WARNING",
            "message": (
                "No registered standard taxonomy namespace was identified for this filing. "
                "Canonical financial mappings were disabled; raw facts remain available for review."
            ),
        })
        return findings

    if not resolved_taxonomy.canonical_mapping_enabled:
        findings.append({
            "check_name": "taxonomy_mapping_disabled",
            "severity": "WARNING",
            "message": (
                f"Taxonomy family {resolved_taxonomy.taxonomy_id!r} was identified, but its "
                "canonical financial mappings have not been validated/enabled. No canonical "
                "financial values were emitted; raw facts remain available for review."
            ),
        })
        return findings

    catalog_status = taxonomy_version_status(
        resolved_taxonomy.taxonomy_id, resolved_taxonomy.version
    )
    if catalog_status != "EXACT_VERSION_AVAILABLE":
        fallback_version = get_period_type_catalog_version()
        findings.append({
            "check_name": "taxonomy_catalog_exact_version_unavailable",
            "severity": "WARNING",
            "message": (
                f"No exact imported taxonomy catalog is available for "
                f"{resolved_taxonomy.taxonomy_id}@{resolved_taxonomy.version}. "
                f"The legacy {fallback_version} periodType list may be used only as a defensive "
                "fallback; concept existence and exact-version semantics remain unverified. "
                "Keep this filing out of fully trusted data until the matching taxonomy package is imported."
            ),
        })

    expectations = _normalization_period_expectations(resolved_taxonomy)
    for fact in doc.facts:
        if fact.concept not in expectations:
            continue
        context = doc.contexts.get(fact.context_ref)
        if context is None or context.has_dimensions:
            continue

        context_end = context.instant_date if context.is_instant else context.period_end
        if context_end != period_end:
            continue
        # Annual filings can legitimately contain both annual and Q4 contexts
        # for the same duration concept. Ignore alternate durations rather
        # than labeling them invalid for the requested canonical period.
        if not context.is_instant and period_start is not None and context.period_start != period_start:
            continue

        if fact.namespace not in allowed_namespaces:
            finding = (
                "namespace_guard",
                fact.concept,
                fact.context_ref,
                fact.namespace,
            )
            if finding not in seen:
                seen.add(finding)
                findings.append({
                    "check_name": "normalization_namespace_guard",
                    "severity": "WARNING",
                    "message": (
                        f"Rejected {fact.raw_tag} in context {fact.context_ref}: its namespace "
                        f"{fact.namespace!r} is not the registered taxonomy namespace "
                        f"{resolved_taxonomy.namespace!r}; local-name matching is not sufficient "
                        "for a canonical financial mapping. The raw fact was preserved."
                    ),
                })
            continue

        expected_type = expectations[fact.concept]
        if expected_type not in ("instant", "duration"):
            finding = ("missing_period_type_metadata", fact.concept, fact.context_ref)
            if finding not in seen:
                seen.add(finding)
                findings.append({
                    "check_name": "normalization_period_type_metadata_missing",
                    "severity": "WARNING",
                    "message": (
                        f"No periodType metadata is available for mapped concept {fact.concept!r} "
                        f"in context {fact.context_ref}. It was not checked against a guessed "
                        "statement-level period type; review the exact taxonomy XSD before trusting it."
                    ),
                })
            continue
        actual_type = "instant" if context.is_instant else "duration"
        if actual_type != expected_type:
            finding = ("period_type_guard", fact.concept, fact.context_ref, actual_type, expected_type)
            if finding not in seen:
                seen.add(finding)
                findings.append({
                    "check_name": "normalization_period_type_guard",
                    "severity": "WARNING",
                    "message": (
                        f"Rejected {fact.raw_tag} in context {fact.context_ref}: expected a "
                        f"{expected_type} context for its mapped statement field, but the source "
                        f"context is {actual_type}. The raw fact was preserved."
                    ),
                })

    return findings


def normalize(doc: ParsedXbrlDocument, period_end: date, period_start: Optional[date] = None) -> NormalizationResult:
    # Resolve taxonomy before reading any values. If there is no supported
    # taxonomy identity, we fail closed rather than guessing from local names.
    resolved_taxonomy = resolve_taxonomy_for_document(doc)
    allowed_namespaces = (
        {resolved_taxonomy.namespace}
        if resolved_taxonomy is not None and resolved_taxonomy.canonical_mapping_enabled
        else set()
    )

    statement_type_raw = _resolve_text_metadata(
        doc, METADATA_CONCEPTS["statement_type"], period_end=period_end,
        period_start=period_start, allowed_namespaces=allowed_namespaces,
        resolved_taxonomy=resolved_taxonomy,
    )
    statement_type = None
    if statement_type_raw:
        normalized = statement_type_raw.strip().lower()
        if "consolidat" in normalized:
            statement_type = "consolidated"
        elif "standalone" in normalized:
            statement_type = "standalone"
        else:
            statement_type = None  # unrecognized value — do not guess

    reporting_quarter_raw = _resolve_text_metadata(
        doc, METADATA_CONCEPTS["reporting_quarter"], period_end=period_end,
        period_start=period_start, allowed_namespaces=allowed_namespaces,
        resolved_taxonomy=resolved_taxonomy,
    )
    audited_status_raw = _resolve_text_metadata(
        doc, METADATA_CONCEPTS["audited_status"], period_end=period_end,
        period_start=period_start, allowed_namespaces=allowed_namespaces,
        resolved_taxonomy=resolved_taxonomy,
    )

    # Unit: look at the unit actually attached to RevenueFromOperations (or
    # whichever income-statement fact resolves first); if facts use
    # different units inconsistently, that is itself a data-quality issue
    # to flag, not silently paper over.
    unit_label = "UNKNOWN"
    warnings = []

    income_statement = {}
    units_seen = set()
    for field_name, candidates in INCOME_STATEMENT_CONCEPT_MAP.items():
        value, matched_concept = _resolve_field(
            doc, candidates, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        income_statement[field_name] = value
        if value is not None:
            fact = doc.non_dimensioned_fact_for_period(
                matched_concept, period_end, period_start,
                allowed_namespaces=allowed_namespaces,
                expected_period_type=get_concept_period_type(
                    matched_concept,
                    taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
                    version=resolved_taxonomy.version if resolved_taxonomy else None,
                ),
            )
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
        # The taxonomy's own periodType is authoritative per concept. Do not
        # assume all fields presented with balance-sheet information are
        # instant concepts; the supplied schema marks share-capital value as
        # a duration concept.
        value, _ = _resolve_field(
            doc, candidates, period_end, None,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        balance_sheet[field_name] = value

    # Balance-sheet SUBTOTAL concepts (CurrentAssets, Equity, bare
    # Liabilities, etc.) — same non-persisted-extra-key mechanism as the
    # income-statement supplementary fields: merged into the same
    # `balance_sheet` dict, never written to a new DB column, used for
    # reconciliation (validate_balance_sheet's three-way
    # Assets = Equity + Liabilities check) and traceability.
    for field_name, candidates in BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS.items():
        value, _ = _resolve_field(
            doc, candidates, period_end, None,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        balance_sheet[field_name] = value

    cashflow_statement = {}
    for field_name, candidates in CASHFLOW_CONCEPT_MAP.items():
        value, _ = _resolve_field(
            doc, candidates, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        cashflow_statement[field_name] = value

    # Cash-flow supplementary concepts (FX effect, interest/dividend
    # received detail, equity issuance/buyback) — same mechanism, merged
    # into `cashflow_statement`. fx_effect_on_cash specifically feeds the
    # CFO + CFI + CFF + FX = net change in cash reconciliation.
    for field_name, candidates in CASHFLOW_SUPPLEMENTARY_CONCEPTS.items():
        value, _ = _resolve_field(
            doc, candidates, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        cashflow_statement[field_name] = value

    # Reconciliation-bridge items (ISSUE 5): resolved the same way as any
    # other income-statement field, but deliberately merged into the same
    # `income_statement` dict rather than a new persisted table — the
    # repository layer only writes the whitelisted DB columns, so these
    # extra keys ride along for the validator's benefit without requiring
    # a schema migration, and without ever being written to income_statement
    # rows themselves.
    for field_name, candidates in RECONCILIATION_BRIDGE_CONCEPTS.items():
        value, _ = _resolve_field(
            doc, candidates, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        income_statement[field_name] = value

    # Supplementary income-statement concepts (EPS continuing/discontinued
    # breakdown, exceptional items, NCI/parent profit & OCI attribution) —
    # same non-persisted-extra-key mechanism as the reconciliation bridge
    # above. These exist purely so real, confirmed financial concepts in
    # the source get classified MAPPED instead of inflating
    # UNMAPPED_FINANCIAL, and so they're available for future derived
    # metrics without a schema migration.
    for field_name, candidates in INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS.items():
        value, _ = _resolve_field(
            doc, candidates, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            resolved_taxonomy=resolved_taxonomy,
        )
        income_statement[field_name] = value

    ratios = []
    for concept_name, display_name in RATIO_CONCEPTS.items():
        fact = doc.non_dimensioned_fact_for_period(
            concept_name, period_end, period_start,
            allowed_namespaces=allowed_namespaces,
            expected_period_type=get_concept_period_type(
                concept_name,
                taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
                version=resolved_taxonomy.version if resolved_taxonomy else None,
            ),
        )
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
        BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS,
        CASHFLOW_CONCEPT_MAP,
        CASHFLOW_SUPPLEMENTARY_CONCEPTS,
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

    classification = classify_facts(
        doc.facts,
        all_mapped_concepts,
        allowed_namespaces=allowed_namespaces,
        taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
        taxonomy_version=resolved_taxonomy.version if resolved_taxonomy else None,
        mapped_metadata_concepts={concept for candidates in METADATA_CONCEPTS.values() for concept in candidates},
    )
    taxonomy_classification = classify_facts_taxonomy(
        doc,
        all_mapped_concepts,
        canonical_mapping_enabled=bool(
            resolved_taxonomy is not None and resolved_taxonomy.canonical_mapping_enabled
        ),
        taxonomy_id=resolved_taxonomy.taxonomy_id if resolved_taxonomy else None,
        taxonomy_version=resolved_taxonomy.version if resolved_taxonomy else None,
        mapped_metadata_concepts={concept for candidates in METADATA_CONCEPTS.values() for concept in candidates},
    )
    guard_findings = _build_guard_findings(
        doc, period_end, period_start, allowed_namespaces, resolved_taxonomy
    )

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
        resolved_taxonomy=resolved_taxonomy,
        taxonomy_classification=taxonomy_classification,
        income_statement_coverage=income_statement_coverage,
        balance_sheet_coverage=balance_sheet_coverage,
        cashflow_statement_coverage=cashflow_statement_coverage,
        segment_data_coverage=segment_data_coverage,
        eps_coverage=eps_coverage,
        ratios_coverage=ratios_coverage,
        guard_findings=guard_findings,
    )
