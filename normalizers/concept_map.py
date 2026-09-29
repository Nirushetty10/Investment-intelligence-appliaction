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
    "other_comprehensive_income": ["OtherComprehensiveIncome"],
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
}

# profit_after_tax is deliberately mapped separately: some filings report it
# as ProfitLossForPeriod (continuing+discontinued combined), others as a
# distinct "Profit/Loss for the period from continuing operations" figure.
# Do not assume these are interchangeable; only fill the field that was
# actually reported under its own concept.
INCOME_STATEMENT_CONCEPT_MAP_PAT_ALIASES = [
    "ProfitLossForPeriod",
    "NetProfitLossForThePeriodFromContinuingOperations",
]

BALANCE_SHEET_CONCEPT_MAP = {
    "equity_share_capital": ["PaidUpEquityShareCapital", "EquityShareCapital"],
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

METADATA_CONCEPTS = {
    "statement_type": "NatureOfReportStandaloneOrConsolidated",
    "reporting_quarter": "ReportingQuarter",
    "audited_status": "WhetherResultsAreAuditedOrUnaudited",
    "rounding_level": "LevelOfRoundingUsedInFinancialResults",
}
