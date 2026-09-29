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
from datetime import date

from config.settings import COVERAGE, HTTP, TEST_SYMBOLS
from db.connection import init_schema, session_scope

from discovery.filing_discovery import (
    discover_integrated_filings,
    registry_entry_from_catalog_row,
)

from repositories.filing_repository import (
    upsert_filing_registry_entry,
)

from sources.nse_source import (
    NSEClient,
    NSEFetchError,
)


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