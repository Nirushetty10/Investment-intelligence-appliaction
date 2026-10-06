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

# Financial (monetary) concepts that are genuinely NOT mapped to any
# normalized field AND are not a known detail/component of something we
# already capture — these are the ones that should actually worry a
# reviewer. Kept deliberately small: "we don't have a field for this and
# it isn't obviously a sub-item of a total we do capture" is a real gap,
# not busywork.
KNOWN_UNMAPPED_FINANCIAL_CONCEPTS = {
    "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement":
        "Niche regulatory-deferral disclosure (rare, utilities-sector) that can affect reported "
        "profit — no normalized field for Phase 1, genuinely worth a human look if it appears "
        "with a non-zero value.",
}

# Financial (monetary) concepts that ARE understood — specifically known
# to be a detail-level breakdown/component of a total already captured by
# a mapped field above — but are deliberately NOT given their own
# normalized field. These are classified INTENTIONALLY_UNMAPPED with
# category "detail_component" (see normalizers/fact_classifier.py), NOT
# UNMAPPED_FINANCIAL: the information is not lost (every raw fact is
# still stored in xbrl_facts regardless of classification — see spec
# §26), it's just correctly understood as decomposition of a figure we
# already have directly from its own total concept, not a new fact.
#
# Every entry documents WHICH total it's a component of, so this reads as
# reviewed reasoning, not a dumping ground.
DETAIL_COMPONENT_CONCEPTS = {
    # --- OCI reclassification detail: components of other_comprehensive_income ---
    "AmountOfItemThatWillBeReclassifiedToProfitAndLoss":
        "OCI reclassification component — total already captured via other_comprehensive_income",
    "AmountOfItemThatWillNotBeReclassifiedToProfitAndLoss":
        "OCI reclassification component — total already captured via other_comprehensive_income",
    "IncomeTaxRelatingToItemsThatWillBeReclassifiedToProfitOrLoss":
        "Tax effect on an OCI reclassification component — OCI total already captured net of tax "
        "via other_comprehensive_income",
    "IncomeTaxRelatingToItemsThatWillNotBeReclassifiedToProfitOrLoss":
        "Tax effect on an OCI reclassification component — OCI total already captured net of tax "
        "via other_comprehensive_income",

    # --- Cash-flow investing/financing detail: components of cfi/cff totals ---
    "InterestReceivedClassifiedAsInvestingActivities":
        "Detail line within investing activities — cfi total already captured directly "
        "(also separately available as cashflow supplementary field interest_received)",
    "DividendsReceivedClassifiedAsInvestingActivities":
        "Detail line within investing activities — cfi total already captured directly",
    "PaymentsToAcquireIntangibleAssets":
        "Detail line within investing activities — cfi total already captured directly",
    "ProceedsFromSaleOfIntangibleAssets":
        "Detail line within investing activities — cfi total already captured directly",
    "PaymentOfFinanceLeaseLiabilities":
        "Detail line within financing activities — cff total already captured directly",
}

#
# ISSUE 5 (mapping-coverage completeness): this map was previously only
# ~9 fields deep, each with a single candidate concept name, several of
# them guessed rather than confirmed. This is expanded to cover every
# balance_sheet schema column with multiple plausible candidate names
# drawn from the standard Ind-AS / SEBI "in-capmkt" XBRL taxonomy
# (the same taxonomy family the real RELIANCE Q1 filing uses — that
# filing's own concepts, e.g. RevenueFromOperations/ProfitBeforeTax/
# TaxExpense, follow this exact naming convention).
#
# HONESTY NOTE: unlike the income-statement map (built and verified
# against the real RELIANCE Q1 FY27 XBRL) and the equity_share_capital
# fix above (also verified against real data), these balance-sheet/
# cashflow aliases are NOT yet verified against a real filing that
# actually contains a full balance sheet — I do not have that file.
# They are included because the alternative (leaving them unmapped) is
# worse: when a real annual filing's XBRL does carry a concept matching
# one of these candidates, it will now correctly resolve; if none match,
# the field simply stays None (no harm done) and the unmatched concept
# surfaces as UNMAPPED_FINANCIAL for review, per the classifier's
# fail-safe design — nothing is silently hidden either way. Treat this
# as "ready to verify", not "confirmed", until run against a real annual
# filing with balance-sheet data.
#
BALANCE_SHEET_CONCEPT_MAP = {
    "equity_share_capital": [
        "PaidUpValueOfEquityShareCapital", "PaidUpEquityShareCapital",
        "EquityShareCapital", "IssuedCapital",
    ],
    "other_equity": ["OtherEquity"],

    "long_term_borrowings": [
        "BorrowingsNoncurrent", "LongTermBorrowings", "NoncurrentBorrowings", "LongtermBorrowings",
        "LoansNoncurrent", "LoansNonCurrent",
    ],
    "other_long_term_liabilities": [
        "OtherNoncurrentLiabilities", "OtherNonCurrentLiabilities", "OtherLongTermLiabilities",
    ],
    "deferred_tax_liabilities": [
        "DeferredTaxLiabilitiesNet", "DeferredTaxLiabilities",
    ],
    "long_term_provisions": [
        "NoncurrentProvisions", "LongTermProvisions", "ProvisionsNoncurrent",
    ],

    "short_term_borrowings": [
        "BorrowingsCurrent", "ShortTermBorrowings", "CurrentBorrowings", "ShorttermBorrowings",
        "LoansCurrent",
    ],
    "trade_payables": [
        "TradePayablesCurrent", "TradePayables",
    ],
    "other_current_liabilities": [
        "OtherCurrentLiabilities", "OtherCurrentFinancialLiabilities",
    ],
    "short_term_provisions": [
        "CurrentProvisions", "ShortTermProvisions", "ProvisionsCurrent",
    ],

    # IMPORTANT: "Liabilities" (bare) was REMOVED as a candidate here.
    # "EquityAndLiabilities" is the true balance-sheet-balancing total
    # (Equity + Liabilities combined, which by definition always equals
    # Assets); "Liabilities" alone is a DIFFERENT, smaller figure
    # (Liabilities only, excluding Equity). Treating them as
    # interchangeable aliases for one field was a real bug: if a filer
    # happened to tag "Liabilities" instead of "EquityAndLiabilities",
    # the Assets-vs-total_liabilities reconciliation check would compare
    # Assets against a figure that's supposed to be smaller by exactly
    # Equity, producing a false reconciliation ERROR. "Liabilities" (bare)
    # is now its own supplementary field — see
    # BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS below — used for the explicit
    # three-way Assets = Equity + Liabilities check instead.
    "total_liabilities": ["EquityAndLiabilities"],

    "property_plant_equipment": ["PropertyPlantAndEquipment"],
    "capital_work_in_progress": ["CapitalWorkInProgress"],
    "goodwill": ["Goodwill"],
    "intangible_assets": [
        "OtherIntangibleAssets", "IntangibleAssetsOtherThanGoodwill", "IntangibleAssets",
    ],
    "non_current_investments": [
        "NoncurrentInvestments", "NonCurrentInvestments",
    ],
    "deferred_tax_assets": [
        "DeferredTaxAssetsNet", "DeferredTaxAssets",
    ],
    "other_non_current_assets": [
        "OtherNoncurrentAssets", "OtherNonCurrentAssets",
    ],

    "current_investments": ["CurrentInvestments"],
    "inventories": ["Inventories"],
    "trade_receivables": [
        "TradeReceivablesCurrent", "TradeReceivables",
    ],
    "cash_and_cash_equivalents": ["CashAndCashEquivalents"],
    "bank_balances": [
        "BankBalancesOtherThanCashAndCashEquivalents", "OtherBankBalances",
    ],
    "other_current_assets": ["OtherCurrentAssets"],

    "total_assets": ["Assets"],
}

# Balance-sheet SUBTOTAL concepts (CurrentAssets, NoncurrentAssets,
# CurrentLiabilities, NoncurrentLiabilities, Equity, bare Liabilities).
# These are genuinely useful, genuinely financial figures — but they are
# subtotals/components of the line items already captured individually
# above, not elementary new facts, so (per the review requested) they get
# mapped here as supplementary fields (same non-schema-persisted
# mechanism as INCOME_STATEMENT_SUPPLEMENTARY_CONCEPTS: used for
# reconciliation/traceability, not written to new DB columns) rather than
# either being left UNMAPPED_FINANCIAL or given full dedicated schema
# columns neither requested nor needed for Phase 1.
#
# "total_equity" and "total_liabilities_excl_equity" specifically exist
# to support the real three-way balance-sheet reconciliation
# (Assets = Equity + Liabilities) — see validators/financial_validator.py
# validate_balance_sheet. Do NOT reuse "total_liabilities" (which maps to
# EquityAndLiabilities, the combined balancing total) for this purpose.
BALANCE_SHEET_SUPPLEMENTARY_CONCEPTS = {
    "total_equity": ["Equity"],
    "total_liabilities_excl_equity": ["Liabilities"],
    "total_current_assets": ["CurrentAssets"],
    "total_non_current_assets": ["NoncurrentAssets", "NonCurrentAssets"],
    "total_current_liabilities": ["CurrentLiabilities"],
    "total_non_current_liabilities": ["NoncurrentLiabilities", "NonCurrentLiabilities"],
}

CASHFLOW_CONCEPT_MAP = {
    "cfo": [
        "CashFlowsFromUsedInOperatingActivities",
        "NetCashFlowsFromUsedInOperatingActivities",
    ],
    "cfi": [
        "CashFlowsFromUsedInInvestingActivities",
        "NetCashFlowsFromUsedInInvestingActivities",
    ],
    "cff": [
        "CashFlowsFromUsedInFinancingActivities",
        "NetCashFlowsFromUsedInFinancingActivities",
    ],
    "net_change_in_cash": [
        "IncreaseDecreaseInCashAndCashEquivalents",
        "NetIncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges",
    ],
    "opening_cash_balance": [
        "CashAndCashEquivalentsAtBeginningOfPeriod",
        "CashAndCashEquivalentCashFlowStatementAtBeginningOfPeriod",
    ],
    "closing_cash_balance": [
        "CashAndCashEquivalentsAtEndOfPeriod",
        "CashAndCashEquivalentCashFlowStatementAtEndOfPeriod",
    ],
    "depreciation_addback": [
        "AdjustmentForDepreciationAndAmortisationExpense",
        "AdjustmentsForDepreciationAndAmortisationExpense",
    ],
    "interest_paid": [
        "InterestPaidClassifiedAsOperatingActivities",
        "InterestPaidClassifiedAsFinancingActivities",
        "FinanceCostsPaid",
    ],
    "tax_paid": [
        "IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
        "IncomeTaxesPaid",
    ],
    "purchase_of_ppe": [
        "PurchaseOfPropertyPlantAndEquipment",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ],
    "sale_of_ppe": [
        "ProceedsFromSalesOfPropertyPlantAndEquipment",
        "ProceedsFromSaleOfPropertyPlantAndEquipment",
    ],
    "investments_net": [
        # Genuinely a net figure in some filings (one concept covering
        # both purchases and sales) but a few filers tag purchases and
        # proceeds as two separate concepts with no single "net" tag —
        # when that happens, only the first of these two to resolve wins
        # and the field will UNDER-represent the true net investing flow
        # (document, don't invent, a netting calculation we weren't given).
        "PaymentsForInvestments", "PurchaseOfInvestments",
        "ProceedsFromSaleOfInvestments",
    ],
    "borrowings": [
        "ProceedsFromBorrowings",
        "ProceedsFromCurrentBorrowings", "ProceedsFromNoncurrentBorrowings",
    ],
    "repayments": [
        "RepaymentsOfBorrowings",
        "RepaymentsOfCurrentBorrowings", "RepaymentsOfNoncurrentBorrowings",
    ],
    "dividends_paid": [
        "DividendsPaidClassifiedAsFinancingActivities",
        "DividendsPaid",
    ],
}

# Cash-flow concepts that are genuinely useful but don't have a dedicated
# cashflow_statement schema column (same non-persisted-extra-key
# mechanism used throughout this file). fx_effect_on_cash specifically
# exists to support the CFO + CFI + CFF + FX = net change in cash
# reconciliation (see validators/financial_validator.py validate_cashflow)
# — without it, a real FX-driven gap between CFO+CFI+CFF and the reported
# net change in cash would look like an unexplained discrepancy.
CASHFLOW_SUPPLEMENTARY_CONCEPTS = {
    "fx_effect_on_cash": [
        "EffectOfExchangeRateChangesOnCashAndCashEquivalents",
        "EffectOfExchangeRateChangesOnCashAndCashEquivalentsBeforeDilutionOfNonControllingInterests",
    ],
    "interest_received": [
        "InterestReceivedClassifiedAsInvestingActivities",
        "InterestReceivedClassifiedAsOperatingActivities",
    ],
    "dividend_received": [
        "DividendsReceivedClassifiedAsInvestingActivities",
        "DividendReceivedClassifiedAsInvestingActivities",
    ],
    "proceeds_from_issue_of_equity": ["ProceedsFromIssueOfEquity"],
    "payments_for_share_buyback": ["PaymentsForShareBuyBack", "PaymentsForRepurchaseOfShares"],
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
