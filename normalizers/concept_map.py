"""
Maps known XBRL concept local-names to normalized schema fields.

This is intentionally DATA, not code branches, because NSE/Ind-AS taxonomy
concept names vary across companies, sectors (bank/NBFC taxonomies differ
from general commercial/industrial), and years (spec §40: don't assume one
format for every year). Adding support for a new taxonomy variant should
mean adding entries here, not editing normalizer logic.

Each normalized field maps to a LIST of acceptable concept local-names,
tried in order. The normalizer only accepts a match found as a single,
non-dimensioned fact for the exact target period (see
ParsedXbrlDocument.non_dimensioned_fact_for_period) — if none of the
candidate concepts resolves unambiguously, the field is left NULL rather
than guessed.

This is NOT an exhaustive taxonomy mapping — it covers the general
commercial & industrial (Ind-AS, non-financial-sector) taxonomy concepts
used in the Phase-1 fixture. Banking/NBFC/insurance taxonomies use
different concept sets entirely and need their own mapping table before
being run (do not silently reuse this map for those sectors).
"""

INCOME_STATEMENT_CONCEPT_MAP = {
    "revenue_from_operations": ["RevenueFromOperations"],
    "other_income": ["OtherIncome"],
    "total_income": ["TotalIncome", "Income"],
    "cost_of_materials": ["CostOfMaterialsConsumed"],
    "purchases": ["PurchasesOfStockInTrade"],
    "changes_in_inventory": [
        "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade"
    ],
    "employee_benefit_expense": ["EmployeeBenefitExpense"],
    "finance_cost": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense", "Depreciation"],
    "amortization": ["AmortisationExpense"],
    "other_expenses": ["OtherExpenses"],
    "total_expenses": ["Expenses", "TotalExpenses"],
    "operating_profit": ["ProfitBeforeExceptionalItemsAndTax"],
    "profit_before_tax": ["ProfitBeforeTax"],
    "current_tax": ["CurrentTax"],
    "deferred_tax": ["DeferredTax"],
    "total_tax": ["TaxExpense", "TotalTaxExpense"],
    "profit_for_period": ["ProfitLossForPeriod"],
    # Real RELIANCE filing tags the identical value under BOTH
    # "OtherComprehensiveIncome" and "OtherComprehensiveIncomeNetOfTaxes"
    # (same context, same amount) — two concept names for one disclosure.
    # Both are listed so either is recognized as understood; only the
    # first unambiguous match is used for the value itself.
    "other_comprehensive_income": ["OtherComprehensiveIncome", "OtherComprehensiveIncomeNetOfTaxes"],
    "total_comprehensive_income": ["ComprehensiveIncomeForThePeriod"],
    "basic_eps": [
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsPerShare",
    ],
    "diluted_eps": [
        "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "DilutedEarningsPerShare",
    ],
    "eps_face_value": ["FaceValueOfEquityShareCapital"],

    # profit_after_tax = profit/loss for the period from CONTINUING
    # operations only (i.e. PBT - Tax, before adding associates/JV share
    # or discontinued-operations results). This is deliberately distinct
    # from profit_for_period (the final, all-in figure) — see
    # RECONCILIATION_BRIDGE_CONCEPTS below and validators/financial_validator.py
    # for how the two are reconciled. Confirmed against the real NSE
    # in-capmkt taxonomy (RELIANCE Q1 FY27 Consolidated filing).
    "profit_after_tax": ["ProfitLossForPeriodFromContinuingOperations"],
}

# Bridge items between "profit after tax from continuing operations" and the
# final "profit for the period" line. Deliberately kept OUT of
# INCOME_STATEMENT_CONCEPT_MAP / the income_statement DB columns (no schema
# migration needed for Phase 1 — see repositories/financial_repository.py,
# which only persists the whitelisted income_statement columns and ignores
# extra dict keys) and used only by the validator to build an auditable
# reconciliation: continuing-ops PAT + associates/JV share + discontinued
# ops after tax = profit for period. A company that doesn't have one of
# these lines simply won't tag the concept at all — that is read as "not
# applicable" for the arithmetic check, never as an invented 0 written to
# storage (the field itself is never persisted).
RECONCILIATION_BRIDGE_CONCEPTS = {
    "share_of_profit_associates_jv": [
        "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod"
    ],
    "profit_discontinued_operations_after_tax": [
        "ProfitLossFromDiscontinuedOperationsAfterTax"
    ],
}

# Genuine financial concepts confirmed present in the real RELIANCE filing
# that don't have a dedicated income_statement DB column (no schema
# migration for Phase 1 — same non-persisted-extra-key mechanism as
# RECONCILIATION_BRIDGE_CONCEPTS: repositories/financial_repository.py
# only writes the whitelisted columns, so these ride along in the
# normalizer's dict purely for classification/traceability/future use).
# Every one of these was found, by name, in the actual filing — this is
# not a speculative taxonomy dump.
INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS = {
    "exceptional_items_before_tax": ["ExceptionalItemsBeforeTax"],
    "profit_discontinued_operations_before_tax": ["ProfitLossFromDiscontinuedOperationsBeforeTax"],
    "tax_expense_discontinued_operations": ["TaxExpenseOfDiscontinuedOperations"],
    "basic_eps_continuing_operations": ["BasicEarningsLossPerShareFromContinuingOperations"],
    "basic_eps_discontinued_operations": ["BasicEarningsLossPerShareFromDiscontinuedOperations"],
    "diluted_eps_continuing_operations": ["DilutedEarningsLossPerShareFromContinuingOperations"],
    "diluted_eps_discontinued_operations": ["DilutedEarningsLossPerShareFromDiscontinuedOperations"],
    # Profit/OCI split between the parent's owners and non-controlling
    # interests. Stored for traceability ONLY — NEVER added into the
    # profit_for_period reconciliation (NCI is an allocation of total
    # profit, not an extra profit component; see validators/financial_validator.py).
    "profit_attributable_owners_of_parent": ["ProfitOrLossAttributableToOwnersOfParent"],
    "profit_attributable_non_controlling_interests": ["ProfitOrLossAttributableToNonControllingInterests"],
    "comprehensive_income_attributable_owners_of_parent": [
        "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent"
    ],
    "comprehensive_income_attributable_nci": [
        "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests"
    ],
}

# Financial (monetary) concepts confirmed present in the real filing that
# are genuinely NOT mapped to any normalized field yet — either because
# they're detail-level breakdowns of a total we already capture elsewhere
# (OCI reclassification components) or a niche disclosure not worth a
# dedicated field for Phase 1. Listed explicitly (with a reason) so the
# classifier can label them UNMAPPED_FINANCIAL precisely instead of lumping
# them in with metadata — see normalizers/fact_classifier.py.
KNOWN_UNMAPPED_FINANCIAL_CONCEPTS = {
    "AmountOfItemThatWillBeReclassifiedToProfitAndLoss":
        "OCI reclassification component detail — total already captured via other_comprehensive_income",
    "AmountOfItemThatWillNotBeReclassifiedToProfitAndLoss":
        "OCI reclassification component detail — total already captured via other_comprehensive_income",
    "IncomeTaxRelatingToItemsThatWillBeReclassifiedToProfitOrLoss":
        "Tax effect on an OCI component — not yet broken out in normalized schema",
    "IncomeTaxRelatingToItemsThatWillNotBeReclassifiedToProfitOrLoss":
        "Tax effect on an OCI component — not yet broken out in normalized schema",
    "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement":
        "Niche regulatory-deferral disclosure — no normalized field for Phase 1",
}

BALANCE_SHEET_CONCEPT_MAP = {
    # Real taxonomy concept is PaidUpValueOfEquityShareCapital — confirmed
    # against the actual filing (PaidUpEquityShareCapital, the previous
    # sole candidate, matches zero facts in it). Both kept for safety
    # across filings/years.
    "equity_share_capital": [
        "PaidUpValueOfEquityShareCapital", "PaidUpEquityShareCapital", "EquityShareCapital"
    ],
    "other_equity": ["OtherEquity"],
    "long_term_borrowings": ["LongTermBorrowings"],
    "trade_payables": ["TradePayables"],
    "inventories": ["Inventories"],
    "trade_receivables": ["TradeReceivables"],
    "cash_and_cash_equivalents": ["CashAndCashEquivalents"],
    "total_assets": ["Assets"],
    "total_liabilities": ["EquityAndLiabilities", "Liabilities"],
}

CASHFLOW_CONCEPT_MAP = {
    "cfo": ["CashFlowsFromUsedInOperatingActivities"],
    "cfi": ["CashFlowsFromUsedInInvestingActivities"],
    "cff": ["CashFlowsFromUsedInFinancingActivities"],
    "net_change_in_cash": ["IncreaseDecreaseInCashAndCashEquivalents"],
    "opening_cash_balance": ["CashAndCashEquivalentsAtBeginningOfPeriod"],
    "closing_cash_balance": ["CashAndCashEquivalentsAtEndOfPeriod"],
}

RATIO_CONCEPTS = {
    "CurrentRatio": "Current Ratio",
    "DebtEquityRatio": "Debt Equity Ratio",
    "DebtServiceCoverageRatio": "Debt Service Coverage Ratio",
    "InterestServiceCoverageRatio": "Interest Service Coverage Ratio",
    "LongTermDebtToWorkingCapital": "Long-term Debt to Working Capital",
    "BadDebtsToAccountReceivableRatio": "Bad Debts to Accounts Receivable",
    "CurrentLiabilityRatio": "Current Liability Ratio",
    "TotalDebtsToTotalAssets": "Total Debts to Total Assets",
    "DebtorsTurnoverRatio": "Debtors Turnover",
    "InventoryTurnoverRatio": "Inventory Turnover",
    "OperatingMargin": "Operating Margin",
    "NetProfitMargin": "Net Profit Margin",
}


# Every value is a LIST of acceptable concept local-names, same convention
# as the statement maps above — resolved in order, first unambiguous
# non-dimensioned match wins. This matters because the reconstructed HTML
# fixture (tests/fixtures/reliance_2026q1_consolidated_ixbrl.html) and the
# real NSE XML (tests/fixtures/reliance_2026q1_consolidated.xbrl.xml) use
# two DIFFERENT concept names for the same disclosure:
#   HTML fixture : NatureOfReportStandaloneOrConsolidated  (with "Or")
#   Real NSE XML : NatureOfReportStandaloneConsolidated    (without "Or")
# Both are kept so neither regresses the other (see tests/test_ixbrl_parser.py
# ISSUE 3/4 notes).
METADATA_CONCEPTS = {
    "statement_type": [
        "NatureOfReportStandaloneConsolidated",
        "NatureOfReportStandaloneOrConsolidated",
    ],
    "reporting_quarter": ["ReportingQuarter"],
    "audited_status": ["WhetherResultsAreAuditedOrUnaudited"],
    "rounding_level": ["LevelOfRoundingUsedInFinancialResults", "LevelOfRounding"],
}
