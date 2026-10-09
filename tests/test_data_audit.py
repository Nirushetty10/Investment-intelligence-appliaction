from collections import namedtuple
from decimal import Decimal

from tools.audit_filing_integrity import (
    compare_fact_multisets,
    context_mismatches,
    database_fact_signature,
    period_metadata_mismatches,
    ratio_value_mismatches,
    resolve_storage_path,
    source_fact_signature,
    unit_mismatches,
)


def _source_fact(**overrides):
    Fact = namedtuple("Fact", ["namespace", "concept", "raw_tag", "raw_value", "numeric_value", "unit_ref", "decimals", "scale", "sign", "context_ref"])
    data = dict(namespace="urn:sebi:indas", concept="RevenueFromOperations", raw_tag="in-capmkt:RevenueFromOperations", raw_value="123.45678", numeric_value=Decimal("123.45678"), unit_ref="INR", decimals="2", scale=0, sign=None, context_ref="D_Q1")
    data.update(overrides)
    return Fact(**data)


def _database_fact(**overrides):
    data = dict(namespace="urn:sebi:indas", concept="RevenueFromOperations", raw_tag="in-capmkt:RevenueFromOperations", raw_value="123.45678", numeric_value=Decimal("123.4568"), unit_ref="INR", decimals="2", scale=0, sign=None, context_ref="D_Q1")
    data.update(overrides)
    return data


def test_fact_multiset_accepts_database_numeric_rounding_to_four_decimals():
    result = compare_fact_multisets([_source_fact()], [_database_fact()])
    assert result["match"] is True
    assert result["missing_from_database_count"] == 0
    assert result["extra_in_database_count"] == 0


def test_fact_multiset_detects_missing_and_extra_duplicates():
    src = [_source_fact(), _source_fact()]
    db = [_database_fact()]
    result = compare_fact_multisets(src, db)
    assert result["match"] is False
    assert result["missing_from_database_count"] == 1
    assert result["extra_in_database_count"] == 0


def test_fact_signature_includes_context_namespace_and_raw_value():
    source = source_fact_signature(_source_fact())
    wrong_context = database_fact_signature(_database_fact(context_ref="I_Q1"))
    wrong_namespace = database_fact_signature(_database_fact(namespace="urn:issuer:extension"))
    wrong_value = database_fact_signature(_database_fact(raw_value="999"))
    assert source != wrong_context
    assert source != wrong_namespace
    assert source != wrong_value


def test_context_mismatches_detect_period_and_dimension_changes():
    from datetime import date
    Ctx = namedtuple("Ctx", ["context_ref", "entity_identifier", "period_start", "period_end", "instant_date", "dimensions"])
    doc = namedtuple("Doc", ["contexts"])(contexts={"D_Q1": Ctx("D_Q1", "NSE:500", date(2026, 4, 1), date(2026, 6, 30), None, {"SegmentAxis": "RetailMember"})})
    rows = [{"context_ref": "D_Q1", "entity_identifier": "NSE:500", "period_start": date(2026, 4, 1), "period_end": date(2026, 6, 30), "instant_date": None, "dimensions_json": {"SegmentAxis": "OilMember"}}]
    mismatches = context_mismatches(doc, rows)
    assert len(mismatches) == 1
    assert mismatches[0]["issue"] == "CONTEXT_MISMATCH"
    assert "dimensions_json" in mismatches[0]["fields"]


def test_unit_mismatches_detect_missing_or_changed_units():
    Doc = namedtuple("Doc", ["units"])
    Unit = namedtuple("Unit", ["measure"])
    doc = Doc(units={"INR": Unit("INR"), "pure": Unit("pure")})
    rows = [{"unit_ref": "INR", "measure": "USD"}]
    mismatches = unit_mismatches(doc, rows)
    assert {item["issue"] for item in mismatches} == {"UNIT_MISMATCH", "MISSING_FROM_DATABASE"}


def test_storage_path_resolves_relative_path_from_project_root(tmp_path):
    raw_dir = tmp_path / "raw_store" / "xbrl" / "RELIANCE"
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / "filing_7.xml"
    raw_file.write_text("<xbrl/>", encoding="utf-8")
    resolved = resolve_storage_path("raw_store/xbrl/RELIANCE/filing_7.xml", project_root=tmp_path)
    assert resolved == raw_file.resolve()


def test_period_metadata_audit_detects_wrong_period_type_and_start():
    from datetime import date
    Resolved = namedtuple("Resolved", ["period_start", "period_type", "period_end"])
    resolved = Resolved(date(2025, 4, 1), "annual", date(2026, 3, 31))
    rows = [{"period_id": 9, "period_start_date": date(2026, 1, 1), "period_end_date": date(2026, 3, 31), "period_type": "quarterly"}]
    mismatches = period_metadata_mismatches(resolved, rows)
    assert len(mismatches) == 1
    assert set(mismatches[0]["fields"]) == {"period_start_date", "period_type"}


def test_ratio_audit_detects_mismatched_value_without_rescaling():
    source = [{"ratio_name": "Debt Equity Ratio", "value": Decimal("0.0041"), "unit": "pure", "source_concept": "DebtEquityRatio"}]
    database = [{"ratio_name": "Debt Equity Ratio", "value": Decimal("0.41"), "unit": "pure", "source_concept": "DebtEquityRatio"}]
    mismatches = ratio_value_mismatches(source, database)
    assert len(mismatches) == 1
    assert mismatches[0]["issue"] == "RATIO_MISMATCH"
    assert mismatches[0]["current_recompute"]["value"] == "0.004100"
    assert mismatches[0]["database"]["value"] == "0.410000"
