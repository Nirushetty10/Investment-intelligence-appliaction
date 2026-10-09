#!/usr/bin/env python3
"""Read-only audit of source XBRL bytes, persisted raw facts and normalized rows.

This command never writes to PostgreSQL. It verifies that:
  1. the saved source XML/HTML bytes match the recorded SHA-256,
  2. source parse contexts/units/facts match what is stored in PostgreSQL,
  3. current-code normalization can be reproduced and compared with stored rows,
  4. a TRUSTED period does not contradict the persisted taxonomy/trust evidence.

Differences between current recomputation and older normalized rows are reported
for review only; the command never updates or deletes financial data.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Iterable

# Make direct execution (`python tools/audit_filing_integrity.py`) work from
# the repository root on Windows and Linux without requiring PYTHONPATH.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from config.settings import BASE_DIR
from db.connection import get_engine
from normalizers.financial_normalizer import normalize
from parsers.ixbrl_parser import ParsedXbrlDocument, parse_document
from resolvers.period_resolver import resolve_canonical_period
from repositories.financial_repository import (
    _BALANCE_SHEET_COLUMNS,
    _CASHFLOW_COLUMNS,
    _INCOME_STATEMENT_COLUMNS,
)

LOG = logging.getLogger("nse_pipeline.data_audit")
FOUR_DP = Decimal("0.0001")
TWO_DP = Decimal("0.01")
SIX_DP = Decimal("0.000001")


def _q(value: Any, quantum: Decimal = FOUR_DP) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(quantum)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _sort_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def source_fact_signature(fact: Any) -> tuple:
    """Stable source-fact identity matching the columns actually persisted."""
    return (
        fact.namespace,
        fact.concept,
        fact.raw_tag,
        fact.raw_value,
        _q(fact.numeric_value),
        fact.unit_ref,
        fact.decimals,
        fact.scale,
        fact.sign,
        fact.context_ref,
    )


def database_fact_signature(row: Any) -> tuple:
    return (
        row["namespace"],
        row["concept"],
        row["raw_tag"],
        row["raw_value"],
        _q(row["numeric_value"]),
        row["unit_ref"],
        row["decimals"],
        row["scale"],
        row["sign"],
        row["context_ref"],
    )


def compare_fact_multisets(source_facts: Iterable[Any], database_rows: Iterable[Any]) -> dict:
    """Compare fact multisets so duplicates cannot disappear in set comparison."""
    src = Counter(source_fact_signature(f) for f in source_facts)
    db = Counter(database_fact_signature(r) for r in database_rows)
    missing = list((src - db).items())
    extra = list((db - src).items())
    return {
        "source_fact_count": sum(src.values()),
        "database_fact_count": sum(db.values()),
        "missing_from_database_count": sum(n for _, n in missing),
        "extra_in_database_count": sum(n for _, n in extra),
        "missing_from_database_examples": [
            {"signature": _signature_json(sig), "count": count}
            for sig, count in missing[:10]
        ],
        "extra_in_database_examples": [
            {"signature": _signature_json(sig), "count": count}
            for sig, count in extra[:10]
        ],
        "match": not missing and not extra,
    }


def _signature_json(signature: tuple) -> dict:
    keys = (
        "namespace", "concept", "raw_tag", "raw_value", "numeric_value",
        "unit_ref", "decimals", "scale", "sign", "context_ref",
    )
    values = [str(v) if isinstance(v, Decimal) else v for v in signature]
    return dict(zip(keys, values))


def context_mismatches(doc: ParsedXbrlDocument, rows: Iterable[Any]) -> list[dict]:
    stored = {row["context_ref"]: row for row in rows}
    mismatches = []
    for context_ref, source in doc.contexts.items():
        row = stored.pop(context_ref, None)
        if row is None:
            mismatches.append({"context_ref": context_ref, "issue": "MISSING_FROM_DATABASE"})
            continue
        expected = {
            "entity_identifier": source.entity_identifier,
            "period_start": source.period_start,
            "period_end": source.period_end,
            "instant_date": source.instant_date,
            "dimensions_json": _sort_json(source.dimensions),
        }
        actual = {
            "entity_identifier": row["entity_identifier"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "instant_date": row["instant_date"],
            "dimensions_json": _sort_json(row["dimensions_json"]),
        }
        changed = {
            key: {"source": _iso(expected[key]) if key.endswith("date") or key == "period_start" or key == "period_end" else expected[key],
                  "database": _iso(actual[key]) if key.endswith("date") or key == "period_start" or key == "period_end" else actual[key]}
            for key in expected
            if expected[key] != actual[key]
        }
        if changed:
            mismatches.append({"context_ref": context_ref, "issue": "CONTEXT_MISMATCH", "fields": changed})
    for context_ref in sorted(stored):
        mismatches.append({"context_ref": context_ref, "issue": "EXTRA_IN_DATABASE"})
    return mismatches


def unit_mismatches(doc: ParsedXbrlDocument, rows: Iterable[Any]) -> list[dict]:
    source = {key: unit.measure for key, unit in doc.units.items()}
    stored = {row["unit_ref"]: row["measure"] for row in rows}
    problems = []
    for unit_ref in sorted(set(source) | set(stored)):
        if unit_ref not in source:
            problems.append({"unit_ref": unit_ref, "issue": "EXTRA_IN_DATABASE", "database": stored[unit_ref]})
        elif unit_ref not in stored:
            problems.append({"unit_ref": unit_ref, "issue": "MISSING_FROM_DATABASE", "source": source[unit_ref]})
        elif source[unit_ref] != stored[unit_ref]:
            problems.append({"unit_ref": unit_ref, "issue": "UNIT_MISMATCH", "source": source[unit_ref], "database": stored[unit_ref]})
    return problems


def period_metadata_mismatches(resolved: Any, period_rows: Iterable[dict]) -> list[dict]:
    """Compare persisted period keys with the canonical source-context resolution."""
    problems = []
    for row in period_rows:
        changed = {}
        if resolved.period_end is not None and row["period_end_date"] != resolved.period_end:
            changed["period_end_date"] = {"source_resolver": _iso(resolved.period_end), "database": _iso(row["period_end_date"])}
        if resolved.period_start is not None and row["period_start_date"] != resolved.period_start:
            changed["period_start_date"] = {"source_resolver": _iso(resolved.period_start), "database": _iso(row["period_start_date"])}
        if resolved.period_type is not None and row["period_type"] != resolved.period_type:
            changed["period_type"] = {"source_resolver": resolved.period_type, "database": row["period_type"]}
        if changed:
            problems.append({"period_id": row["period_id"], "fields": changed})
    return problems


def ratio_value_mismatches(source_ratios: Iterable[dict], database_ratios: Iterable[dict]) -> list[dict]:
    """Compare normalized ratio values without scaling or repairing either side."""
    expected = {r.get("ratio_name"): r for r in source_ratios}
    actual = {r.get("ratio_name"): r for r in database_ratios}
    problems = []
    for name in sorted(set(expected) | set(actual), key=lambda v: str(v)):
        src = expected.get(name)
        db = actual.get(name)
        if src is None:
            problems.append({"ratio_name": name, "issue": "EXTRA_IN_DATABASE"})
            continue
        if db is None:
            problems.append({"ratio_name": name, "issue": "MISSING_FROM_DATABASE"})
            continue
        if _q(src.get("value"), SIX_DP) != _q(db.get("value"), SIX_DP) or src.get("unit") != db.get("unit") or src.get("source_concept") != db.get("source_concept"):
            problems.append({
                "ratio_name": name,
                "issue": "RATIO_MISMATCH",
                "current_recompute": {"value": str(_q(src.get("value"), SIX_DP)) if src.get("value") is not None else None,
                                       "unit": src.get("unit"), "source_concept": src.get("source_concept")},
                "database": {"value": str(_q(db.get("value"), SIX_DP)) if db.get("value") is not None else None,
                             "unit": db.get("unit"), "source_concept": db.get("source_concept")},
            })
    return problems


def _decimal_for_column(field: str, value: Any, kind: str) -> Decimal | None:
    if field in {"basic_eps", "diluted_eps", "eps_face_value"}:
        return _q(value, FOUR_DP)
    if kind == "ratio":
        return _q(value, SIX_DP)
    return _q(value, TWO_DP)


def _compare_statement(
    *, row: Any, expected_fields: dict, columns: list[str], prefix: str, kind: str,
    report: dict,
) -> None:
    row_exists_key = f"{prefix}_row_id"
    if row[row_exists_key] is None:
        if any(expected_fields.get(c) is not None for c in columns):
            report["issues"].append({
                "severity": "WARNING", "code": f"{prefix.upper()}_ROW_MISSING",
                "message": f"Current normalization produces values for {prefix}, but no persisted {prefix} row exists.",
            })
        return
    drift = []
    for col in columns:
        expected = _decimal_for_column(col, expected_fields.get(col), kind)
        actual = _decimal_for_column(col, row.get(f"{prefix}__{col}"), kind)
        if expected != actual:
            drift.append({"field": col, "current_recompute": str(expected) if expected is not None else None,
                          "database": str(actual) if actual is not None else None})
    if drift:
        report["issues"].append({
            "severity": "WARNING", "code": "NORMALIZED_RECOMPUTE_DRIFT",
            "statement": prefix, "message": "Stored normalized values differ from current-code recomputation. Review before reprocessing; this audit did not modify the rows.",
            "difference_count": len(drift), "differences": drift[:25],
        })


def resolve_storage_path(storage_path: str | None, project_root: Path = BASE_DIR) -> Path | None:
    if not storage_path:
        return None
    raw = Path(storage_path)
    candidates = [raw] if raw.is_absolute() else [project_root / raw, Path.cwd() / raw, project_root / "raw_store" / raw]
    # If the DB contains a path from a previous project root, recover only the
    # suffix beneath raw_store; never use an arbitrary basename match.
    parts_lower = [part.lower() for part in raw.parts]
    if "raw_store" in parts_lower:
        index = parts_lower.index("raw_store")
        suffix = Path(*raw.parts[index + 1 :])
        candidates.append(project_root / "raw_store" / suffix)
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _issue(report: dict, severity: str, code: str, message: str, **details: Any) -> None:
    report["issues"].append({"severity": severity, "code": code, "message": message, **details})


def _as_jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_as_jsonable(v) for v in value]
    return value


def _get_filing_rows(conn, symbols: list[str], filing_ids: list[int], limit: int) -> list[Any]:
    where, params = [], {"limit": limit}
    if symbols:
        where.append("upper(r.symbol) = ANY(:symbols)")
        params["symbols"] = [s.upper() for s in symbols]
    if filing_ids:
        where.append("r.filing_id = ANY(:filing_ids)")
        params["filing_ids"] = filing_ids
    if not where:
        raise ValueError("Pass --symbols (for example RELIANCE) and/or --filing-ids.")
    sql = text(
        """
        SELECT r.filing_id, r.symbol, r.company_name, r.period_type AS registry_period_type,
               r.statement_type AS registry_statement_type, r.period_start_date AS registry_period_start,
               r.period_end_date AS registry_period_end, r.financial_year, r.reporting_quarter,
               r.xbrl_url, r.ixbrl_url, r.parse_status, r.validation_status, r.normalization_status,
               r.submission_type, r.cumulative, r.accounting_standard,
               rf.raw_id, rf.source_url AS raw_source_url, rf.content_type AS raw_content_type,
               rf.content_hash AS raw_content_hash, rf.storage_path AS raw_storage_path, rf.created_at AS raw_created_at
        FROM nse_filing_registry r
        LEFT JOIN LATERAL (
            SELECT rf.*
            FROM raw_filings rf
            WHERE rf.filing_id = r.filing_id
            ORDER BY
                CASE WHEN r.xbrl_url IS NOT NULL AND rf.source_url = r.xbrl_url THEN 0 ELSE 1 END,
                CASE WHEN lower(coalesce(rf.content_type, '')) LIKE '%html%'
                           OR (r.ixbrl_url IS NOT NULL AND rf.source_url = r.ixbrl_url) THEN 1 ELSE 0 END,
                rf.created_at DESC, rf.raw_id DESC
            LIMIT 1
        ) rf ON TRUE
        WHERE """ + " AND ".join(where) + "\n"
        "ORDER BY r.period_end_date DESC NULLS LAST, r.broadcast_date DESC NULLS LAST, r.filing_id DESC\n"
        "LIMIT :limit"
    )
    return conn.execute(sql, params).mappings().all()


def _get_source_and_database_counts(conn, filing_id: int) -> dict:
    counts = conn.execute(text(
        """
        SELECT
          (SELECT COUNT(*) FROM xbrl_contexts WHERE filing_id=:fid) AS contexts,
          (SELECT COUNT(*) FROM xbrl_units WHERE filing_id=:fid) AS units,
          (SELECT COUNT(*) FROM xbrl_facts WHERE filing_id=:fid) AS facts,
          (SELECT COUNT(*) FROM raw_xbrl_documents WHERE filing_id=:fid) AS raw_xbrl_documents
        """
    ), {"fid": filing_id}).mappings().one()
    return dict(counts)


def _get_context_rows(conn, filing_id: int) -> list[Any]:
    return conn.execute(text(
        "SELECT context_ref, entity_identifier, period_start, period_end, instant_date, dimensions_json "
        "FROM xbrl_contexts WHERE filing_id=:fid ORDER BY context_ref"
    ), {"fid": filing_id}).mappings().all()


def _get_unit_rows(conn, filing_id: int) -> list[Any]:
    return conn.execute(text(
        "SELECT unit_ref, measure FROM xbrl_units WHERE filing_id=:fid ORDER BY unit_ref"
    ), {"fid": filing_id}).mappings().all()


def _get_fact_rows(conn, filing_id: int) -> list[Any]:
    return conn.execute(text(
        """
        SELECT f.namespace, f.concept, f.raw_tag, f.raw_value, f.numeric_value,
               f.unit_ref, f.decimals, f.scale, f.sign, c.context_ref
        FROM xbrl_facts f
        LEFT JOIN xbrl_contexts c ON c.context_id = f.context_id
        WHERE f.filing_id=:fid
        ORDER BY f.fact_id
        """
    ), {"fid": filing_id}).mappings().all()


def _get_period_rows(conn, filing_id: int) -> list[Any]:
    select_parts = [
        "p.period_id", "p.period_type", "p.statement_type", "p.period_start_date", "p.period_end_date",
        "p.trust_status", "p.taxonomy_id", "p.taxonomy_version", "p.taxonomy_catalog_status",
        "p.canonical_mapping_enabled", "p.trust_reasons", "p.source_schema_refs",
        "i.id AS income_row_id", "i.unit AS income_unit",
        "b.id AS balance_row_id", "b.unit AS balance_unit",
        "cfs.id AS cashflow_row_id", "cfs.unit AS cashflow_unit",
    ]
    select_parts += [f"i.{col} AS income__{col}" for col in _INCOME_STATEMENT_COLUMNS]
    select_parts += [f"b.{col} AS balance__{col}" for col in _BALANCE_SHEET_COLUMNS]
    select_parts += [f"cfs.{col} AS cashflow__{col}" for col in _CASHFLOW_COLUMNS]
    sql = text(
        "SELECT " + ", ".join(select_parts) + " FROM financial_periods p "
        "LEFT JOIN income_statement i ON i.period_id=p.period_id "
        "LEFT JOIN balance_sheet b ON b.period_id=p.period_id "
        "LEFT JOIN cashflow_statement cfs ON cfs.period_id=p.period_id "
        "WHERE p.source_filing_id=:fid ORDER BY p.period_id"
    )
    periods = conn.execute(sql, {"fid": filing_id}).mappings().all()
    for row in periods:
        ratios = conn.execute(text(
            "SELECT ratio_name, value, unit, source_concept, needs_validation FROM ratios WHERE period_id=:pid ORDER BY ratio_name"
        ), {"pid": row["period_id"]}).mappings().all()
        row = dict(row)
        row["ratios"] = [dict(r) for r in ratios]
        yield row


def _audit_trust_row(conn, row: Any, filing_id: int, report: dict) -> None:
    reasons = row["trust_reasons"] or []
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            reasons = [reasons]
    catalog_ok = row["taxonomy_catalog_status"] == "EXACT_VERSION_AVAILABLE"
    mapping_ok = bool(row["canonical_mapping_enabled"])
    quality_counts = conn.execute(text(
        "SELECT severity, COUNT(*) AS n FROM data_quality_log WHERE filing_id=:fid "
        "AND severity IN ('WARNING','ERROR') GROUP BY severity"
    ), {"fid": filing_id}).mappings().all()
    has_blocking_quality = bool(quality_counts)
    if row["trust_status"] == "TRUSTED" and (not catalog_ok or not mapping_ok or reasons or has_blocking_quality):
        _issue(report, "ERROR", "TRUST_STATUS_CONTRADICTS_EVIDENCE",
               "A period is marked TRUSTED while persisted taxonomy/trust/data-quality evidence contains unresolved or blocking conditions.",
               period_id=row["period_id"], taxonomy_catalog_status=row["taxonomy_catalog_status"],
               canonical_mapping_enabled=mapping_ok, trust_reasons=_as_jsonable(reasons),
               quality_counts={q["severity"]: q["n"] for q in quality_counts})


def audit_one_filing(conn, filing: Any) -> dict:
    filing_id = int(filing["filing_id"])
    report = {
        "filing_id": filing_id,
        "symbol": filing["symbol"],
        "period_end_date": _iso(filing["registry_period_end"]),
        "registry_period_type": filing["registry_period_type"],
        "registry_statement_type": filing["registry_statement_type"],
        "parse_status": filing["parse_status"],
        "validation_status": filing["validation_status"],
        "normalization_status": filing["normalization_status"],
        "raw_source": {"raw_id": filing["raw_id"], "url": filing["raw_source_url"],
                       "content_type": filing["raw_content_type"], "storage_path": filing["raw_storage_path"],
                       "recorded_sha256": filing["raw_content_hash"]},
        "database_counts": {},
        "source_comparison": {},
        "normalized_periods": [],
        "issues": [],
    }
    counts = _get_source_and_database_counts(conn, filing_id)
    report["database_counts"] = counts
    if filing["raw_id"] is None or not filing["raw_storage_path"]:
        _issue(report, "ERROR", "RAW_SOURCE_RECORD_MISSING", "No raw source bytes are linked to this filing; raw-to-database audit cannot be completed.")
        report["status"] = "FAIL"
        return report

    source_path = resolve_storage_path(filing["raw_storage_path"])
    if source_path is None:
        _issue(report, "ERROR", "RAW_SOURCE_FILE_NOT_FOUND", "Recorded raw source path does not exist on this machine.",
               recorded_storage_path=filing["raw_storage_path"])
        report["status"] = "FAIL"
        return report
    report["raw_source"]["resolved_path"] = str(source_path)
    raw_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(raw_bytes).hexdigest()
    report["raw_source"]["computed_sha256"] = source_hash
    hash_matches = source_hash.lower() == str(filing["raw_content_hash"] or "").lower()
    report["raw_source"]["hash_matches_database"] = hash_matches
    if not hash_matches:
        _issue(report, "ERROR", "RAW_SOURCE_HASH_MISMATCH", "SHA-256 of source bytes does not match raw_filings.content_hash.",
               recorded_sha256=filing["raw_content_hash"], computed_sha256=source_hash)

    try:
        doc = parse_document(raw_bytes)
    except Exception as exc:  # audit must produce a report for malformed source bytes
        _issue(report, "ERROR", "SOURCE_PARSE_FAILED", f"Could not parse saved source bytes: {exc}")
        report["status"] = "FAIL"
        return report

    source_counts = {"contexts": len(doc.contexts), "units": len(doc.units), "facts": len(doc.facts), "source_format": doc.source_format}
    report["source_comparison"]["source_counts"] = source_counts
    report["source_comparison"]["schema_refs"] = list(doc.schema_refs)
    for key in ("contexts", "units", "facts"):
        if source_counts[key] != counts[key]:
            _issue(report, "ERROR", f"{key.upper()}_COUNT_MISMATCH", f"Source has {source_counts[key]} {key}, database has {counts[key]}.",
                   source_count=source_counts[key], database_count=counts[key])

    period_resolution_input = {
        "period_end_date": filing["registry_period_end"],
        "period_start_date": filing["registry_period_start"],
        "period_type": filing["registry_period_type"],
        "submission_type": filing["submission_type"],
        "financial_year": filing["financial_year"],
    }
    resolved_period = resolve_canonical_period(doc, period_resolution_input)
    report["source_comparison"]["canonical_period_resolution"] = {
        "period_start": _iso(resolved_period.period_start),
        "period_end": _iso(resolved_period.period_end),
        "period_type": resolved_period.period_type,
        "financial_year": resolved_period.financial_year,
        "financial_quarter": resolved_period.financial_quarter,
        "registry_period_type": resolved_period.registry_period_type,
        "conflict": resolved_period.conflict,
        "reason": resolved_period.resolution_reason,
    }
    if resolved_period.conflict:
        _issue(report, "WARNING", "REGISTRY_SOURCE_PERIOD_CONFLICT", "Filing registry metadata conflicts with period evidence in the source XBRL.",
               reason=resolved_period.resolution_reason)

    context_rows = _get_context_rows(conn, filing_id)
    context_diff = context_mismatches(doc, context_rows)
    report["source_comparison"]["context_mismatch_count"] = len(context_diff)
    report["source_comparison"]["context_mismatches"] = context_diff[:20]
    if context_diff:
        _issue(report, "ERROR", "CONTEXT_CONTENT_MISMATCH", "Persisted contexts differ from source contexts.", mismatch_count=len(context_diff))

    unit_rows = _get_unit_rows(conn, filing_id)
    unit_diff = unit_mismatches(doc, unit_rows)
    report["source_comparison"]["unit_mismatch_count"] = len(unit_diff)
    report["source_comparison"]["unit_mismatches"] = unit_diff[:20]
    if unit_diff:
        _issue(report, "ERROR", "UNIT_CONTENT_MISMATCH", "Persisted units differ from source unit declarations.", mismatch_count=len(unit_diff))

    fact_rows = _get_fact_rows(conn, filing_id)
    fact_diff = compare_fact_multisets(doc.facts, fact_rows)
    report["source_comparison"]["facts"] = fact_diff
    if not fact_diff["match"]:
        _issue(report, "ERROR", "FACT_CONTENT_MISMATCH", "Persisted XBRL facts differ from the saved source document.",
               missing_from_database_count=fact_diff["missing_from_database_count"],
               extra_in_database_count=fact_diff["extra_in_database_count"])

    period_rows = list(_get_period_rows(conn, filing_id))
    report["normalized_periods_count"] = len(period_rows)
    period_diffs = period_metadata_mismatches(resolved_period, period_rows)
    report["source_comparison"]["normalized_period_mismatches"] = period_diffs
    if period_diffs:
        _issue(report, "ERROR", "NORMALIZED_PERIOD_METADATA_MISMATCH", "Persisted normalized period type/start/end differs from the source-context resolver.",
               mismatches=period_diffs)
    if period_rows and (resolved_period.period_start is None or resolved_period.period_type is None):
        _issue(report, "WARNING", "SOURCE_PERIOD_RESOLUTION_AMBIGUOUS", "A normalized period exists, but current source-context resolution cannot independently confirm its start/type.",
               resolver_reason=resolved_period.resolution_reason)
    if not period_rows and filing["normalization_status"] == "SUCCESS":
        _issue(report, "ERROR", "NORMALIZED_PERIOD_MISSING", "Filing is marked normalization SUCCESS but no financial_periods row points to it.")

    for period_row in period_rows:
        current = {
            "period_id": period_row["period_id"],
            "period_type": period_row["period_type"],
            "statement_type": period_row["statement_type"],
            "period_start_date": _iso(period_row["period_start_date"]),
            "period_end_date": _iso(period_row["period_end_date"]),
            "trust_status": period_row["trust_status"],
            "taxonomy_id": period_row["taxonomy_id"],
            "taxonomy_version": period_row["taxonomy_version"],
            "taxonomy_catalog_status": period_row["taxonomy_catalog_status"],
            "canonical_mapping_enabled": bool(period_row["canonical_mapping_enabled"]),
            "trust_reasons": _as_jsonable(period_row["trust_reasons"] or []),
            "source_schema_refs": _as_jsonable(period_row["source_schema_refs"] or []),
            "persisted_ratios_count": len(period_row["ratios"]),
            "flagged_ratios_count": sum(bool(r["needs_validation"]) for r in period_row["ratios"]),
            "recompute_drift": {},
        }
        _audit_trust_row(conn, period_row, filing_id, report)
        try:
            recomputed = normalize(doc, period_end=period_row["period_end_date"], period_start=period_row["period_start_date"])
            _compare_statement(row=period_row, expected_fields=recomputed.income_statement,
                               columns=list(_INCOME_STATEMENT_COLUMNS), prefix="income", kind="numeric", report=report)
            _compare_statement(row=period_row, expected_fields=recomputed.balance_sheet,
                               columns=list(_BALANCE_SHEET_COLUMNS), prefix="balance", kind="numeric", report=report)
            _compare_statement(row=period_row, expected_fields=recomputed.cashflow_statement,
                               columns=list(_CASHFLOW_COLUMNS), prefix="cashflow", kind="numeric", report=report)
            current["current_recompute_taxonomy"] = {
                "taxonomy_id": getattr(recomputed.resolved_taxonomy, "taxonomy_id", None),
                "taxonomy_version": getattr(recomputed.resolved_taxonomy, "version", None),
                "canonical_mapping_enabled": bool(getattr(recomputed.resolved_taxonomy, "canonical_mapping_enabled", False)),
                "unmapped_financial_count": getattr(recomputed.classification, "unmapped_financial_count", None),
            }
            current["recompute_drift"]["ratio_names_current"] = sorted(r.get("ratio_name") for r in recomputed.ratios)
            ratio_diffs = ratio_value_mismatches(recomputed.ratios, period_row["ratios"])
            current["recompute_drift"]["ratio_mismatch_count"] = len(ratio_diffs)
            current["recompute_drift"]["ratio_mismatches"] = ratio_diffs[:25]
            if ratio_diffs:
                _issue(report, "WARNING", "NORMALIZED_RECOMPUTE_DRIFT", "Stored ratio values differ from current-code recomputation; raw ratios were not modified.",
                       period_id=period_row["period_id"], statement="ratios", difference_count=len(ratio_diffs), differences=ratio_diffs[:25])
        except Exception as exc:
            _issue(report, "WARNING", "NORMALIZATION_RECOMPUTE_FAILED", f"Current-code normalization could not be recomputed: {exc}", period_id=period_row["period_id"])
        report["normalized_periods"].append(current)

    if any(i["severity"] == "ERROR" for i in report["issues"]):
        report["status"] = "FAIL"
    elif report["issues"]:
        report["status"] = "REVIEW_REQUIRED"
    else:
        report["status"] = "PASS"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only audit of source XBRL vs PostgreSQL raw and normalized records.")
    parser.add_argument("--symbols", help="Comma-separated NSE symbols, e.g. RELIANCE,TCS.")
    parser.add_argument("--filing-ids", help="Comma-separated nse_filing_registry filing IDs.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum filings to audit (default: 10).")
    parser.add_argument("--output", help="Optional JSON report path. If omitted, prints the full report to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    symbols = [x.strip().upper() for x in (args.symbols or "").split(",") if x.strip()]
    try:
        filing_ids = [int(x.strip()) for x in (args.filing_ids or "").split(",") if x.strip()]
    except ValueError:
        print("--filing-ids must be comma-separated integers.", file=sys.stderr)
        return 2
    if not symbols and not filing_ids:
        print("Pass --symbols RELIANCE or --filing-ids 7,9.", file=sys.stderr)
        return 2
    if args.limit < 1 or args.limit > 1000:
        print("--limit must be between 1 and 1000.", file=sys.stderr)
        return 2

    engine = get_engine()
    try:
        with engine.connect() as conn:
            with conn.begin():
                # Safety: PostgreSQL rejects any accidental DML/DDL in this session.
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                filings = _get_filing_rows(conn, symbols, filing_ids, args.limit)
                reports = [audit_one_filing(conn, filing) for filing in filings]
    except Exception as exc:
        print(f"Audit could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not reports:
        overall = "FAIL"
    else:
        overall = "PASS" if all(r.get("status") == "PASS" for r in reports) else "REVIEW_REQUIRED"
        if any(r.get("status") == "FAIL" for r in reports):
            overall = "FAIL"
    result = {
        "audit_type": "READ_ONLY_SOURCE_TO_DATABASE_INTEGRITY",
        "database": {"host": "configured", "database_name": "configured", "credentials_emitted": False},
        "selected_filing_count": len(reports),
        "overall_status": overall,
        "summary": {
            "passed": sum(r.get("status") == "PASS" for r in reports),
            "review_required": sum(r.get("status") == "REVIEW_REQUIRED" for r in reports),
            "failed": sum(r.get("status") == "FAIL" for r in reports),
            "total_issues": sum(len(r.get("issues", [])) for r in reports),
            "no_filings_found": not bool(reports),
        },
        "filings": reports,
        "read_only_guarantee": "PostgreSQL transaction declared READ ONLY; no database data or schema was modified.",
    }
    json_text = json.dumps(result, indent=2, default=_as_jsonable)
    if args.output:
        out = Path(args.output)
        if not out.is_absolute():
            out = Path.cwd() / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json_text + "\n", encoding="utf-8")
        print(f"Audit report written to: {out}")
        print(json.dumps({"overall_status": overall, **result["summary"]}, indent=2))
    else:
        print(json_text)
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
