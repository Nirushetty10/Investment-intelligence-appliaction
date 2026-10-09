"""
normalizers/taxonomy_classifier.py

A second, complementary classification lens to normalizers/fact_classifier.py
(which answers "do we have a normalized field for this fact" via
MAPPED / INTENTIONALLY_UNMAPPED / UNMAPPED_FINANCIAL). This module answers
a different question — "where does this concept come from, taxonomically"
— via exactly the five categories requested:

    TAXONOMY_MAPPED            — a standard taxonomy concept we understand:
                                  either normalized into a field, or a
                                  known detail/component of one (reuses
                                  fact_classifier's MAPPED +
                                  DETAIL_COMPONENT_CONCEPTS + the
                                  AdjustmentsFor*/OtherAdjustments* pattern).
    COMPANY_EXTENSION           — namespace has been positively identified
                                  as an issuer extension by a schema/extension
                                  resolver; unfamiliar namespace alone is
                                  insufficient evidence.
    UNRECOGNIZED_NAMESPACE      — namespace is not in the current registry
                                  and cannot yet be identified as either a
                                  supported standard taxonomy or a confirmed
                                  issuer extension. Never use it for
                                  canonical mapping.
    DIMENSIONAL_SEGMENT         — tagged on a dimensioned context (a real
                                  segment/scenario breakdown), OR a
                                  concept whose NAME is a known segment
                                  concept even if (unusually) its context
                                  lacks dimensions. Never company-wide.
    STRUCTURAL_METADATA         — filer identity, board-meeting/admin
                                  dates, auditor/compliance declarations,
                                  disclosure text blocks — not a financial
                                  value.
    GENUINELY_UNMAPPED_FINANCIAL — a real, standard-taxonomy, non-segment,
                                  non-metadata financial concept with no
                                  normalized field and no known
                                  decomposition relationship to one. This
                                  is the only bucket that should worry a
                                  reviewer.

This is a classification lens. Canonical normalization separately enforces
namespace eligibility and context period type. Raw fact values are never
changed by classification.
"""
from dataclasses import dataclass
from typing import Optional

from normalizers.concept_map import DETAIL_COMPONENT_CONCEPTS
from normalizers.fact_classifier import (
    INTENTIONALLY_UNMAPPED_CONCEPTS,
    NON_FINANCIAL_VALUE_KINDS,
    _DETAIL_COMPONENT_PREFIXES,
)
from taxonomy.registry import DEFAULT_REGISTRY
from taxonomy.catalog import get_concept_metadata, taxonomy_version_status

TAXONOMY_MAPPED = "TAXONOMY_MAPPED"
COMPANY_EXTENSION = "COMPANY_EXTENSION"
UNRECOGNIZED_NAMESPACE = "UNRECOGNIZED_NAMESPACE"
TAXONOMY_UNSUPPORTED = "TAXONOMY_UNSUPPORTED"
DIMENSIONAL_SEGMENT = "DIMENSIONAL_SEGMENT"
STRUCTURAL_METADATA = "STRUCTURAL_METADATA"
TAXONOMY_NON_FINANCIAL = "TAXONOMY_NON_FINANCIAL"
GENUINELY_UNMAPPED_FINANCIAL = "GENUINELY_UNMAPPED_FINANCIAL"

_STRUCTURAL_METADATA_CATEGORIES = {
    "filer_identity", "board_meeting_admin", "auditor_compliance", "disclosure_text",
}


@dataclass
class TaxonomyClassificationResult:
    category: str
    reason: str


def classify_fact_taxonomy(
    fact,
    context,
    mapped_concepts: set,
    registry=DEFAULT_REGISTRY,
    confirmed_extension_namespaces=None,
    canonical_mapping_enabled: bool = True,
    taxonomy_id: Optional[str] = None,
    taxonomy_version: Optional[str] = None,
    mapped_metadata_concepts: Optional[set] = None,
) -> TaxonomyClassificationResult:
    """
    `fact` is a parsers.ixbrl_parser.XbrlFact; `context` is the
    corresponding parsers.ixbrl_parser.XbrlContext (or None). `mapped_concepts`
    is the same set financial_normalizer.normalize() builds from every
    concept map (the "would this resolve to a normalized field" set).
    """
    concept = fact.concept

    # 1. Use issuer-extension only when there is affirmative schema evidence.
    # An arbitrary unfamiliar namespace could also be a standard taxonomy
    # we have not registered yet, so it must not be mislabeled as an issuer
    # extension merely because the URI is new to this application.
    confirmed_extension_namespaces = confirmed_extension_namespaces or set()
    if fact.namespace and fact.namespace in confirmed_extension_namespaces:
        return TaxonomyClassificationResult(
            COMPANY_EXTENSION,
            f"Namespace {fact.namespace!r} is identified by extension-schema evidence "
            "as an issuer-defined extension.",
        )

    if not fact.namespace or not registry.is_standard_namespace(fact.namespace):
        return TaxonomyClassificationResult(
            UNRECOGNIZED_NAMESPACE,
            f"Namespace {fact.namespace!r} is not recognized by the active taxonomy registry; "
            "it was not assumed to be a company extension and is not eligible for canonical mapping.",
        )

    # 2. Dimensional/segment: real dimensioned context, OR a concept whose
    # name is a known segment concept even without one (spec item 7: keep
    # dimensional facts separate from company-wide facts, by context
    # evidence first and concept-name as a fallback signal).
    if context is not None and context.has_dimensions:
        return TaxonomyClassificationResult(
            DIMENSIONAL_SEGMENT, "Tagged on a dimensioned (segment/scenario) context."
        )
    if INTENTIONALLY_UNMAPPED_CONCEPTS.get(concept) == "segment_dimensional":
        return TaxonomyClassificationResult(
            DIMENSIONAL_SEGMENT,
            "Concept name is a known segment-reporting element (unusually not dimensioned "
            "in this instance).",
        )

    # 3. Structural/metadata.
    metadata_category = INTENTIONALLY_UNMAPPED_CONCEPTS.get(concept)
    if metadata_category in _STRUCTURAL_METADATA_CATEGORIES:
        return TaxonomyClassificationResult(
            STRUCTURAL_METADATA, f"Filer/filing administrative metadata ({metadata_category})."
        )

    # Use taxonomy-declared value type only when the exact family/version is
    # loaded and the fact QName's namespace matches that concept declaration.
    # This avoids misclassifying concepts by local name or borrowing a type
    # from a nearby historical taxonomy version.
    if taxonomy_version_status(taxonomy_id, taxonomy_version) == "EXACT_VERSION_AVAILABLE":
        metadata = get_concept_metadata(taxonomy_id, taxonomy_version, concept)
        if (
            metadata
            and metadata.get("namespace") == fact.namespace
            and metadata.get("value_kind") in NON_FINANCIAL_VALUE_KINDS
            and concept not in (mapped_metadata_concepts or set())
        ):
            return TaxonomyClassificationResult(
                TAXONOMY_NON_FINANCIAL,
                f"Taxonomy declares value_kind={metadata['value_kind']}; this is a non-financial disclosure/metadata fact, not an unmapped financial value.",
            )

    # Recognizing a taxonomy family does not mean its canonical mappings are
    # validated. Never report a concept as mapped for a sector whose mapping
    # set has not been enabled and tested.
    if not canonical_mapping_enabled:
        return TaxonomyClassificationResult(
            TAXONOMY_UNSUPPORTED,
            "The filing's taxonomy family is recognized, but canonical mapping is not enabled "
            "for this family yet. The fact remains preserved for sector-specific mapping work.",
        )

    # 4. Taxonomy-mapped: resolved into a normalized field, OR a known
    # detail/component of one.
    if concept in mapped_concepts:
        return TaxonomyClassificationResult(TAXONOMY_MAPPED, "Resolved into a normalized field.")
    if concept in DETAIL_COMPONENT_CONCEPTS:
        return TaxonomyClassificationResult(
            TAXONOMY_MAPPED, f"Known detail/component concept: {DETAIL_COMPONENT_CONCEPTS[concept]}"
        )
    if concept.startswith(_DETAIL_COMPONENT_PREFIXES):
        return TaxonomyClassificationResult(
            TAXONOMY_MAPPED, "Matches the AdjustmentsFor*/OtherAdjustments* cash-flow detail pattern."
        )

    # 5. Fail-safe default.
    return TaxonomyClassificationResult(
        GENUINELY_UNMAPPED_FINANCIAL,
        "Standard-taxonomy financial concept with no normalized field and no known "
        "decomposition relationship — needs review.",
    )


@dataclass
class TaxonomyClassificationTally:
    counts: dict
    by_concept: dict  # category -> {concept: count}

    @classmethod
    def empty(cls):
        return cls(counts={k: 0 for k in (
            TAXONOMY_MAPPED, COMPANY_EXTENSION, UNRECOGNIZED_NAMESPACE, TAXONOMY_UNSUPPORTED,
            DIMENSIONAL_SEGMENT, STRUCTURAL_METADATA, TAXONOMY_NON_FINANCIAL, GENUINELY_UNMAPPED_FINANCIAL,
        )}, by_concept={})


def classify_facts_taxonomy(
    doc, mapped_concepts: set, registry=DEFAULT_REGISTRY, canonical_mapping_enabled: bool = True,
    taxonomy_id: Optional[str] = None, taxonomy_version: Optional[str] = None,
    mapped_metadata_concepts: Optional[set] = None,
) -> TaxonomyClassificationTally:
    tally = TaxonomyClassificationTally.empty()
    for fact in doc.facts:
        context = doc.contexts.get(fact.context_ref)
        result = classify_fact_taxonomy(
            fact,
            context,
            mapped_concepts,
            registry,
            confirmed_extension_namespaces=getattr(doc, "confirmed_extension_namespaces", set()),
            canonical_mapping_enabled=canonical_mapping_enabled,
            taxonomy_id=taxonomy_id,
            taxonomy_version=taxonomy_version,
            mapped_metadata_concepts=mapped_metadata_concepts,
        )
        tally.counts[result.category] += 1
        tally.by_concept.setdefault(result.category, {})
        tally.by_concept[result.category][fact.concept] = tally.by_concept[result.category].get(fact.concept, 0) + 1
    return tally
