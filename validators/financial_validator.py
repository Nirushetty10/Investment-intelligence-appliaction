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
    # Structured identifier of what this issue is about (e.g. a ratio_name
    # for ratio_scale_plausibility) — set when applicable, so callers never
    # need to parse `message` text to find out which ratio/field triggered
    # this (ISSUE 6: nse_financials_pipeline.process_filing needs this to
    # set ratios.needs_validation per-ratio without fragile string parsing).
    subject: str = None


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

    issues.extend(_validate_profit_reconciliation(stmt))

    return issues


def _validate_profit_reconciliation(stmt: dict) -> list:
    """
    ISSUE 5: PBT/Tax/PAT reconciliation, aware of the continuing-operations
    bridge (associates/JV share, discontinued operations) when the source
    explicitly reports it.

    Two independent checks, deliberately kept separate because they have
    different confidence levels:

    1. "continuing_operations_reconciliation" (ERROR-level): PBT - Tax must
       equal profit-after-tax FROM CONTINUING OPERATIONS
       (income_statement["profit_after_tax"], mapped from
       ProfitLossForPeriodFromContinuingOperations). This is pure
       arithmetic with no other legitimate accounting explanation for a
       mismatch, so a failure here is a real ERROR — but it only runs when
       the source actually reports this specific continuing-operations
       figure separately from the final "profit for period" line; filings
       that don't break this out (e.g. the reconstructed HTML fixture)
       skip this check rather than being forced through it.

    2. "total_profit_reconciliation" (severity depends on evidence):
       continuing-ops PAT + associates/JV share + discontinued-ops-after-tax
       should equal profit_for_period. When the source explicitly tagged
       the bridge items (even as an explicit 0 — see
       ISSUE 5 notes on RELIANCE reporting 0 for discontinued operations),
       we have enough information to treat a mismatch as a genuine ERROR.
       When neither bridge item is available, we fall back to the older,
       looser WARNING check (PBT - Tax vs PAT directly) — a mismatch could
       still be a legitimate item this check simply cannot see (e.g.
       non-controlling interest allocation, which is a split of total
       profit and is deliberately NEVER added into this equation, or some
       other item the taxonomy doesn't expose to us).

    Never invents a bridge-item value: `stmt.get(...)` returning None means
    "not reported", and a None bridge item is treated as "not applicable to
    this reconciliation" only when used in the addition below — it is never
    written back into the dict or the database as 0.
    """
    issues = []

    pbt = stmt.get("profit_before_tax")
    total_tax = stmt.get("total_tax")
    continuing_pat = stmt.get("profit_after_tax")
    pat = stmt.get("profit_for_period")
    assoc_jv = stmt.get("share_of_profit_associates_jv")
    discontinued = stmt.get("profit_discontinued_operations_after_tax")

    # --- Check 1: continuing-operations arithmetic (pure math, ERROR-level) ---
    if pbt is not None and total_tax is not None and continuing_pat is not None:
        expected_continuing = pbt - total_tax
        if not _within_tolerance(expected_continuing, continuing_pat):
            issues.append(
                ValidationIssue(
                    "continuing_operations_reconciliation",
                    "ERROR",
                    f"PBT ({pbt}) - Tax ({total_tax}) = {expected_continuing}, but "
                    f"profit from continuing operations reported as {continuing_pat}.",
                )
            )
    else:
        issues.append(
            ValidationIssue(
                "continuing_operations_reconciliation", "INFO",
                "Skipped: PBT, tax, or a separately-reported continuing-operations "
                "profit figure not available in this filing.",
            )
        )

    # --- Check 2: bridge to the final reported profit for the period ---
    have_bridge_evidence = assoc_jv is not None or discontinued is not None
    if continuing_pat is not None and pat is not None:
        expected_total = continuing_pat + (assoc_jv or Decimal(0)) + (discontinued or Decimal(0))
        if not _within_tolerance(expected_total, pat):
            severity = "ERROR" if have_bridge_evidence else "WARNING"
            bridge_desc = (
                f"continuing-ops PAT ({continuing_pat}) + associates/JV share "
                f"({assoc_jv if assoc_jv is not None else 'not reported'}) + "
                f"discontinued ops after tax "
                f"({discontinued if discontinued is not None else 'not reported'})"
            )
            note = (
                "" if have_bridge_evidence else
                " No associates/JV or discontinued-operations figures were reported "
                "separately, so this gap may be a legitimate item this check cannot "
                "see (e.g. non-controlling interest allocation) — verify against raw "
                "facts before treating as an error."
            )
            issues.append(
                ValidationIssue(
                    "total_profit_reconciliation",
                    severity,
                    f"{bridge_desc} = {expected_total}, but reported profit for "
                    f"period is {pat}.{note}",
                )
            )
    else:
        issues.append(
            ValidationIssue(
                "total_profit_reconciliation", "INFO",
                "Skipped: continuing-operations PAT or reported profit for period not available",
            )
        )

    return issues


def validate_balance_sheet(stmt: dict) -> list:
    """
    Two independent checks:

    1. "balance_sheet_reconciliation" (existing): Total Assets vs the
       combined Equity+Liabilities balancing total (total_liabilities,
       which maps ONLY to the "EquityAndLiabilities" concept — see
       concept_map.py's comment on why bare "Liabilities" was removed as
       an alias for this field). This is the loosest, most-often-available
       check (just needs the two grand totals).

    2. "balance_sheet_three_way_reconciliation" (new): the literal
       Assets = Equity + Liabilities identity requested, using the
       supplementary total_equity and total_liabilities_excl_equity
       fields (mapped from the "Equity" and bare "Liabilities" concepts
       respectively — see BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS). Only
       runs when BOTH of those are available; ERROR-level since, when
       available, there's no legitimate accounting reason for this not
       to reconcile (unlike the income-statement profit bridge, there's
       no NCI-style allocation ambiguity here).
    """
    issues = []
    total_assets = stmt.get("total_assets")
    total_liabilities = stmt.get("total_liabilities")  # EquityAndLiabilities combined total only
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

    total_equity = stmt.get("total_equity")
    total_liabilities_excl_equity = stmt.get("total_liabilities_excl_equity")
    if total_assets is not None and total_equity is not None and total_liabilities_excl_equity is not None:
        expected = total_equity + total_liabilities_excl_equity
        if not _within_tolerance(total_assets, expected):
            issues.append(
                ValidationIssue(
                    "balance_sheet_three_way_reconciliation",
                    "ERROR",
                    f"Equity ({total_equity}) + Liabilities ({total_liabilities_excl_equity}) = "
                    f"{expected}, but Total Assets reported as {total_assets}",
                )
            )
    else:
        issues.append(
            ValidationIssue(
                "balance_sheet_three_way_reconciliation", "INFO",
                "Skipped: total_assets, total_equity, or total_liabilities_excl_equity not available "
                "(filing may not separately disclose bare Equity/Liabilities subtotals)",
            )
        )

    return issues


def validate_cashflow(stmt: dict) -> list:
    """
    Two independent checks:

    1. "cashflow_reconciliation" (existing): Opening + Net Change = Closing.

    2. "cashflow_components_reconciliation" (new, per explicit request):
       CFO + CFI + CFF (+ FX, when disclosed) should reconcile with the
       reported net change in cash. When fx_effect_on_cash IS available,
       a mismatch is a real ERROR (full evidence, no missing variable to
       excuse it). When FX is NOT disclosed, a mismatch is only a WARNING
       — an undisclosed FX effect on foreign-currency cash balances is a
       completely ordinary, legitimate reason for CFO+CFI+CFF alone not
       to exactly equal the net change.
    """
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

    cfo = stmt.get("cfo")
    cfi = stmt.get("cfi")
    cff = stmt.get("cff")
    fx = stmt.get("fx_effect_on_cash")
    if cfo is not None and cfi is not None and cff is not None and net_change is not None:
        components_total = cfo + cfi + cff + (fx or Decimal(0))
        if not _within_tolerance(components_total, net_change):
            severity = "ERROR" if fx is not None else "WARNING"
            fx_desc = f"+ FX effect ({fx})" if fx is not None else "(no FX effect disclosed)"
            note = (
                "" if fx is not None else
                " An undisclosed foreign-exchange effect on cash balances is a legitimate, "
                "ordinary explanation for this gap — verify against raw facts before treating "
                "as an error."
            )
            issues.append(
                ValidationIssue(
                    "cashflow_components_reconciliation",
                    severity,
                    f"CFO ({cfo}) + CFI ({cfi}) + CFF ({cff}) {fx_desc} = {components_total}, "
                    f"but reported net change in cash is {net_change}.{note}",
                )
            )
    else:
        issues.append(
            ValidationIssue(
                "cashflow_components_reconciliation", "INFO",
                "Skipped: cfo, cfi, cff, or net_change_in_cash not all available",
            )
        )

    return issues


def validate_ratio_plausibility(ratios: list) -> list:
    """
    ISSUE 6: NSE's presentation HTML for RELIANCE's real Q1 FY27 filing
    displays Debt Equity Ratio / Debt Service Coverage Ratio / Interest
    Service Coverage Ratio at roughly 100x the value tagged in the
    machine-readable XBRL (e.g. XBRL: DebtEquityRatio=0.004 vs HTML
    display: "0.400"). This looks like a known pattern in Indian-filer
    XBRL submissions where a ratio is tagged as if it were a fraction-of-a-
    percent rather than a plain ratio.

    Per explicit instruction, this function does NOT correct, rescale, or
    otherwise silently transform the value — it only raises an auditable
    INFO-level flag so the discrepancy is visible to a human reviewer,
    with the raw value and source concept intact. Do not extend this to
    apply any multiplier without first confirming the actual XBRL taxonomy
    semantics (e.g. a documented decimals/scale convention specific to
    these ratio concepts) — none was found in this filing's own facts
    (no `scale` attribute was present on any of the three ratio facts).
    """
    issues = []
    # Empirically observed range for a plausible "pure" financial ratio of
    # this kind; a value far below this AND a known name match is treated
    # as "worth a human look", not as proof of an error.
    suspiciously_small_names = {
        "Debt Equity Ratio",
        "Debt Service Coverage Ratio",
        "Interest Service Coverage Ratio",
    }
    for ratio in ratios:
        name = ratio.get("ratio_name")
        value = ratio.get("value")
        if name in suspiciously_small_names and value is not None and Decimal(0) < value < Decimal("0.05"):
            issues.append(
                ValidationIssue(
                    "ratio_scale_plausibility",
                    "INFO",
                    f"{name} = {value} (source concept: {ratio.get('source_concept')}, "
                    f"unit: {ratio.get('unit')}) as tagged in the raw XBRL. This is "
                    f"noticeably smaller than the value NSE's own presentation HTML "
                    f"shows for the same filing, consistent with a known Indian-filer "
                    f"XBRL tagging inconsistency for this ratio family. The value is "
                    f"stored exactly as declared in the source — no scaling has been "
                    f"applied. Verify against the filing's presentation document "
                    f"before using this figure.",
                    subject=name,
                )
            )
    return issues


def overall_status(issues: list) -> str:
    if any(i.severity == "ERROR" for i in issues):
        return "FAILED" if all(i.severity == "ERROR" for i in issues) else "PARTIAL"
    # WARNING means the filing may have been ingested and normalized, but at
    # least one correctness/coverage condition remains unresolved. Do not call
    # this VALIDATED merely because no arithmetic ERROR was detected.
    if any(i.severity == "WARNING" for i in issues):
        return "PARTIAL"
    return "VALIDATED"
