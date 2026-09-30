"""
discovery/filing_discovery.py

Discovery is strictly separated from parsing.

This module is responsible for:

    NSE Integrated Filing API
            ↓
       raw catalog rows
            ↓
       diagnostics
            ↓
       filing registry

It does NOT open or parse an XBRL document.

The exact response shape from NSE can change, so discovery intentionally
records the response keys and raw rows before mapping them into the
normalized filing registry.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

from config.settings import NSE
from sources.nse_source import NSEClient

logger = logging.getLogger("nse_pipeline.discovery")


class DiscoveryDiagnostics:
    """
    Accumulates a step-by-step trace of the discovery process.

    The goal is to make a zero-result discovery explainable instead of
    simply reporting "0 rows".
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.steps: list = []

    def log(self, label: str, **details):
        self.steps.append(
            {
                "label": label,
                **details,
            }
        )

        detail_str = ", ".join(
            f"{key}={value}"
            for key, value in details.items()
        )

        logger.info(
            "[discovery:%s] %s | %s",
            self.symbol,
            label,
            detail_str,
        )

    def render(self) -> str:
        lines = [
            f"Discovery trace for {self.symbol}:"
        ]

        for step in self.steps:
            detail_str = "  ".join(
                f"{key}={value}"
                for key, value in step.items()
                if key != "label"
            )

            lines.append(
                f"  {step['label']}: {detail_str}"
            )

        return "\n".join(lines)


def discover_integrated_filings(
    client: NSEClient,
    symbol: str,
    from_date: date,
    to_date: date,
) -> tuple:
    """
    Discover Integrated Filing - Financials records from NSE.

    Returns:

        (
            rows,
            diagnostics
        )

    `rows` contains the raw catalog dictionaries returned by NSE.

    Important:
    We do not discard rows simply because we do not yet understand every
    field. The diagnostics phase is intentionally conservative so that
    the actual NSE response can be inspected before finalizing the mapping.
    """

    diag = DiscoveryDiagnostics(symbol)

    # Current NSE Integrated Filing API request shape.
    params = {
        "type": "Integrated Filing- Financials",
        "symbol": symbol,
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
        "page": 1,
        "size": 100,
    }

    diag.log(
        "request",
        url=NSE.integrated_filings,
        params=params,
    )

    fetch_result = client.get(
        NSE.integrated_filings,
        params=params,
        is_api=True,
    )

    diag.log(
        "http_response",
        status=fetch_result.status_code,
        content_type=fetch_result.content_type,
        content_length=len(fetch_result.content or ""),
    )

    # ---------------------------------------------------------------
    # Parse JSON
    # ---------------------------------------------------------------

    try:
        payload = json.loads(fetch_result.content)

    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        diag.log(
            "parse_error",
            error=str(exc),
            raw_head=(fetch_result.content or "")[:1000],
        )

        return [], diag

    # ---------------------------------------------------------------
    # Identify response shape
    # ---------------------------------------------------------------

    rows = []

    if isinstance(payload, dict):

        diag.log(
            "response_shape",
            type="dict",
            keys=list(payload.keys())[:50],
        )

        # Try the common container names first.
        candidate_keys = [
            "data",
            "records",
            "results",
            "filings",
            "rows",
        ]

        for key in candidate_keys:
            value = payload.get(key)

            if isinstance(value, list):
                rows = value

                diag.log(
                    "rows_container",
                    key=key,
                    rows_found=len(rows),
                )

                break

        # Some APIs return a single object under data.
        if not rows and isinstance(payload.get("data"), dict):

            data_object = payload["data"]

            diag.log(
                "data_object_keys",
                keys=list(data_object.keys())[:50],
            )

            for key in candidate_keys:
                value = data_object.get(key)

                if isinstance(value, list):
                    rows = value

                    diag.log(
                        "nested_rows_container",
                        key=key,
                        rows_found=len(rows),
                    )

                    break

        if not rows:
            diag.log(
                "rows_container",
                key="none",
                rows_found=0,
            )

    elif isinstance(payload, list):

        rows = payload

        diag.log(
            "response_shape",
            type="list",
            rows_found=len(rows),
        )

    else:

        diag.log(
            "response_shape",
            type=str(type(payload)),
            rows_found=0,
        )

    # ---------------------------------------------------------------
    # Inspect actual keys
    # ---------------------------------------------------------------

    if rows:

        first_row = rows[0]

        if isinstance(first_row, dict):

            diag.log(
                "first_row_keys",
                keys=list(first_row.keys()),
            )

            # Log a compact representation of the first row.
            # This is extremely useful while validating the live NSE API.
            try:
                first_row_preview = json.dumps(
                    first_row,
                    ensure_ascii=False,
                    default=str,
                )[:3000]

            except Exception:
                first_row_preview = str(first_row)[:3000]

            diag.log(
                "first_row_preview",
                value=first_row_preview,
            )

    # ---------------------------------------------------------------
    # Symbol filtering
    # ---------------------------------------------------------------

    matching_symbol = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        row_symbol = (
            row.get("symbol")
            or row.get("Symbol")
            or row.get("SYMBOL")
        )

        if (
            row_symbol is not None
            and str(row_symbol).strip().upper()
            == symbol.upper()
        ):
            matching_symbol.append(row)

    diag.log(
        "rows_matching_symbol",
        count=len(matching_symbol),
    )

    # ---------------------------------------------------------------
    # XBRL detection
    # ---------------------------------------------------------------

    matching_with_xbrl = []

    for row in matching_symbol:

        xbrl_value = (
            row.get("xbrl")
            or row.get("XBRL")
            or row.get("xbrlFile")
            or row.get("xbrl_file")
            or row.get("xbrl_attachment")
            or row.get("xbrlUrl")
            or row.get("xbrlURL")
        )

        if xbrl_value:
            matching_with_xbrl.append(row)

    diag.log(
        "rows_with_xbrl_attachment",
        count=len(matching_with_xbrl),
    )

    # ---------------------------------------------------------------
    # IMPORTANT:
    #
    # For this first live validation, return the rows with XBRL.
    # We will finalize the field mapping after seeing the real response.
    # ---------------------------------------------------------------

    diag.log(
        "rows_selected",
        count=len(matching_with_xbrl),
    )

    return matching_with_xbrl, diag


def registry_entry_from_catalog_row(
    symbol: str,
    company_name: Optional[str],
    row: dict,
) -> dict:
    """
    Convert a raw NSE Integrated Filing catalog row into the internal
    nse_filing_registry structure.

    The live NSE Integrated Filing API currently exposes fields such as:

        symbol
        cmName
        qe_Date
        audited
        consolidated
        ixbrl
        xbrl
        broadcast_Date
        revised_Date
        revision_Remark
        pdf_attach

    For Phase 1:
        - ixbrl is the primary source URL because the parser consumes
          the iXBRL HTML document.
        - xbrl is retained inside catalog_json as the original XML URL.
    """

    # ---------------------------------------------------------------
    # Period end
    # ---------------------------------------------------------------

    period_end_raw = (
        row.get("qe_Date")
        or row.get("period_end")
        or row.get("periodEnd")
        or row.get("toDate")
        or row.get("quarterEndDate")
        or row.get("quarter_end_date")
        or row.get("relatingTo")
        or row.get("period")
    )

    period_end = _try_parse_date(period_end_raw)

    # ---------------------------------------------------------------
    # Symbol
    # ---------------------------------------------------------------

    resolved_symbol = (
        row.get("symbol")
        or row.get("Symbol")
        or row.get("SYMBOL")
        or symbol
    )

    # ---------------------------------------------------------------
    # Company
    # ---------------------------------------------------------------

    resolved_company_name = (
        company_name
        or row.get("cmName")
        or row.get("smName")
        or row.get("companyName")
        or row.get("Company Name")
        or row.get("company_name")
    )

    # ---------------------------------------------------------------
    # Statement type
    # ---------------------------------------------------------------

    statement_value = (
        row.get("consolidated")
        or row.get("Consolidated")
        or row.get("consolidatedStandalone")
        or row.get("statement_type")
        or row.get("statementType")
    )

    statement_type = _normalize_statement_type(
        statement_value
    )

    # ---------------------------------------------------------------
    # Submission type
    # ---------------------------------------------------------------

    submission_type = (
        row.get("submission_type")
        or row.get("submissionType")
        or row.get("typeOfSubmission")
        or row.get("Type of Submission")
        or row.get("audited")
        or row.get("Audited / Unaudited")
    )

    # ---------------------------------------------------------------
    # iXBRL + XML URLs
    # ---------------------------------------------------------------

    # ISSUE 7 / ISSUE 9 CORRECTION:
    # The live "ixbrl" field NSE returns points to a rendered IndAS-details
    # HTML page that contains ZERO inline-XBRL structures (no ix:header,
    # ix:nonFraction, ix:nonNumeric, xbrli:context — verified against the
    # actual RELIANCE filing 7 response). It is a presentation companion
    # document, not inline XBRL, despite its name/field key. The "xbrl"
    # field is the genuine machine-readable XBRL XML and is what
    # parsers.ixbrl_parser.parse_document() must be pointed at.
    #
    # This was reversed in an earlier version of this function (xbrl_url
    # was set to the ixbrl HTML). Do not revert this without re-confirming
    # the live response actually contains inline-XBRL tags.
    ixbrl_url = (
        row.get("ixbrl")
        or row.get("iXBRL")
        or row.get("ixbrlFile")
        or row.get("ixbrl_file")
        or row.get("ixbrl_attachment")
        or row.get("ixbrlUrl")
        or row.get("ixbrlURL")
    )

    xml_xbrl_url = (
        row.get("xbrl")
        or row.get("XBRL")
        or row.get("xbrlFile")
        or row.get("xbrl_file")
        or row.get("xbrl_attachment")
        or row.get("xbrlUrl")
        or row.get("xbrlURL")
    )

    # ---------------------------------------------------------------
    # Detail URL
    # ---------------------------------------------------------------

    detail_url = (
        row.get("detail_url")
        or row.get("detailUrl")
        or row.get("detailURL")
        or row.get("attachment")
        or row.get("details")
        or row.get("Details")
        or row.get("pdf_attach")
    )

    # ---------------------------------------------------------------
    # Period start
    # ---------------------------------------------------------------

    period_start_raw = (
        row.get("period_start")
        or row.get("periodStart")
        or row.get("fromDate")
    )

    # ---------------------------------------------------------------
    # Broadcast date
    # ---------------------------------------------------------------

    broadcast_raw = (
        row.get("broadcast_Date")
        or row.get("broadcast_date")
        or row.get("broadcastDate")
        or row.get("broadcastDateTime")
        or row.get("BROADCAST DATE/TIME")
    )

    # ---------------------------------------------------------------
    # Revision date
    # ---------------------------------------------------------------

    revision_date_raw = (
        row.get("revised_Date")
        or row.get("revision_date")
        or row.get("revisionDate")
        or row.get("revisedDate")
        or row.get("revisedDateTime")
        or row.get("Revised DATE/TIME")
    )

    # ---------------------------------------------------------------
    # Revision remarks
    # ---------------------------------------------------------------

    revision_remarks = (
        row.get("revision_Remark")
        or row.get("remarks")
        or row.get("revisionRemarks")
        or row.get("Revision Remarks")
    )

    # ---------------------------------------------------------------
    # Financial year
    # ---------------------------------------------------------------

    financial_year = (
        row.get("financial_year")
        or row.get("financialYear")
        or row.get("fy")
        or row.get("financialYearEnd")
    )

    # ---------------------------------------------------------------
    # Quarter
    # ---------------------------------------------------------------

    reporting_quarter = (
        row.get("quarter")
        or row.get("reportingQuarter")
        or row.get("period")
    )

    # ---------------------------------------------------------------
    # Preserve original NSE catalog row
    # ---------------------------------------------------------------

    catalog_json = dict(row)

    # Explicitly retain both URLs with clear names.
    catalog_json["ixbrl_url"] = ixbrl_url
    catalog_json["xml_xbrl_url"] = xml_xbrl_url

    # ---------------------------------------------------------------
    # Build registry entry
    # ---------------------------------------------------------------

    return {
        "company_id": None,

        "symbol": str(resolved_symbol).upper()
        if resolved_symbol
        else symbol,

        "company_name": resolved_company_name,

        "period_start_date": _try_parse_date(
            period_start_raw
        ),

        "period_end_date": period_end,

        "period_type": "quarterly",

        "reporting_quarter": reporting_quarter,

        "financial_year": financial_year,

        "statement_type": statement_type,

        "submission_type": submission_type,

        "cumulative": bool(
            row.get("cumulative", False)
        ),

        "accounting_standard": (
            row.get("accounting_standard")
            or row.get("accountingStandard")
            or "IND_AS"
        ),

        "broadcast_date": _try_parse_datetime(
            broadcast_raw
        ),

        "revision_date": _try_parse_datetime(
            revision_date_raw
        ),

        "revision_remarks": revision_remarks,

        # ISSUE 7: xbrl_url is now the authoritative machine-readable XML —
        # this is what gets downloaded and passed to parse_document().
        "xbrl_url": xml_xbrl_url,

        # Companion rendered-HTML document, retained separately (schema
        # column added — see db/schema.sql). Never fed to the financial
        # parser.
        "ixbrl_url": ixbrl_url,

        "detail_url": detail_url,

        "source_url": NSE.integrated_filings,

        "source_kind": "INTEGRATED_FILING_IXBRL",

        "filing_hash": None,

        # Preserve the complete original NSE response.
        "catalog_json": catalog_json,

        "discovery_status": "DISCOVERED",
    }

def _normalize_statement_type(value) -> str:
    """
    Normalize NSE's consolidated/standalone representation.

    We intentionally do not infer this from filenames.
    """

    if value is None:
        return "standalone"

    text = str(value).strip().lower()

    if "consolidated" in text:
        return "consolidated"

    if "standalone" in text:
        return "standalone"

    return "standalone"


def _try_parse_date(value):
    """
    Parse the common date representations encountered in NSE responses.
    """

    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    value = str(value).strip()

    formats = (
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%Y/%m/%d",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt,
            ).date()

        except ValueError:
            continue

    return None


def _try_parse_datetime(value):
    """
    Parse common NSE broadcast/revision timestamp formats.
    """

    if not value:
        return None

    if isinstance(value, datetime):
        return value

    value = str(value).strip()

    formats = (
        "%d-%b-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt,
            )

        except ValueError:
            continue

    return None