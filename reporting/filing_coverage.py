"""
reporting/filing_coverage.py

Builds the per-filing coverage report: what the source filing actually
contains (AVAILABLE / NOT_REPORTED_IN_FILING) versus how completely the
pipeline understood what it does contain (mapped / intentionally-unmapped /
unmapped-financial fact counts). The whole point is keeping these two
questions visibly separate — "the filing doesn't report a balance sheet"
and "we have a mapping bug" must never be conflated into one number.
"""
from dataclasses import dataclass, field
from typing import Optional

from normalizers.financial_normalizer import NEEDS_VALIDATION, NormalizationResult
from resolvers.period_resolver import ResolvedPeriod
from validators.financial_validator import validate_ratio_plausibility


@dataclass
class FilingCoverageReport:
    symbol: str
    financial_year: Optional[str]
    period_type: Optional[str]          # "annual" / "quarterly" / None — the CANONICAL value
    financial_quarter: Optional[str]    # "Q1".."Q4", or None for annual — the CANONICAL value
    period_start: Optional[object]       # date, for display/audit
    period_end: Optional[object]         # date, for display/audit
    statement_type: Optional[str]

    income_statement_status: str
    balance_sheet_status: str
    cashflow_status: str
    segment_data_status: str
    ratios_status: str
    eps_status: str

    mapped_facts: int
    intentionally_unmapped_facts: int
    unmapped_financial_facts: int
    unmapped_financial_concepts: dict = field(default_factory=dict)  # concept -> fact count

    ratio_validation_issues: list = field(default_factory=list)  # list of ValidationIssue

    # Taxonomy-provenance lens (spec item 4) — complementary to the three
    # counts above, not a replacement: answers "where does this concept
    # come from" (standard taxonomy / company extension / segment /
    # metadata / genuinely unmapped) rather than "do we have a field for
    # it". See normalizers/taxonomy_classifier.py.
    resolved_taxonomy_id: Optional[str] = None
    resolved_taxonomy_version: Optional[str] = None
    taxonomy_mapped_facts: int = 0
    company_extension_facts: int = 0
    unrecognized_namespace_facts: int = 0
    dimensional_segment_facts: int = 0
    structural_metadata_facts: int = 0
    genuinely_unmapped_financial_facts: int = 0
    genuinely_unmapped_financial_concepts: dict = field(default_factory=dict)


def build_coverage_report(
    symbol: str,
    resolved: ResolvedPeriod,
    result: NormalizationResult,
) -> FilingCoverageReport:
    """
    ISSUE 3 fix: the report must display the CANONICAL resolved period
    (resolved.period_type / resolved.financial_quarter / resolved.period_start
    / resolved.period_end from resolvers.period_resolver), never the raw
    XBRL disclosure text (result.period.reporting_quarter) — a real annual
    filing can carry a "ReportingQuarter"="Fourth quarter" disclosure fact
    even though the canonical/dominant period for the filing is annual
    (per submission_type + duration-context evidence), and showing that
    raw text in the report was exactly what produced the stale
    "Fourth quarter 2025-26" label for filing_id=9's coverage report
    despite canonical resolution already being correct.

    `resolved` is the SAME ResolvedPeriod object process_filing() uses to
    drive normalize()/persistence (ISSUE 2: one canonical period feeding
    normalization, reporting, and persistence alike) — not recomputed here.
    """
    ratio_issues = validate_ratio_plausibility(result.ratios)
    ratios_status = result.ratios_coverage
    if ratios_status == "AVAILABLE" and ratio_issues:
        ratios_status = f"{ratios_status} / {NEEDS_VALIDATION}"

    return FilingCoverageReport(
        symbol=symbol,
        financial_year=resolved.financial_year,
        period_type=resolved.period_type,
        financial_quarter=resolved.financial_quarter,
        period_start=resolved.period_start,
        period_end=resolved.period_end,
        statement_type=result.period.statement_type,
        income_statement_status=result.income_statement_coverage,
        balance_sheet_status=result.balance_sheet_coverage,
        cashflow_status=result.cashflow_statement_coverage,
        segment_data_status=result.segment_data_coverage,
        ratios_status=ratios_status,
        eps_status=result.eps_coverage,
        mapped_facts=result.classification.mapped_count,
        intentionally_unmapped_facts=result.classification.intentionally_unmapped_count,
        unmapped_financial_facts=result.classification.unmapped_financial_count,
        unmapped_financial_concepts=dict(result.classification.unmapped_financial_concepts),
        ratio_validation_issues=ratio_issues,
        resolved_taxonomy_id=result.resolved_taxonomy.taxonomy_id if result.resolved_taxonomy else None,
        resolved_taxonomy_version=result.resolved_taxonomy.version if result.resolved_taxonomy else None,
        taxonomy_mapped_facts=result.taxonomy_classification.counts.get("TAXONOMY_MAPPED", 0),
        company_extension_facts=result.taxonomy_classification.counts.get("COMPANY_EXTENSION", 0),
        unrecognized_namespace_facts=result.taxonomy_classification.counts.get("UNRECOGNIZED_NAMESPACE", 0),
        dimensional_segment_facts=result.taxonomy_classification.counts.get("DIMENSIONAL_SEGMENT", 0),
        structural_metadata_facts=result.taxonomy_classification.counts.get("STRUCTURAL_METADATA", 0),
        genuinely_unmapped_financial_facts=result.taxonomy_classification.counts.get("GENUINELY_UNMAPPED_FINANCIAL", 0),
        genuinely_unmapped_financial_concepts=dict(
            result.taxonomy_classification.by_concept.get("GENUINELY_UNMAPPED_FINANCIAL", {})
        ),
    )


def format_coverage_report(report: FilingCoverageReport) -> str:
    fy = report.financial_year or "—"
    stmt_type = (report.statement_type or "unknown").capitalize()

    if report.period_type == "annual":
        period_label = f"Annual FY{fy}"
    elif report.period_type == "quarterly":
        period_label = f"{report.financial_quarter or '—'} FY{fy}"
    else:
        period_label = f"Unresolved period FY{fy}"

    if report.period_start and report.period_end:
        period_label += f" ({report.period_start} \u2192 {report.period_end})"

    lines = [
        f"{report.symbol} — {period_label} — {stmt_type}",
        "",
        f"  Income Statement: {report.income_statement_status}",
        f"  Balance Sheet:    {report.balance_sheet_status}",
        f"  Cash Flow:        {report.cashflow_status}",
        f"  Segment Data:     {report.segment_data_status}",
        f"  Ratios:           {report.ratios_status}",
        f"  EPS:              {report.eps_status}",
        "",
        f"  Mapped facts:              {report.mapped_facts}",
        f"  Intentionally unmapped:    {report.intentionally_unmapped_facts}",
        f"  Unmapped financial facts:  {report.unmapped_financial_facts}",
    ]

    if report.unmapped_financial_concepts:
        lines.append("")
        lines.append("  Unmapped financial concepts (need review):")
        for concept, count in sorted(report.unmapped_financial_concepts.items()):
            lines.append(f"    - {concept} ({count} fact{'s' if count != 1 else ''})")

    if report.ratio_validation_issues:
        lines.append("")
        lines.append("  Ratio validation notes:")
        for issue in report.ratio_validation_issues:
            lines.append(f"    - [{issue.severity}] {issue.message}")

    taxonomy_label = (
        f"{report.resolved_taxonomy_id} ({report.resolved_taxonomy_version})"
        if report.resolved_taxonomy_id else "UNRESOLVED (no registered taxonomy matched this filing's namespace)"
    )
    lines.append("")
    lines.append(f"  Taxonomy: {taxonomy_label}")
    lines.append(f"    Taxonomy-mapped:              {report.taxonomy_mapped_facts}")
    lines.append(f"    Company extension:            {report.company_extension_facts}")
    lines.append(f"    Unrecognized namespace:       {report.unrecognized_namespace_facts}")
    lines.append(f"    Dimensional/segment:           {report.dimensional_segment_facts}")
    lines.append(f"    Structural/metadata:           {report.structural_metadata_facts}")
    lines.append(f"    Genuinely unmapped financial:  {report.genuinely_unmapped_financial_facts}")
    if report.genuinely_unmapped_financial_concepts:
        for concept, count in sorted(report.genuinely_unmapped_financial_concepts.items()):
            lines.append(f"      - {concept} ({count} fact{'s' if count != 1 else ''})")

    return "\n".join(lines)
