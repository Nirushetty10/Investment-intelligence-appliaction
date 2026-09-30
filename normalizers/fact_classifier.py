"""
normalizers/fact_classifier.py

Classifies every raw XBRL fact's concept into exactly one of three buckets,
so mapping-coverage reporting isn't misleading (a flat "108 unmapped facts"
mixes admin metadata, disclosure text, and segment breakdowns in with
genuinely-important financial concepts that still need mapping):

    MAPPED               — resolved into a normalized field somewhere
                            (income statement, balance sheet, cashflow,
                            ratios, reconciliation bridge, or the
                            supplementary financial concepts).
    INTENTIONALLY_UNMAPPED — filer identity, board-meeting/admin dates,
                            auditor/compliance declarations, disclosure
                            text blocks, or segment/dimensional facts
                            (segment data belongs in its own
                            segment_financials table per the original
                            spec §18 — out of scope for this normalizer,
                            and explicitly not the same thing as "unknown").
    UNMAPPED_FINANCIAL    — a genuine monetary/financial concept that is
                            not yet mapped anywhere. This is the bucket
                            that should shrink toward zero over time, and
                            the ONLY one that should worry a reviewer.

Classification is closed-world but fails SAFE: any concept not explicitly
listed as MAPPED (via the concept maps) or INTENTIONALLY_UNMAPPED (via the
category table below) defaults to UNMAPPED_FINANCIAL rather than being
silently ignored — an unrecognized concept must surface for review, never
disappear into a bucket that looks fine.
"""
from dataclasses import dataclass, field
from typing import Optional

from normalizers.concept_map import KNOWN_UNMAPPED_FINANCIAL_CONCEPTS

MAPPED = "MAPPED"
INTENTIONALLY_UNMAPPED = "INTENTIONALLY_UNMAPPED"
UNMAPPED_FINANCIAL = "UNMAPPED_FINANCIAL"

# Concept local-name -> category label. Every entry here was confirmed, by
# name, against the real RELIANCE Q1 FY27 filing (tests/fixtures/
# reliance_2026q1_consolidated.xbrl.xml) — this is not a speculative
# taxonomy dump, it's exactly what showed up as "unmapped" before this fix
# and was individually reviewed.
INTENTIONALLY_UNMAPPED_CONCEPTS = {
    # --- Filer / instrument identity (not a financial value) ---
    "NameOfTheCompany": "filer_identity",
    "Symbol": "filer_identity",
    "ISIN": "filer_identity",
    "ScripCode": "filer_identity",
    "MSEISymbol": "filer_identity",
    "ClassOfSecurity": "filer_identity",
    "TypeOfCompany": "filer_identity",
    "TypeOfReportingPeriod": "filer_identity",
    "DescriptionOfPresentationCurrency": "filer_identity",
    "IsCompanyReportingMultisegmentOrSingleSegment": "filer_identity",

    # --- Board meeting / filing administration dates & times ---
    "DateOfBoardMeetingWhenFinancialResultsWereApproved": "board_meeting_admin",
    "DateOfEndOfBoardMeeting": "board_meeting_admin",
    "DateOfStartOfBoardMeeting": "board_meeting_admin",
    "StartTimeOfBoardMeeting": "board_meeting_admin",
    "EndTimeOfBoardMeeting": "board_meeting_admin",
    "DateOnWhichPriorIntimationOfTheMeetingForConsideringFinancialResultsWasInformedToTheExchange": "board_meeting_admin",
    "DateOfEndOfFinancialYear": "board_meeting_admin",
    "DateOfStartOfFinancialYear": "board_meeting_admin",
    "DateOfEndOfReportingPeriod": "board_meeting_admin",
    "DateOfStartOfReportingPeriod": "board_meeting_admin",

    # --- Auditor / regulatory compliance declarations (not financial values) ---
    "AuditorsFirmName": "auditor_compliance",
    "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification": "auditor_compliance",
    "DeclarationPursuantToClauseDOfSubRegulation3OfRegulation33OfSEBILODRRegulation2015": "auditor_compliance",
    "WhetherResultsAreAuditedOrUnauditedForImpactOfAuditQualification": "auditor_compliance",
    "WhetherTheFirmHoldsAValidPeerReviewCertificateIssuedByPeerReviewBoardOfICAI": "auditor_compliance",
    "ValidityDateOfCertificate": "auditor_compliance",

    # --- Free-text disclosure blocks (no numeric value to normalize) ---
    "DisclosureOfNotesOnFinancialResultsExplanatoryTextBlock": "disclosure_text",
    "DisclosureOfNotesOnSegmentsExplanatoryTextBlock": "disclosure_text",
    "DescriptionOfOtherExpenses": "disclosure_text",
    "DescriptionOfReportableSegment": "disclosure_text",
    "DescriptionOfItemThatWillBeReclassifiedToProfitAndLoss": "disclosure_text",
    "DescriptionOfItemThatWillNotBeReclassifiedToProfitAndLoss": "disclosure_text",

    # --- Segment / dimensional facts: belong in segment_financials (spec
    # §18), deliberately never mapped into company-wide statements here ---
    "SegmentRevenue": "segment_dimensional",
    "SegmentRevenueFromOperations": "segment_dimensional",
    "SegmentAssets": "segment_dimensional",
    "SegmentLiabilities": "segment_dimensional",
    "SegmentFinanceCosts": "segment_dimensional",
    "SegmentProfitBeforeTax": "segment_dimensional",
    "SegmentProfitLossBeforeTaxAndFinanceCosts": "segment_dimensional",
    "NetSegmentAssets": "segment_dimensional",
    "NetSegmentLiabilities": "segment_dimensional",
    "InterSegmentRevenue": "segment_dimensional",
    "UnAllocableAssets": "segment_dimensional",
    "UnAllocableLiabilities": "segment_dimensional",
    "OtherUnallocableExpenditureNetOffUnAllocableIncome": "segment_dimensional",
}


@dataclass
class FactClassificationResult:
    classification: str            # MAPPED / INTENTIONALLY_UNMAPPED / UNMAPPED_FINANCIAL
    category: Optional[str] = None  # sub-reason, e.g. "segment_dimensional", "filer_identity"


def classify_concept(concept: str, mapped_concepts: set) -> FactClassificationResult:
    """
    `mapped_concepts` is the full set of concept local-names the normalizer
    actually resolves fields from (built once per normalize() call from
    every concept map, including supplementary/bridge/ratio/metadata maps
    — see normalizers/financial_normalizer.py build_mapped_concepts()).
    """
    if concept in mapped_concepts:
        return FactClassificationResult(MAPPED)

    if concept in INTENTIONALLY_UNMAPPED_CONCEPTS:
        return FactClassificationResult(
            INTENTIONALLY_UNMAPPED, INTENTIONALLY_UNMAPPED_CONCEPTS[concept]
        )

    # Fail SAFE: anything not explicitly accounted for above — whether a
    # concept we've reviewed and noted in KNOWN_UNMAPPED_FINANCIAL_CONCEPTS,
    # or one we have genuinely never seen before — defaults to
    # UNMAPPED_FINANCIAL. A brand-new, never-reviewed concept must surface
    # for review, not vanish into "intentionally ignored".
    reason = KNOWN_UNMAPPED_FINANCIAL_CONCEPTS.get(concept, "unrecognized_concept_needs_review")
    return FactClassificationResult(UNMAPPED_FINANCIAL, reason)


@dataclass
class ClassificationTally:
    mapped_count: int = 0
    intentionally_unmapped_count: int = 0
    unmapped_financial_count: int = 0
    intentionally_unmapped_by_category: dict = field(default_factory=dict)   # category -> count
    unmapped_financial_concepts: dict = field(default_factory=dict)          # concept -> count

    @property
    def total(self) -> int:
        return self.mapped_count + self.intentionally_unmapped_count + self.unmapped_financial_count

    # Backward-compatible alias: pre-classification code (and the existing
    # 26 tests) refers to a single flat "unmapped_fact_count", meaning
    # "not mapped, for any reason". Sum of the two non-MAPPED buckets
    # preserves that exact number.
    @property
    def unmapped_fact_count(self) -> int:
        return self.intentionally_unmapped_count + self.unmapped_financial_count


def classify_facts(facts, mapped_concepts: set) -> ClassificationTally:
    tally = ClassificationTally()
    for fact in facts:
        result = classify_concept(fact.concept, mapped_concepts)
        if result.classification == MAPPED:
            tally.mapped_count += 1
        elif result.classification == INTENTIONALLY_UNMAPPED:
            tally.intentionally_unmapped_count += 1
            tally.intentionally_unmapped_by_category[result.category] = (
                tally.intentionally_unmapped_by_category.get(result.category, 0) + 1
            )
        else:
            tally.unmapped_financial_count += 1
            tally.unmapped_financial_concepts[fact.concept] = (
                tally.unmapped_financial_concepts.get(fact.concept, 0) + 1
            )
    return tally
