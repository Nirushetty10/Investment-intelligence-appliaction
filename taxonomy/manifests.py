"""
taxonomy/manifests.py

HONESTY NOTE (read this before trusting anything else in this module):
The project now has several official taxonomy packages supplied alongside
the source snapshot, including 2025 sector XSD/linkbase packages and a 2026
REIT/InvIT package. However, this module's
IND_AS_OTHER_THAN_BANKS_CONFIRMED_CONCEPTS set below is still extracted
from ONE real RELIANCE Q1 FY27 filing and is NOT a complete declaration list
from those XSDs. Runtime import of all packages/concepts/relationships is a
separate step and is not complete yet. These concepts are confirmed to occur
in that filing; aliases not included here remain unverified until checked
against the exact applicable taxonomy version.
"""

# Identity of the one taxonomy version this project has direct evidence
# for. Extracted from the real fixture's own declarations:
#   <!--IFIndAs V2.1 (26-06-2026)-->
#   <link:schemaRef xlink:href="in-capmkt-ent-2026-01-31.xsd" .../>
#   xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"
IND_AS_OTHER_THAN_BANKS = {
    "taxonomy_id": "ind_as_other_than_banks",
    "label": "SEBI Integrated Filing — Ind AS (Other than Banks)",
    "version": "2026-01-31",
    "informal_version_tag": "IFIndAs V2.1 (26-06-2026)",
    "namespace": "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt",
    "schema_ref_pattern": "in-capmkt-ent-*.xsd",
    "standard_namespace_prefixes": ("in-capmkt",),
}

# Every concept local-name CONFIRMED present (by direct extraction) in the
# one real filing on disk. This is a SAMPLE of the real taxonomy's
# elements (whatever this one filer's one quarter happened to use), NOT
# the complete element declaration list from the XSD — the real taxonomy
# almost certainly defines many more elements than any single filing uses
# (e.g. full balance-sheet and cash-flow line items, which this
# particular quarterly result does not report at all).
#
# Used for: (a) confirming a concept_map.py alias IS real wherever
# overlap exists, (b) the taxonomy-provenance classifier's "company
# extension" detection (comparing a fact's actual namespace against
# `standard_namespace_prefixes` above — this check does NOT depend on
# this concept list and works for any concept, confirmed or not).
IND_AS_OTHER_THAN_BANKS_CONFIRMED_CONCEPTS = frozenset({
    "AmountOfItemThatWillBeReclassifiedToProfitAndLoss",
    "AmountOfItemThatWillNotBeReclassifiedToProfitAndLoss",
    "AuditorsFirmName",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "BasicEarningsLossPerShareFromContinuingOperations",
    "BasicEarningsLossPerShareFromDiscontinuedOperations",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade",
    "ClassOfSecurity",
    "ComprehensiveIncomeForThePeriod",
    "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParent",
    "ComprehensiveIncomeForThePeriodAttributableToOwnersOfParentNonControllingInterests",
    "CostOfMaterialsConsumed",
    "CurrentTax",
    "DateOfBoardMeetingWhenFinancialResultsWereApproved",
    "DateOfEndOfBoardMeeting",
    "DateOfEndOfFinancialYear",
    "DateOfEndOfReportingPeriod",
    "DateOfStartOfBoardMeeting",
    "DateOfStartOfFinancialYear",
    "DateOfStartOfReportingPeriod",
    "DateOnWhichPriorIntimationOfTheMeetingForConsideringFinancialResultsWasInformedToTheExchange",
    "DebtEquityRatio",
    "DebtServiceCoverageRatio",
    "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification",
    "DeclarationPursuantToClauseDOfSubRegulation3OfRegulation33OfSEBILODRRegulation2015",
    "DeferredTax",
    "DepreciationDepletionAndAmortisationExpense",
    "DescriptionOfItemThatWillBeReclassifiedToProfitAndLoss",
    "DescriptionOfItemThatWillNotBeReclassifiedToProfitAndLoss",
    "DescriptionOfOtherExpenses",
    "DescriptionOfPresentationCurrency",
    "DescriptionOfReportableSegment",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
    "DilutedEarningsLossPerShareFromContinuingOperations",
    "DilutedEarningsLossPerShareFromDiscontinuedOperations",
    "DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock",
    "DisclosureOfNotesOnSegmentsExplanatoryTextBlock",
    "EmployeeBenefitExpense",
    "EndTimeOfBoardMeeting",
    "ExceptionalItemsBeforeTax",
    "Expenses",
    "FaceValueOfEquityShareCapital",
    "FinanceCosts",
    "ISIN",
    "Income",
    "IncomeTaxRelatingToItemsThatWillBeReclassifiedToProfitOrLoss",
    "IncomeTaxRelatingToItemsThatWillNotBeReclassifiedToProfitOrLoss",
    "InterSegmentRevenue",
    "InterestServiceCoverageRatio",
    "IsCompanyReportingMultisegmentOrSingleSegment",
    "LevelOfRounding",
    "MSEISymbol",
    "NameOfTheCompany",
    "NatureOfReportStandaloneConsolidated",
    "NetMovementInRegulatoryDeferralAccountBalancesRelatedToProfitOrLossAndTheRelatedDeferredTaxMovement",
    "NetSegmentAssets",
    "NetSegmentLiabilities",
    "OtherComprehensiveIncome",
    "OtherComprehensiveIncomeNetOfTaxes",
    "OtherExpenses",
    "OtherIncome",
    "OtherUnallocableExpenditureNetOffUnAllocableIncome",
    "PaidUpValueOfEquityShareCapital",
    "ProfitBeforeExceptionalItemsAndTax",
    "ProfitBeforeTax",
    "ProfitLossForPeriod",
    "ProfitLossForPeriodFromContinuingOperations",
    "ProfitLossFromDiscontinuedOperationsAfterTax",
    "ProfitLossFromDiscontinuedOperationsBeforeTax",
    "ProfitOrLossAttributableToNonControllingInterests",
    "ProfitOrLossAttributableToOwnersOfParent",
    "PurchasesOfStockInTrade",
    "ReportingQuarter",
    "RevenueFromOperations",
    "ScripCode",
    "SegmentAssets",
    "SegmentFinanceCosts",
    "SegmentLiabilities",
    "SegmentProfitBeforeTax",
    "SegmentProfitLossBeforeTaxAndFinanceCosts",
    "SegmentRevenue",
    "SegmentRevenueFromOperations",
    "ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod",
    "StartTimeOfBoardMeeting",
    "Symbol",
    "TaxExpense",
    "TaxExpenseOfDiscontinuedOperations",
    "TypeOfCompany",
    "TypeOfReportingPeriod",
    "UnAllocableAssets",
    "UnAllocableLiabilities",
    "ValidityDateOfCertificate",
    "WhetherResultsAreAuditedOrUnaudited",
    "WhetherResultsAreAuditedOrUnauditedForImpactOfAuditQualification",
    "WhetherTheFirmHoldsAValidPeerReviewCertificateIssuedByPeerReviewBoardOfICAI",
})

# Concepts we have ACTIVELY disproved — not "absent from our one sample",
# but specifically shown to be wrong for a field they were once mapped to
# (real data checked, zero matching facts found, and the correct real
# concept name identified and substituted). A concept landing back in a
# concept_map.py alias list after appearing here would be a real
# regression — see tests/test_taxonomy_validation.py.
KNOWN_INVALID_CONCEPTS = {
    "PaidUpEquityShareCapital": (
        "Previously aliased for equity_share_capital; checked against the "
        "real RELIANCE Q1 FY27 filing and found to match ZERO facts. The "
        "filing uses PaidUpValueOfEquityShareCapital instead (confirmed "
        "above). Removed from concept_map.py — do not re-add without new "
        "evidence."
    ),
}
