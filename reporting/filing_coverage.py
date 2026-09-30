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
from validators.financial_validator import validate_ratio_plausibility


@dataclass
class FilingCoverageReport:
    symbol: str
    financial_year: Optional[str]
    reporting_quarter: Optional[str]
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


def build_coverage_report(
    symbol: str,
    financial_year: Optional[str],
    result: NormalizationResult,
) -> FilingCoverageReport:
    ratio_issues = validate_ratio_plausibility(result.ratios)
    ratios_status = result.ratios_coverage
    if ratios_status == "AVAILABLE" and ratio_issues:
        ratios_status = f"{ratios_status} / {NEEDS_VALIDATION}"

    return FilingCoverageReport(
        symbol=symbol,
        financial_year=financial_year,
        reporting_quarter=result.period.reporting_quarter,
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
    )


def format_coverage_report(report: FilingCoverageReport) -> str:
    quarter = report.reporting_quarter or "—"
    fy = report.financial_year or "—"
    stmt_type = (report.statement_type or "unknown").capitalize()

    lines = [
        f"{report.symbol} — {quarter} {fy} — {stmt_type}",
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

    return "\n".join(lines)
