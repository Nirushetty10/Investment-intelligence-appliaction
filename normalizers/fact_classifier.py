"""
normalizers/fact_classifier.py

Classifies every raw XBRL fact's concept into exactly one of three TOP-LEVEL
buckets, so mapping-coverage reporting isn't misleading (a flat "108
unmapped facts" mixes admin metadata, disclosure text, and segment
breakdowns in with genuinely-important financial concepts that still need
mapping):

    MAPPED               — resolved into a normalized field somewhere
                            (income statement, balance sheet, cashflow,
                            ratios, reconciliation bridge, or the
                            supplementary financial concepts).
    INTENTIONALLY_UNMAPPED — never a financial-value concern. Has FOUR
                            sub-categories (the `category` field on
                            FactClassificationResult), one of which is new:
                              - filer_identity / board_meeting_admin /
                                auditor_compliance / disclosure_text:
                                not a financial value at all.
                              - segment_dimensional: belongs in
                                segment_financials (spec §18), out of
                                scope for this normalizer.
                              - detail_component: IS a genuine financial
                                value, but a known sub-item of a total
                                we already capture directly from its own
                                concept — e.g. one of several
                                "AdjustmentsFor..." lines that reconcile
                                PBT to operating cash flow, when we
                                already have the CFO total itself. The
                                information is never lost (every raw fact
                                is stored in xbrl_facts regardless), it's
                                just correctly understood as decomposition
                                rather than a new fact.
    UNMAPPED_FINANCIAL    — a genuine monetary/financial concept that is
                            NOT a known detail/component of anything we
                            capture and has no normalized field. This is
                            the bucket that should shrink toward zero over
                            time, and the ONLY one that should worry a
                            reviewer — deliberately kept honest rather
                            than optimized to look small (see
                            DETAIL_COMPONENT_CONCEPTS's docstring in
                            concept_map.py: every entry documents WHICH
                            total it's a component of, so moving something
                            here is reviewed reasoning, not a shortcut).

Classification is closed-world but fails SAFE: any concept not explicitly
listed as MAPPED, INTENTIONALLY_UNMAPPED (exact-name table or the
detail-component pattern check), defaults to UNMAPPED_FINANCIAL rather
than being silently ignored — an unrecognized concept must surface for
review, never disappear into a bucket that looks fine.
"""
from dataclasses import dataclass, field
from typing import Optional

from normalizers.concept_map import DETAIL_COMPONENT_CONCEPTS, KNOWN_UNMAPPED_FINANCIAL_CONCEPTS
from taxonomy.catalog import get_concept_metadata, taxonomy_version_status

MAPPED = "MAPPED"
INTENTIONALLY_UNMAPPED = "INTENTIONALLY_UNMAPPED"
UNMAPPED_FINANCIAL = "UNMAPPED_FINANCIAL"

# These taxonomy value kinds are not numeric financial values. The taxonomy
# importer derives them from the element's declared XSD type, not the raw
# value or its local name. Unknown/custom types are deliberately NOT in this
# set: an unresolved type must remain reviewable rather than being hidden.
NON_FINANCIAL_VALUE_KINDS = {
    "string", "boolean", "date", "enumeration", "text_block", "identifier",
}

# Concept-name PREFIX patterns that reliably indicate a cash-flow
# reconciliation detail line (a component reconciling PBT to operating
# cash flow, or similar) in the standard Ind-AS XBRL taxonomy — used as a
# fallback for the many individual "AdjustmentsFor<SpecificItem>" concepts
# that cannot be exhaustively enumerated by exact name without the actual
# filing in hand (a real annual filing can easily carry 15-20 distinct
# such lines: depreciation, finance costs, interest income, dividend
# income, unrealised FX, provisions, fair-value gains/losses, impairment,
# working-capital movements for each major current-asset/liability
# category, etc.). This is pattern-matching on XBRL NAMING CONVENTION,
# not on any financial VALUE — a safe, principled basis for
# classification, unlike guessing at amounts. Every concept matched here
# is a genuine financial figure (never hidden — still in xbrl_facts) that
# is correctly understood as a CFO-total reconciliation component, since
# the CFO total itself is captured directly from its own concept
# (CashFlowsFromUsedInOperatingActivities) rather than being computed by
# summing these detail lines.
_DETAIL_COMPONENT_PREFIXES = (
    "AdjustmentsFor",
    "AdjustmentFor",
    "OtherAdjustments",
)

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


def classify_concept(
    concept: str,
    mapped_concepts: set,
    *,
    value_kind: Optional[str] = None,
    mapped_metadata_concepts: Optional[set] = None,
) -> FactClassificationResult:
    """
    `mapped_concepts` is the full set of concept local-names the normalizer
    actually resolves fields from (built once per normalize() call from
    every concept map, including supplementary/bridge/ratio/metadata maps
    — see normalizers/financial_normalizer.py build_mapped_concepts()).
    """
    if concept in INTENTIONALLY_UNMAPPED_CONCEPTS:
        return FactClassificationResult(
            INTENTIONALLY_UNMAPPED, INTENTIONALLY_UNMAPPED_CONCEPTS[concept]
        )

    # Explicit metadata concepts (e.g. reporting quarter / audit status) are
    # consumed by the metadata resolver, so a non-numeric value is expected.
    # For every other concept, an authoritative non-financial XSD type must
    # not count as a mapped financial value merely because an alias happens
    # to appear in a numeric concept map.
    if value_kind in NON_FINANCIAL_VALUE_KINDS:
        if concept in (mapped_metadata_concepts or set()):
            return FactClassificationResult(MAPPED)
        return FactClassificationResult(
            INTENTIONALLY_UNMAPPED, f"taxonomy_non_financial_{value_kind}"
        )

    if concept in mapped_concepts:
        return FactClassificationResult(MAPPED)

    if concept in DETAIL_COMPONENT_CONCEPTS:
        return FactClassificationResult(INTENTIONALLY_UNMAPPED, "detail_component")

    if concept.startswith(_DETAIL_COMPONENT_PREFIXES):
        return FactClassificationResult(INTENTIONALLY_UNMAPPED, "detail_component")

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


def classify_facts(
    facts,
    mapped_concepts: set,
    allowed_namespaces=None,
    *,
    taxonomy_id: Optional[str] = None,
    taxonomy_version: Optional[str] = None,
    mapped_metadata_concepts: Optional[set] = None,
) -> ClassificationTally:
    exact_catalog_available = taxonomy_version_status(taxonomy_id, taxonomy_version) == "EXACT_VERSION_AVAILABLE"
    tally = ClassificationTally()
    for fact in facts:
        value_kind = None
        if exact_catalog_available:
            metadata = get_concept_metadata(taxonomy_id, taxonomy_version, fact.concept)
            # QName identity matters here as well: never borrow type metadata
            # from a standard concept when the fact itself belongs to an
            # issuer extension or another namespace.
            if metadata and metadata.get("namespace") == fact.namespace:
                value_kind = metadata.get("value_kind")

        # A known local name in an unsupported/extension namespace is not a
        # successful mapping. Keep known metadata/detail classifications,
        # but force a mapped-looking financial concept from an ineligible
        # namespace into the review bucket instead of reporting it as mapped.
        if (
            allowed_namespaces is not None
            and fact.namespace not in allowed_namespaces
            and fact.concept in mapped_concepts
            and fact.concept not in INTENTIONALLY_UNMAPPED_CONCEPTS
            and fact.concept not in DETAIL_COMPONENT_CONCEPTS
            and not fact.concept.startswith(_DETAIL_COMPONENT_PREFIXES)
        ):
            result = FactClassificationResult(UNMAPPED_FINANCIAL, "unrecognized_namespace_needs_review")
        else:
            result = classify_concept(
                fact.concept,
                mapped_concepts,
                value_kind=value_kind,
                mapped_metadata_concepts=mapped_metadata_concepts,
            )
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
