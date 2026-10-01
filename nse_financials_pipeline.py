#!/usr/bin/env python3

"""
nse_financials_pipeline.py — CLI entrypoint.

Usage:

    python nse_financials_pipeline.py --init-db

    python nse_financials_pipeline.py \
        --symbols RELIANCE \
        --start-year 2026 \
        --end-year 2026 \
        --discovery-only

    python nse_financials_pipeline.py \
        --symbols RELIANCE,TCS,INFY \
        --start-year 2025 \
        --end-year 2026

Flags:

    --delay N
        Seconds between HTTP requests.

    --retry N
        Maximum retries per request.

    --workers N
        Parallelism.

    --start-year
        First year to discover.

    --end-year
        Last year to discover.

    --symbols
        Comma-separated NSE symbols.

    --all
        Full NSE universe.
        Not enabled for the current Phase-1 scaffold.

    --test-set
        Use the predefined test symbol set.

    --resume
        Resume existing ingestion.

    --force
        Re-process already processed filings.

    --validate-only
        Re-run validation.

    --discovery-only
        Only run discovery and populate the filing registry.

    --init-db
        Initialize PostgreSQL schema and EXIT.
"""

import argparse
import logging
import sys
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text

from config.settings import COVERAGE, HTTP, TEST_SYMBOLS
from db.connection import init_schema, session_scope

from discovery.filing_discovery import (
    discover_integrated_filings,
    registry_entry_from_catalog_row,
)

from parsers.ixbrl_parser import parse_document
from normalizers.financial_normalizer import normalize
from validators.financial_validator import (
    overall_status,
    validate_income_statement,
    validate_ratio_plausibility,
)

from repositories.filing_repository import (
    upsert_filing_registry_entry,
    get_filing_by_id,
    update_filing_status,
    insert_raw_filing,
    insert_xbrl_context,
    insert_xbrl_unit,
    insert_xbrl_fact,
)
from repositories.financial_repository import (
    upsert_financial_period,
    upsert_income_statement,
    upsert_balance_sheet,
    upsert_cashflow_statement,
    insert_ratio,
    insert_data_quality_log,
)
from repositories.company_repository import get_or_create_company
from reporting.filing_coverage import build_coverage_report, format_coverage_report

# ISSUE 1/2 fix (filing_id=9): period_start, period_type, financial_quarter
# and financial_year are now resolved TOGETHER by one canonical resolver
# instead of independently/registry-blindly. _derive_period_start_from_document
# and _quarter_code_from_dates are re-exported here (not redefined) so
# existing code / tests that do
# `from nse_financials_pipeline import _derive_period_start_from_document`
# keep working completely unchanged — resolve_canonical_period is the new
# entry point process_filing() actually uses.
from resolvers.period_resolver import (
    ResolvedPeriod,
    resolve_canonical_period,
    _derive_period_start_from_document,
    _quarter_code_from_dates,
)

from sources.nse_source import (
    NSEClient,
    NSEFetchError,
)

PARSER_VERSION = "phase1-1.0.0"


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(name)s: "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "nse_pipeline.cli"
)


def parse_args():
    """
    Parse CLI arguments.
    """

    parser = argparse.ArgumentParser(
        description=(
            "NSE financial data ingestion pipeline"
        )
    )

    parser.add_argument(
        "--symbols",
        type=str,
        help=(
            "Comma-separated NSE symbols, "
            "e.g. RELIANCE,TCS"
        ),
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Run the full NSE universe. "
            "Not available until Phase 1-5 validation "
            "is complete."
        ),
    )

    parser.add_argument(
        "--test-set",
        action="store_true",
        help=(
            "Use the Phase-1..4 test symbol set: "
            f"{TEST_SYMBOLS}"
        ),
    )

    parser.add_argument(
        "--start-year",
        type=int,
        default=int(
            COVERAGE.start_date[:4]
        ),
    )

    parser.add_argument(
        "--end-year",
        type=int,
        default=date.today().year,
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=HTTP.default_delay_seconds,
    )

    parser.add_argument(
        "--retry",
        type=int,
        default=HTTP.max_retries,
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=HTTP.max_workers,
    )

    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
    )

    parser.add_argument(
        "--force",
        action="store_true",
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
    )

    parser.add_argument(
        "--discovery-only",
        action="store_true",
    )

    parser.add_argument(
        "--filing-id",
        type=int,
        default=None,
        help=(
            "ISSUE 11: process exactly ONE already-registered filing "
            "end-to-end (download -> raw storage -> parse -> normalize -> "
            "validate -> store) and then exit. Does not run discovery and "
            "does not touch any other filing, even other filings for the "
            "same symbol. Use this for the controlled Phase 1 validation, "
            "e.g. --filing-id 7."
        ),
    )

    parser.add_argument(
        "--init-db",
        action="store_true",
        help=(
            "Initialize PostgreSQL schema and "
            "stop without starting ingestion."
        ),
    )

    return parser.parse_args()


def resolve_symbols(args) -> list:
    """
    Resolve the requested symbol set.
    """

    if args.symbols:

        return [
            symbol.strip().upper()
            for symbol in args.symbols.split(",")
            if symbol.strip()
        ]

    if args.test_set:
        return TEST_SYMBOLS

    if args.all:

        raise NotImplementedError(
            "Phase 6 (full universe) requires the "
            "company master table to be populated first "
            "and the Phase 1-5 checklist to be satisfied."
        )

    # Phase 1 default.
    return TEST_SYMBOLS[:1]


def run_discovery_for_symbol(
    client: NSEClient,
    symbol: str,
    start_year: int,
    end_year: int,
    session,
    run_id,
):
    """
    Run Integrated Filing discovery for one symbol.
    """

    from_date = date(
        start_year,
        1,
        1,
    )

    to_date = date(
        end_year,
        12,
        31,
    )

    logger.info(
        "[run=%s] Starting discovery for %s "
        "(%s -> %s)",
        run_id,
        symbol,
        from_date,
        to_date,
    )

    try:

        rows, diagnostics = (
            discover_integrated_filings(
                client=client,
                symbol=symbol,
                from_date=from_date,
                to_date=to_date,
            )
        )

    except NSEFetchError as exc:

        logger.error(
            "Discovery failed for %s: %s",
            symbol,
            exc,
        )

        return

    # Always render the complete diagnostics.
    logger.info(
        "\n%s",
        diagnostics.render(),
    )

    if not rows:

        logger.warning(
            (
                "No filings discovered for %s "
                "in %d-%d. "
                "Review the discovery diagnostics above."
            ),
            symbol,
            start_year,
            end_year,
        )

        return

    logger.info(
        "Found %d candidate XBRL filing(s) for %s",
        len(rows),
        symbol,
    )

    # ---------------------------------------------------------------
    # Register discovered filings.
    # ---------------------------------------------------------------

    for row in rows:

        entry = registry_entry_from_catalog_row(
            symbol=symbol,
            company_name=None,
            row=row,
        )

        filing_id = (
            upsert_filing_registry_entry(
                session,
                entry,
            )
        )

        logger.info(
            (
                "Registered filing_id=%s "
                "symbol=%s "
                "period_end=%s "
                "statement_type=%s "
                "submission_type=%s "
                "xbrl_url=%s"
            ),
            filing_id,
            symbol,
            entry.get("period_end_date"),
            entry.get("statement_type"),
            entry.get("submission_type"),
            entry.get("xbrl_url"),
        )



def process_filing(client: NSEClient, session, filing_id: int) -> dict:
    """
    ISSUE 8 / ISSUE 9: complete single-filing pipeline.

        registry row
            -> download (xbrl_url — the machine-readable XML, NEVER
               ixbrl_url — see ISSUE 7)
            -> raw_filings (exact bytes preserved, sha256 recorded)
            -> parse_document()
            -> xbrl_contexts / xbrl_units / xbrl_facts
            -> normalize()
            -> validate() (+ data_quality_log)
            -> financial_periods / income_statement / ratios

    Every stage updates its own status column and returns immediately on
    failure — a later stage is never attempted, and never marked SUCCESS,
    if an earlier one failed (ISSUE 9: "Never mark a stage successful when
    that stage failed"). ISSUE 11: this function touches exactly the one
    filing_id it is given; it never queries or loops over sibling filings
    for the same symbol/company.
    """
    summary = {"filing_id": filing_id}

    filing = get_filing_by_id(session, filing_id)
    if filing is None:
        summary["error"] = f"No such filing_id={filing_id} in nse_filing_registry"
        return summary

    summary.update(
        {
            "symbol": filing["symbol"],
            "period_end_date": filing["period_end_date"],
            "statement_type_registered": filing["statement_type"],
        }
    )

    xbrl_url = filing.get("xbrl_url")
    if not xbrl_url:
        update_filing_status(session, filing_id, download_status="FAILED")
        summary["error"] = (
            "No xbrl_url (machine-readable XML) registered for this filing "
            "— refusing to fall back to ixbrl_url (presentation HTML)."
        )
        logger.error(summary["error"])
        return summary

    # ---------------- DOWNLOAD ----------------
    try:
        fetch_result = client.get(xbrl_url, is_api=False)
    except NSEFetchError as exc:
        update_filing_status(session, filing_id, download_status="FAILED")
        summary["error"] = f"Download failed: {exc}"
        logger.error("filing_id=%s download failed: %s", filing_id, exc)
        return summary

    raw_path = fetch_result.save(
        subdir=f"xbrl/{filing['symbol']}", filename=f"filing_{filing_id}.xml"
    )
    insert_raw_filing(
        session,
        {
            "filing_id": filing_id,
            "source_url": xbrl_url,
            "request_params": None,
            "response_timestamp": datetime.fromisoformat(fetch_result.response_timestamp),
            "http_status": fetch_result.status_code,
            "content_type": fetch_result.content_type or "application/xml",
            "content_hash": fetch_result.content_hash,
            "storage_path": str(raw_path),
            "parser_version": PARSER_VERSION,
        },
    )
    update_filing_status(session, filing_id, download_status="SUCCESS")
    summary["raw_xml_path"] = str(raw_path)
    summary["raw_xml_sha256"] = fetch_result.content_hash

    # Best-effort companion HTML (ISSUE 8: "retain separately when
    # practical") — failure here must NOT fail the pipeline; the
    # machine-readable XML is what matters for download_status.
    ixbrl_url = filing.get("ixbrl_url")
    if ixbrl_url:
        try:
            html_result = client.get(ixbrl_url, is_api=False)
            html_path = html_result.save(
                subdir=f"ixbrl_html/{filing['symbol']}", filename=f"filing_{filing_id}.html"
            )
            insert_raw_filing(
                session,
                {
                    "filing_id": filing_id,
                    "source_url": ixbrl_url,
                    "request_params": None,
                    "response_timestamp": datetime.fromisoformat(html_result.response_timestamp),
                    "http_status": html_result.status_code,
                    "content_type": html_result.content_type or "text/html",
                    "content_hash": html_result.content_hash,
                    "storage_path": str(html_path),
                    "parser_version": PARSER_VERSION,
                },
            )
            summary["companion_html_path"] = str(html_path)
        except NSEFetchError as exc:
            logger.warning(
                "filing_id=%s companion iXBRL HTML download failed (non-fatal): %s",
                filing_id, exc,
            )
            summary["companion_html_error"] = str(exc)

    # ---------------- PARSE ----------------
    # NEVER fall back to scraping displayed HTML numbers if this fails.
    try:
        doc = parse_document(fetch_result.content)
    except ValueError as exc:
        update_filing_status(session, filing_id, parse_status="FAILED")
        summary["error"] = f"Parse failed: {exc}"
        logger.error("filing_id=%s parse failed: %s", filing_id, exc)
        return summary
    update_filing_status(session, filing_id, parse_status="SUCCESS")

    # ISSUE 10 (idempotency, extended to reprocessing): xbrl_facts has no
    # natural unique key (a filing can legitimately have the same concept
    # tagged under multiple contexts, so no single-column/pair constraint
    # would be correct). Without this, re-running process_filing() for the
    # same filing_id (e.g. retrying after a transient failure, or manual
    # re-validation) would silently duplicate every fact row on each run.
    # Rather than add a schema constraint, we make reprocessing a clean
    # replace: wipe this filing's previously stored facts/contexts/units
    # before re-inserting the freshly parsed set. xbrl_contexts/xbrl_units
    # already have real ON CONFLICT upserts, so this only changes behavior
    # for the fact rows in practice, but doing all three together keeps
    # foreign keys consistent within one filing's data.
    session.execute(text("DELETE FROM xbrl_facts WHERE filing_id = :fid"), {"fid": filing_id})
    session.execute(text("DELETE FROM xbrl_contexts WHERE filing_id = :fid"), {"fid": filing_id})
    session.execute(text("DELETE FROM xbrl_units WHERE filing_id = :fid"), {"fid": filing_id})

    context_id_map = {}
    for context_ref, ctx in doc.contexts.items():
        context_id_map[context_ref] = insert_xbrl_context(session, filing_id, ctx)
    for unit in doc.units.values():
        insert_xbrl_unit(session, filing_id, unit)
    stored_fact_count = 0
    for fact in doc.facts:
        context_id = context_id_map.get(fact.context_ref)
        if context_id is None:
            # Should not happen (every fact's contextRef must resolve to a
            # parsed context) — if it does, it's a real parser bug, log it
            # loudly rather than skip silently.
            logger.error(
                "filing_id=%s fact references unknown contextRef=%s — skipping, "
                "this indicates a parser bug and should be investigated",
                filing_id, fact.context_ref,
            )
            continue
        insert_xbrl_fact(session, filing_id, context_id, fact)
        stored_fact_count += 1

    summary["contexts_stored"] = len(context_id_map)
    summary["units_stored"] = len(doc.units)
    summary["facts_stored"] = stored_fact_count
    summary["source_format"] = doc.source_format

    # ---------------- RESOLVE CANONICAL PERIOD (ISSUE 1/2 fix) ----------------
    # period_start, period_type, financial_quarter and financial_year are
    # now resolved TOGETHER from the filing's own XBRL contexts, with
    # registry metadata (period_type, period_start_date, submission_type)
    # used as corroborating/cross-checked signals rather than a blind
    # override — see resolvers/period_resolver.py for the full reasoning.
    # This is what fixes filing_id=9 (registry says "quarterly", but an
    # audited filing + an annual-length duration context in its own XBRL
    # means the canonical period is annual).
    period_end = filing["period_end_date"]
    resolved = resolve_canonical_period(doc, filing)
    period_start = resolved.period_start

    if resolved.conflict:
        logger.warning("filing_id=%s period-resolution conflict: %s", filing_id, resolved.resolution_reason)
    else:
        logger.info("filing_id=%s period resolution: %s", filing_id, resolved.resolution_reason)

    if period_start is None or resolved.period_type is None:
        update_filing_status(session, filing_id, normalization_status="PARTIAL")
        summary["error"] = (
            "period_start_date and/or period_type could not be unambiguously resolved "
            f"from this filing's own XBRL contexts or registry metadata: {resolved.resolution_reason} "
            "Normalized period cannot be stored without inventing a date/type. "
            "Context/fact/unit rows ARE stored (see contexts_stored/facts_stored above); "
            "only the financial_periods/income_statement/ratios persistence step was skipped."
        )
        logger.error("filing_id=%s: %s", filing_id, summary["error"])
        return summary

    try:
        result = normalize(doc, period_end=period_end, period_start=period_start)
    except Exception as exc:  # noqa: BLE001 — must record, not crash the run
        update_filing_status(session, filing_id, normalization_status="FAILED")
        summary["error"] = f"Normalization failed: {exc}"
        logger.error("filing_id=%s normalization failed: %s", filing_id, exc)
        return summary
    # ISSUE 8 fix: normalization_status is NOT set to SUCCESS here anymore.
    # normalize() succeeding only means the in-memory computation worked —
    # it says nothing about whether financial_periods/income_statement/
    # balance_sheet/cashflow_statement/ratios actually got persisted. The
    # status is now set only after the STORE block below completes without
    # exception (see "STORE normalized", further down).
    summary["statement_type_resolved"] = result.period.statement_type
    summary["unmapped_fact_count"] = result.unmapped_fact_count
    summary["period_type_resolved"] = resolved.period_type
    summary["period_type_registry"] = resolved.registry_period_type
    summary["period_resolution_conflict"] = resolved.conflict
    summary["period_resolution_reason"] = resolved.resolution_reason

    # Idempotent reprocessing (same reasoning as the xbrl_facts wipe
    # earlier): data_quality_log has no unique constraint, so reprocessing
    # this filing would otherwise duplicate every log entry on each run.
    # This single cleanup covers BOTH the period-resolution-conflict entry
    # below and the validation-issue entries further down — moved here
    # (once, up front) rather than repeated per-section.
    session.execute(text("DELETE FROM data_quality_log WHERE filing_id = :fid"), {"fid": filing_id})

    # ISSUE 6: record registry-vs-source disagreement for audit, without
    # ever overwriting the registry's own period_type/period_start_date
    # columns in nse_filing_registry (those stay exactly as discovery
    # wrote them — only this log entry and the financial_periods row use
    # the resolved/canonical value).
    if resolved.conflict:
        insert_data_quality_log(
            session,
            {
                "filing_id": filing_id,
                "period_id": None,
                "check_name": "period_resolution_conflict",
                "severity": "WARNING",
                "message": resolved.resolution_reason,
                "details_json": {
                    "registry_period_type": resolved.registry_period_type,
                    "registry_period_start": str(resolved.registry_period_start) if resolved.registry_period_start else None,
                    "resolved_period_type": resolved.period_type,
                    "resolved_period_start": str(resolved.period_start) if resolved.period_start else None,
                },
            },
        )

    # ---------------- VALIDATE ----------------
    issues = validate_income_statement(result.income_statement)
    issues += validate_ratio_plausibility(result.ratios)
    status = overall_status(issues)
    update_filing_status(session, filing_id, validation_status=status)
    summary["validation_status"] = status
    summary["validation_issues"] = [
        {"check": i.check_name, "severity": i.severity, "message": i.message} for i in issues
    ]

    # (data_quality_log for this filing_id was already cleared once, up
    # above, right before the period-resolution-conflict entry — not
    # repeated here, or it would delete that entry too.)
    for issue in issues:
        insert_data_quality_log(
            session,
            {
                "filing_id": filing_id,
                "period_id": None,
                "check_name": issue.check_name,
                "severity": issue.severity,
                "message": issue.message,
                "details_json": {},
            },
        )

    # ---------------- STORE normalized (ISSUE 8: atomic + status-after-commit) ----------------
    # Everything in this block is wrapped in one SAVEPOINT (session.begin_nested)
    # so that a failure partway through (e.g. income_statement insert
    # succeeds but a later balance_sheet/ratio insert throws) rolls back
    # ONLY this filing's normalized-persistence attempt — not the raw
    # xbrl_contexts/xbrl_facts/raw_filings rows already staged earlier in
    # this same session for this filing (ISSUE 12: never discard raw data
    # as a side effect of a later failure). normalization_status is set to
    # SUCCESS only after this block completes without exception; on
    # failure it is FAILED, and NOTHING here is left half-persisted inside
    # the database (the savepoint rolls back cleanly).
    period_id = None
    company_id = None
    coverage_report = None
    persistence_exc = None

    try:
        with session.begin_nested():
            company_id = get_or_create_company(session, filing["symbol"], filing.get("company_name"))
            period_id = upsert_financial_period(
                session,
                {
                    "company_id": company_id,
                    # Canonical, source-cross-checked period_type — NOT
                    # filing["period_type"] (the raw, possibly-wrong
                    # registry value). This is the actual fix for
                    # filing_id=9 persisting as "quarterly".
                    "period_type": resolved.period_type,
                    "statement_type": result.period.statement_type or filing["statement_type"],
                    "period_start_date": period_start,
                    "period_end_date": period_end,
                    "financial_year": resolved.financial_year,
                    "financial_quarter": resolved.financial_quarter,
                    "source_filing_id": filing_id,
                    "source": filing["source_kind"],
                },
            )
            upsert_income_statement(session, period_id, result.income_statement, result.period.unit)
            # ISSUE 6: flag scale-suspicious ratios (e.g. DebtEquityRatio
            # tagged ~100x smaller than NSE's own presentation HTML shows
            # for the same filing) as needs_validation=True, WITHOUT
            # altering the preserved raw value itself. `issues` already
            # contains the ratio_scale_plausibility findings from the
            # VALIDATE step above (via validate_ratio_plausibility); each
            # carries a structured `subject` (the ratio_name) rather than
            # requiring message-text parsing. Any downstream fundamental/
            # ML feature pipeline must read ratios via
            # repositories.financial_repository.get_validated_ratios
            # (which filters needs_validation=TRUE rows out), never a bare
            # SELECT * FROM ratios.
            flagged_ratio_names = {
                issue.subject for issue in issues
                if issue.check_name == "ratio_scale_plausibility" and issue.subject
            }
            for ratio in result.ratios:
                ratio_with_flag = dict(ratio)
                ratio_with_flag["needs_validation"] = ratio["ratio_name"] in flagged_ratio_names
                insert_ratio(session, period_id, ratio_with_flag)

            # ISSUE 2 (mapping-coverage fix): only persist balance_sheet /
            # cashflow_statement when the filing actually reports them.
            if result.balance_sheet_coverage == "AVAILABLE":
                upsert_balance_sheet(session, period_id, result.balance_sheet, result.period.unit)
            if result.cashflow_statement_coverage == "AVAILABLE":
                upsert_cashflow_statement(session, period_id, result.cashflow_statement, result.period.unit)
    except Exception as exc:  # noqa: BLE001 — must record, not crash the run
        persistence_exc = exc

    if persistence_exc is not None:
        update_filing_status(session, filing_id, normalization_status="FAILED")
        summary["error"] = (
            f"Normalized persistence failed and was rolled back (financial_periods/"
            f"income_statement/balance_sheet/cashflow_statement/ratios for this filing "
            f"are NOT partially written): {persistence_exc}"
        )
        logger.error("filing_id=%s: %s", filing_id, summary["error"])
        return summary

    # Only now — after financial_periods, income_statement, ratios, and any
    # applicable balance_sheet/cashflow_statement rows are actually
    # persisted — is normalization considered SUCCESS.
    update_filing_status(session, filing_id, normalization_status="SUCCESS")

    # ISSUE 6: per-filing coverage report — makes source-data-availability
    # (AVAILABLE / NOT_REPORTED_IN_FILING) visibly separate from mapping
    # completeness (mapped / intentionally-unmapped / unmapped-financial
    # fact counts), so a reviewer never has to guess which one a number
    # like "108 unmapped" was actually describing.
    coverage_report = build_coverage_report(filing["symbol"], resolved, result)
    summary["coverage_report"] = format_coverage_report(coverage_report)
    summary["mapped_facts"] = coverage_report.mapped_facts
    summary["intentionally_unmapped_facts"] = coverage_report.intentionally_unmapped_facts
    summary["unmapped_financial_facts"] = coverage_report.unmapped_financial_facts
    summary["unmapped_financial_concepts"] = coverage_report.unmapped_financial_concepts
    summary["balance_sheet_status"] = result.balance_sheet_coverage
    summary["cashflow_status"] = result.cashflow_statement_coverage
    logger.info("Coverage report for filing_id=%s:\n%s", filing_id, summary["coverage_report"])

    summary["period_id"] = period_id
    summary["company_id"] = company_id
    summary["financial_year"] = resolved.financial_year
    summary["financial_quarter"] = resolved.financial_quarter
    summary["status"] = "COMPLETE"
    return summary


def main():
    """
    Main CLI entrypoint.
    """

    args = parse_args()

    # ---------------------------------------------------------------
    # Database initialization
    #
    # IMPORTANT:
    # --init-db now ONLY initializes the schema.
    # It does not accidentally start an ingestion run.
    # ---------------------------------------------------------------

    if args.init_db:

        logger.info(
            "Initializing PostgreSQL schema..."
        )

        init_schema()

        logger.info(
            "Schema initialization completed."
        )

        return

    # ---------------------------------------------------------------
    # ISSUE 11: controlled single-filing end-to-end run.
    #
    # This branch is checked BEFORE symbol resolution / discovery on
    # purpose: --filing-id must never trigger discovery for the symbol
    # (which could re-register or touch sibling filings), and must never
    # fall through into the multi-symbol loop below. It processes exactly
    # the one filing_id given and nothing else, then exits.
    # ---------------------------------------------------------------

    if args.filing_id is not None:

        logger.info(
            "Controlled single-filing run: filing_id=%s only. "
            "No discovery, no other filings will be touched.",
            args.filing_id,
        )

        client = NSEClient(
            delay_seconds=args.delay,
            max_retries=args.retry,
        )

        with session_scope() as session:
            summary = process_filing(client, session, args.filing_id)

        logger.info("Filing %s result: %s", args.filing_id, summary)

        if summary.get("status") != "COMPLETE":
            logger.error(
                "filing_id=%s did NOT complete successfully — see 'error' "
                "field above and the relevant *_status column in "
                "nse_filing_registry for where it stopped.",
                args.filing_id,
            )
            sys.exit(1)

        logger.info(
            "filing_id=%s completed: contexts=%s facts=%s units=%s "
            "statement_type=%s validation_status=%s period_id=%s",
            args.filing_id,
            summary.get("contexts_stored"),
            summary.get("facts_stored"),
            summary.get("units_stored"),
            summary.get("statement_type_resolved"),
            summary.get("validation_status"),
            summary.get("period_id"),
        )
        return

    # ---------------------------------------------------------------
    # Resolve symbols
    # ---------------------------------------------------------------

    symbols = resolve_symbols(args)

    run_id = uuid.uuid4()

    logger.info(
        (
            "Run %s starting "
            "for symbols=%s "
            "(%d-%d)"
        ),
        run_id,
        symbols,
        args.start_year,
        args.end_year,
    )

    # ---------------------------------------------------------------
    # NSE client
    # ---------------------------------------------------------------

    client = NSEClient(
        delay_seconds=args.delay,
        max_retries=args.retry,
    )

    # ---------------------------------------------------------------
    # Discovery
    # ---------------------------------------------------------------

    with session_scope() as session:

        for symbol in symbols:

            run_discovery_for_symbol(
                client=client,
                symbol=symbol,
                start_year=args.start_year,
                end_year=args.end_year,
                session=session,
                run_id=run_id,
            )

    # ---------------------------------------------------------------
    # Discovery-only mode
    # ---------------------------------------------------------------

    if args.discovery_only:

        logger.info(
            (
                "--discovery-only set: "
                "stopping after registry population."
            )
        )

        return

    # ---------------------------------------------------------------
    # Phase 1 safety boundary
    # ---------------------------------------------------------------

    logger.info(
        (
            "Download/parse/normalize/validate/store "
            "stages are intentionally not automatically "
            "executed yet."
        )
    )

    logger.info(
        (
            "Phase 1 requires successful validation of "
            "one real NSE filing before scaling to "
            "multiple filings."
        )
    )


if __name__ == "__main__":
    sys.exit(main())