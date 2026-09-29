"""
validators/financial_validator.py

Reconciliation checks (spec §28). Never modifies source values to force a
check to pass — a failed check is recorded as a data_quality_log row and
the filing's validation_status reflects it (VALIDATED / PARTIAL / FAILED).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

DEFAULT_TOLERANCE_PCT = Decimal("0.5")  # 0.5% relative tolerance for rounding


@dataclass
class ValidationIssue:
    check_name: str
    severity: str   # ERROR / WARNING / INFO
    message: str


def _within_tolerance(a: Decimal, b: Decimal, tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT) -> bool:
    if a is None or b is None:
        return True  # can't check what isn't there — not a validation failure, just skipped
    base = max(abs(a), abs(b), Decimal("1"))
    return abs(a - b) / base * Decimal(100) <= tolerance_pct


def validate_income_statement(stmt: dict) -> list:
    issues = []

    revenue = stmt.get("revenue_from_operations")
    other_income = stmt.get("other_income")
    total_income = stmt.get("total_income")
    if revenue is not None and other_income is not None and total_income is not None:
        expected = revenue + other_income
        if not _within_tolerance(expected, total_income):
            issues.append(
                ValidationIssue(
                    "income_reconciliation",
                    "ERROR",
                    f"Revenue ({revenue}) + Other Income ({other_income}) = {expected}, "
                    f"but Total Income reported as {total_income}",
                )
            )
    else:
        issues.append(
            ValidationIssue(
                "income_reconciliation", "INFO",
                "Skipped: revenue, other_income or total_income not available",
            )
        )

    pbt = stmt.get("profit_before_tax")
    total_tax = stmt.get("total_tax")
    pat = stmt.get("profit_for_period")
    if pbt is not None and total_tax is not None and pat is not None:
        expected = pbt - total_tax
        if not _within_tolerance(expected, pat):
            issues.append(
                ValidationIssue(
                    "profit_reconciliation",
                    "WARNING",  # WARNING not ERROR: PAT may legitimately differ from PBT-tax
                                # due to share of associates/JVs, discontinued ops, NCI — all
                                # real accounting items this simple check can't see.
                    f"PBT ({pbt}) - Tax ({total_tax}) = {expected}, but reported profit "
                    f"for period is {pat}. This can be legitimate (associates/JV share, "
                    f"discontinued operations, minority interest) — verify against raw facts "
                    f"before treating as an error.",
                )
            )
    else:
        issues.append(
            ValidationIssue("profit_reconciliation", "INFO", "Skipped: PBT, tax or PAT not available")
        )

    return issues


def validate_balance_sheet(stmt: dict) -> list:
    issues = []
    total_assets = stmt.get("total_assets")
    total_liabilities = stmt.get("total_liabilities")  # expected to include equity per schema note
    if total_assets is not None and total_liabilities is not None:
        if not _within_tolerance(total_assets, total_liabilities):
            issues.append(
                ValidationIssue(
                    "balance_sheet_reconciliation",
                    "ERROR",
                    f"Total Assets ({total_assets}) != Total Liabilities+Equity ({total_liabilities})",
                )
            )
    else:
        issues.append(
            ValidationIssue("balance_sheet_reconciliation", "INFO", "Skipped: total_assets or total_liabilities not available")
        )
    return issues


def validate_cashflow(stmt: dict) -> list:
    issues = []
    opening = stmt.get("opening_cash_balance")
    closing = stmt.get("closing_cash_balance")
    net_change = stmt.get("net_change_in_cash")
    if opening is not None and closing is not None and net_change is not None:
        expected = opening + net_change
        if not _within_tolerance(expected, closing):
            issues.append(
                ValidationIssue(
                    "cashflow_reconciliation",
                    "ERROR",
                    f"Opening Cash ({opening}) + Net Change ({net_change}) = {expected}, "
                    f"but Closing Cash reported as {closing}",
                )
            )
    else:
        issues.append(
            ValidationIssue("cashflow_reconciliation", "INFO", "Skipped: opening/closing/net-change not all available")
        )
    return issues


def overall_status(issues: list) -> str:
    if any(i.severity == "ERROR" for i in issues):
        return "FAILED" if all(i.severity == "ERROR" for i in issues) else "PARTIAL"
    return "VALIDATED"
