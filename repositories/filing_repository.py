"""
repositories/filing_repository.py

All writes to:
    - nse_filing_registry
    - raw_filings
    - xbrl_contexts
    - xbrl_facts

go through here using explicit parameterized SQL.

The filing registry upsert:
    1. Repairs an earlier incomplete discovery row when possible.
    2. Uses the existing natural-key conflict constraint.
    3. Updates filing metadata when the same filing is discovered again.
    4. Does not overwrite processing status fields during rediscovery.
"""

import json
from typing import Optional

from sqlalchemy import text


# ---------------------------------------------------------------------------
# Filing Registry
# ---------------------------------------------------------------------------

def upsert_filing_registry_entry(session, entry: dict) -> int:
    """
    Insert or update a filing registry entry.

    Required keys:
        symbol
        period_type
        statement_type
        source_kind

    Optional keys:
        company_id
        company_name
        period_start_date
        period_end_date
        reporting_quarter
        financial_year
        submission_type
        cumulative
        accounting_standard
        broadcast_date
        revision_date
        revision_remarks
        is_latest_revision
        supersedes_filing_id
        xbrl_url
        ixbrl_url
        detail_url
        source_url
        filing_hash
        catalog_json
        discovery_status

    IMPORTANT (ISSUE 7 — corrected from an earlier assumption):
        `xbrl_url` is the AUTHORITATIVE machine-readable XBRL XML URL —
        this is what parsers.ixbrl_parser.parse_document() must be pointed
        at for financial extraction. NSE's "iXBRL" HTML for Integrated
        Filings was found to contain zero inline-XBRL tags (no ix:header,
        ix:nonFraction, xbrli:context) — it is a plain presentation page,
        not inline XBRL despite the name. It is retained separately as
        `ixbrl_url` for reference/archival only and must NEVER be passed
        to the financial XBRL parser.
    """

    required_fields = {
        "symbol",
        "period_type",
        "statement_type",
        "source_kind",
    }

    missing = {
        field
        for field in required_fields
        if entry.get(field) is None
    }

    if missing:
        raise ValueError(
            f"Missing required filing registry fields: {sorted(missing)}"
        )

    # -----------------------------------------------------------------------
    # Preserve optional fields as explicit NULL when caller did not provide
    # them. This keeps NULL vs False/0 intentional.
    # -----------------------------------------------------------------------

    params = {
        "company_id": None,
        "symbol": None,
        "company_name": None,
        "period_start_date": None,
        "period_end_date": None,
        "period_type": None,
        "reporting_quarter": None,
        "financial_year": None,
        "statement_type": None,
        "submission_type": None,
        "cumulative": None,
        "accounting_standard": None,
        "broadcast_date": None,
        "revision_date": None,
        "revision_remarks": None,
        "xbrl_url": None,
        "ixbrl_url": None,
        "detail_url": None,
        "source_url": None,
        "source_kind": None,
        "filing_hash": None,
        "discovery_status": None,
    }

    params.update(entry)

    catalog_json = entry.get("catalog_json") or {}
    params["catalog_json"] = json.dumps(catalog_json)

    # -----------------------------------------------------------------------
    # Repair the first incomplete discovery row.
    #
    # The initial run may have stored:
    #     period_end_date = NULL
    #
    # while catalog_json already contained:
    #     qe_Date = "30-JUN-2026"
    #
    # Because PostgreSQL UNIQUE constraints allow multiple NULL values,
    # the corrected discovery could otherwise create another registry row.
    #
    # This repair looks for the old incomplete record and updates it before
    # the normal INSERT ... ON CONFLICT path.
    # -----------------------------------------------------------------------

    legacy_period_end_raw = (
        catalog_json.get("qe_Date")
        or catalog_json.get("periodEndDate")
        or catalog_json.get("period_end_date")
    )

    legacy_broadcast_raw = (
        catalog_json.get("broadcast_Date")
        or catalog_json.get("broadcastDate")
        or catalog_json.get("broadcast_date")
    )

    if params["period_end_date"] is not None and legacy_period_end_raw:

        repair_sql = text(
            """
            UPDATE nse_filing_registry
            SET
                company_id = :company_id,
                company_name = :company_name,
                period_start_date = :period_start_date,
                period_end_date = :period_end_date,
                period_type = :period_type,
                reporting_quarter = :reporting_quarter,
                financial_year = :financial_year,
                statement_type = :statement_type,
                submission_type = :submission_type,
                cumulative = :cumulative,
                accounting_standard = :accounting_standard,
                broadcast_date = :broadcast_date,
                revision_date = :revision_date,
                revision_remarks = :revision_remarks,
                xbrl_url = :xbrl_url,
                ixbrl_url = :ixbrl_url,
                detail_url = :detail_url,
                source_url = :source_url,
                filing_hash = :filing_hash,
                catalog_json = CAST(:catalog_json AS JSONB),
                discovery_status = :discovery_status,
                updated_at = now()
            WHERE filing_id = (
                SELECT filing_id
                FROM nse_filing_registry
                WHERE symbol = :symbol
                  AND period_type = :period_type
                  AND statement_type = :statement_type
                  AND source_kind = :source_kind
                  AND period_end_date IS NULL
                  AND catalog_json ->> 'qe_Date' = :legacy_period_end_raw
                  AND (
                        :legacy_broadcast_raw IS NULL
                        OR catalog_json ->> 'broadcast_Date' = :legacy_broadcast_raw
                  )
                ORDER BY filing_id ASC
                LIMIT 1
            )
            RETURNING filing_id
            """
        )

        repair_params = {
            **params,
            "legacy_period_end_raw": legacy_period_end_raw,
            "legacy_broadcast_raw": legacy_broadcast_raw,
        }

        repaired_result = session.execute(
            repair_sql,
            repair_params,
        )

        repaired_filing_id = repaired_result.scalar_one_or_none()

        if repaired_filing_id is not None:
            return repaired_filing_id

    # -----------------------------------------------------------------------
    # Normal insert/upsert.
    #
    # Existing natural key:
    #     symbol
    #     period_end_date
    #     period_type
    #     statement_type
    #     broadcast_date
    #     source_kind
    #
    # Processing statuses are deliberately NOT changed here.
    # Rediscovery should update metadata without resetting:
    #     download_status
    #     parse_status
    #     validation_status
    #     normalization_status
    # -----------------------------------------------------------------------

    sql = text(
        """
        INSERT INTO nse_filing_registry (
            company_id,
            symbol,
            company_name,
            period_start_date,
            period_end_date,
            period_type,
            reporting_quarter,
            financial_year,
            statement_type,
            submission_type,
            cumulative,
            accounting_standard,
            broadcast_date,
            revision_date,
            revision_remarks,
            xbrl_url,
            ixbrl_url,
            detail_url,
            source_url,
            source_kind,
            filing_hash,
            catalog_json,
            discovery_status
        )
        VALUES (
            :company_id,
            :symbol,
            :company_name,
            :period_start_date,
            :period_end_date,
            :period_type,
            :reporting_quarter,
            :financial_year,
            :statement_type,
            :submission_type,
            :cumulative,
            :accounting_standard,
            :broadcast_date,
            :revision_date,
            :revision_remarks,
            :xbrl_url,
            :ixbrl_url,
            :detail_url,
            :source_url,
            :source_kind,
            :filing_hash,
            CAST(:catalog_json AS JSONB),
            :discovery_status
        )

        ON CONFLICT (
            symbol,
            period_end_date,
            period_type,
            statement_type,
            broadcast_date,
            source_kind
        )

        DO UPDATE SET
            company_id = EXCLUDED.company_id,
            company_name = EXCLUDED.company_name,
            period_start_date = EXCLUDED.period_start_date,
            reporting_quarter = EXCLUDED.reporting_quarter,
            financial_year = EXCLUDED.financial_year,
            submission_type = EXCLUDED.submission_type,
            cumulative = EXCLUDED.cumulative,
            accounting_standard = EXCLUDED.accounting_standard,
            revision_date = EXCLUDED.revision_date,
            revision_remarks = EXCLUDED.revision_remarks,
            xbrl_url = EXCLUDED.xbrl_url,
            ixbrl_url = EXCLUDED.ixbrl_url,
            detail_url = EXCLUDED.detail_url,
            source_url = EXCLUDED.source_url,
            filing_hash = EXCLUDED.filing_hash,
            catalog_json = EXCLUDED.catalog_json,
            discovery_status = EXCLUDED.discovery_status,
            updated_at = now()

        RETURNING filing_id
        """
    )

    result = session.execute(sql, params)

    return result.scalar_one()


def get_filing_by_id(session, filing_id: int):
    """
    ISSUE 9: fetch a single registered filing's full row as a dict, needed
    by the end-to-end single-filing pipeline (which must never touch other
    discovered filings — ISSUE 11).
    """
    row = session.execute(
        text("SELECT * FROM nse_filing_registry WHERE filing_id = :filing_id"),
        {"filing_id": filing_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Filing Processing Status
# ---------------------------------------------------------------------------

def update_filing_status(
    session,
    filing_id: int,
    **status_fields,
):
    """
    Update processing status fields only.

    Allowed fields:
        download_status
        parse_status
        validation_status
        normalization_status
    """

    allowed = {
        "download_status",
        "parse_status",
        "validation_status",
        "normalization_status",
    }

    unknown = set(status_fields) - allowed

    if unknown:
        raise ValueError(
            f"Unknown status fields: {sorted(unknown)}"
        )

    if not status_fields:
        return

    set_clause = ", ".join(
        f"{field} = :{field}"
        for field in status_fields
    )

    sql = text(
        f"""
        UPDATE nse_filing_registry
        SET
            {set_clause},
            updated_at = now()
        WHERE filing_id = :filing_id
        """
    )

    session.execute(
        sql,
        {
            **status_fields,
            "filing_id": filing_id,
        },
    )


# ---------------------------------------------------------------------------
# Raw Filing
# ---------------------------------------------------------------------------

def insert_raw_filing(session, record: dict) -> int:
    """
    Insert a downloaded raw filing record.

    Duplicate downloads of the same filing/content hash are idempotent.
    """

    sql = text(
        """
        INSERT INTO raw_filings (
            filing_id,
            source_url,
            request_params,
            response_timestamp,
            http_status,
            content_type,
            content_hash,
            storage_path,
            parser_version
        )
        VALUES (
            :filing_id,
            :source_url,
            CAST(:request_params AS JSONB),
            :response_timestamp,
            :http_status,
            :content_type,
            :content_hash,
            :storage_path,
            :parser_version
        )

        ON CONFLICT (filing_id, content_hash)
        DO UPDATE SET
            storage_path = EXCLUDED.storage_path

        RETURNING raw_id
        """
    )

    params = dict(record)

    params["request_params"] = json.dumps(
        record.get("request_params") or {}
    )

    result = session.execute(sql, params)

    return result.scalar_one()


# ---------------------------------------------------------------------------
# XBRL Context
# ---------------------------------------------------------------------------

def insert_xbrl_context(
    session,
    filing_id: int,
    ctx,
) -> int:
    """
    Insert or update an XBRL context.
    """

    sql = text(
        """
        INSERT INTO xbrl_contexts (
            filing_id,
            context_ref,
            entity_identifier,
            period_start,
            period_end,
            instant_date,
            dimensions_json
        )
        VALUES (
            :filing_id,
            :context_ref,
            :entity_identifier,
            :period_start,
            :period_end,
            :instant_date,
            CAST(:dimensions_json AS JSONB)
        )

        ON CONFLICT (filing_id, context_ref)
        DO UPDATE SET
            entity_identifier = EXCLUDED.entity_identifier,
            period_start = EXCLUDED.period_start,
            period_end = EXCLUDED.period_end,
            instant_date = EXCLUDED.instant_date,
            dimensions_json = EXCLUDED.dimensions_json

        RETURNING context_id
        """
    )

    result = session.execute(
        sql,
        {
            "filing_id": filing_id,
            "context_ref": ctx.context_ref,
            "entity_identifier": ctx.entity_identifier,
            "period_start": ctx.period_start,
            "period_end": ctx.period_end,
            "instant_date": ctx.instant_date,
            "dimensions_json": json.dumps(
                ctx.dimensions or {}
            ),
        },
    )

    return result.scalar_one()


# ---------------------------------------------------------------------------
# XBRL Unit (ISSUE 9 — was missing; needed for full end-to-end persistence)
# ---------------------------------------------------------------------------

def insert_xbrl_unit(session, filing_id: int, unit) -> int:
    """
    Insert or update an XBRL unit declaration (xbrl_units table).
    `unit` is a parsers.ixbrl_parser.XbrlUnit (unit_ref, measure).
    """

    sql = text(
        """
        INSERT INTO xbrl_units (
            filing_id,
            unit_ref,
            measure
        )
        VALUES (
            :filing_id,
            :unit_ref,
            :measure
        )

        ON CONFLICT (filing_id, unit_ref)
        DO UPDATE SET
            measure = EXCLUDED.measure

        RETURNING unit_id
        """
    )

    result = session.execute(
        sql,
        {
            "filing_id": filing_id,
            "unit_ref": unit.unit_ref,
            "measure": unit.measure,
        },
    )

    return result.scalar_one()


# ---------------------------------------------------------------------------
# XBRL Fact
# ---------------------------------------------------------------------------

def insert_xbrl_fact(
    session,
    filing_id: int,
    context_id: int,
    fact,
):
    """
    Insert an XBRL fact.
    """

    sql = text(
        """
        INSERT INTO xbrl_facts (
            filing_id,
            context_id,
            namespace,
            concept,
            raw_tag,
            raw_value,
            numeric_value,
            unit_ref,
            decimals,
            scale,
            sign,
            dimensions_json
        )
        VALUES (
            :filing_id,
            :context_id,
            :namespace,
            :concept,
            :raw_tag,
            :raw_value,
            :numeric_value,
            :unit_ref,
            :decimals,
            :scale,
            :sign,
            CAST(:dimensions_json AS JSONB)
        )
        """
    )

    session.execute(
        sql,
        {
            "filing_id": filing_id,
            "context_id": context_id,
            "namespace": fact.namespace,
            "concept": fact.concept,
            "raw_tag": fact.raw_tag,
            "raw_value": fact.raw_value,
            "numeric_value": fact.numeric_value,
            "unit_ref": fact.unit_ref,
            "decimals": fact.decimals,
            "scale": fact.scale,
            "sign": fact.sign,
            "dimensions_json": json.dumps({}),
        },
    )