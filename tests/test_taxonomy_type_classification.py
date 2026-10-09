from datetime import date
from types import SimpleNamespace

from normalizers.fact_classifier import (
    INTENTIONALLY_UNMAPPED,
    UNMAPPED_FINANCIAL,
    classify_concept,
    classify_facts,
)
from normalizers.taxonomy_classifier import TAXONOMY_NON_FINANCIAL, classify_fact_taxonomy
from parsers.ixbrl_parser import XbrlContext, XbrlFact

CORE_2025 = "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"
CORE_2026 = "http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"


def _fact(concept, namespace=CORE_2025, value="Direct"):
    return XbrlFact(
        context_ref="D1", namespace=namespace, concept=concept,
        raw_tag=f"in-capmkt:{concept}", raw_value=value,
        numeric_value=None, unit_ref=None, decimals=None, scale=None, sign=None,
    )


def test_exact_taxonomy_enumeration_fact_is_not_counted_as_unmapped_financial():
    # TypeOfCashFlowStatement is declared as an enumeration in the supplied
    # Banking taxonomy package. Its non-monetary type is source metadata, not
    # a missing financial amount.
    fact = _fact("TypeOfCashFlowStatement")
    tally = classify_facts(
        [fact], set(), allowed_namespaces={CORE_2025},
        taxonomy_id="banking", taxonomy_version="2025-01-31",
    )
    assert tally.unmapped_financial_count == 0
    assert tally.intentionally_unmapped_count == 1
    assert tally.intentionally_unmapped_by_category["taxonomy_non_financial_enumeration"] == 1


def test_non_financial_xsd_type_cannot_be_counted_as_mapped_numeric_alias():
    result = classify_concept(
        "RevenueFromOperations", {"RevenueFromOperations"}, value_kind="string"
    )
    assert result.classification == INTENTIONALLY_UNMAPPED
    assert result.category == "taxonomy_non_financial_string"


def test_exact_taxonomy_type_is_used_by_taxonomy_classification():
    fact = _fact("TypeOfCashFlowStatement")
    context = XbrlContext(
        context_ref="D1", period_start=date(2025, 4, 1), period_end=date(2025, 6, 30)
    )
    result = classify_fact_taxonomy(
        fact, context, mapped_concepts=set(), taxonomy_id="banking", taxonomy_version="2025-01-31"
    )
    assert result.category == TAXONOMY_NON_FINANCIAL


def test_old_taxonomy_type_is_not_borrowed_when_exact_version_is_missing():
    fact = _fact("TypeOfCashFlowStatement", namespace=CORE_2026)
    tally = classify_facts(
        [fact], set(), allowed_namespaces={CORE_2026},
        taxonomy_id="ind_as_other_than_banks", taxonomy_version="2026-01-31",
    )
    assert tally.unmapped_financial_count == 1
    assert tally.unmapped_financial_concepts["TypeOfCashFlowStatement"] == 1
