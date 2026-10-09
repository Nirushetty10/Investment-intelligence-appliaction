import json
"""
repositories/financial_repository.py

Writes normalized statements. Uses explicit column lists (never SELECT *
or **kwargs-into-columns) so a typo'd or unexpected key fails loudly
instead of silently landing in the wrong column.
"""
from sqlalchemy import text


def upsert_financial_period(session, record: dict) -> int:
    # Older callers remain safe by defaulting existing/unassessed periods to
    # PROVISIONAL; no row is trusted merely because it predates this gate.
    record = dict(record)
    record.setdefault("trust_status", "PROVISIONAL")
    record.setdefault("taxonomy_id", None)
    record.setdefault("taxonomy_version", None)
    record.setdefault("taxonomy_namespace", None)
    record.setdefault("taxonomy_source_package_version", None)
    record.setdefault("taxonomy_catalog_status", "TAXONOMY_UNRESOLVED")
    record.setdefault("canonical_mapping_enabled", False)
    record["source_schema_refs_json"] = json.dumps(record.pop("source_schema_refs", []) or [])
    record["trust_reasons_json"] = json.dumps(record.pop("trust_reasons", []) or [])
    sql = text(
        """
        INSERT INTO financial_periods (
            company_id, period_type, statement_type, period_start_date,
            period_end_date, financial_year, financial_quarter,
            source_filing_id, source, trust_status,
            taxonomy_id, taxonomy_version, taxonomy_namespace,
            taxonomy_source_package_version, taxonomy_catalog_status,
            canonical_mapping_enabled, source_schema_refs, trust_reasons
        ) VALUES (
            :company_id, :period_type, :statement_type, :period_start_date,
            :period_end_date, :financial_year, :financial_quarter,
            :source_filing_id, :source, :trust_status,
            :taxonomy_id, :taxonomy_version, :taxonomy_namespace,
            :taxonomy_source_package_version, :taxonomy_catalog_status,
            :canonical_mapping_enabled, CAST(:source_schema_refs_json AS JSONB),
            CAST(:trust_reasons_json AS JSONB)
        )
        ON CONFLICT (company_id, period_type, statement_type, period_end_date)
        DO UPDATE SET
            source_filing_id = EXCLUDED.source_filing_id,
            source = EXCLUDED.source,
            trust_status = EXCLUDED.trust_status,
            taxonomy_id = EXCLUDED.taxonomy_id,
            taxonomy_version = EXCLUDED.taxonomy_version,
            taxonomy_namespace = EXCLUDED.taxonomy_namespace,
            taxonomy_source_package_version = EXCLUDED.taxonomy_source_package_version,
            taxonomy_catalog_status = EXCLUDED.taxonomy_catalog_status,
            canonical_mapping_enabled = EXCLUDED.canonical_mapping_enabled,
            source_schema_refs = EXCLUDED.source_schema_refs,
            trust_reasons = EXCLUDED.trust_reasons,
            updated_at = now()
        RETURNING period_id
        """
    )
    result = session.execute(sql, record)
    return result.scalar_one()


_INCOME_STATEMENT_COLUMNS = [
    "revenue_from_operations", "other_operating_income", "other_income", "total_income",
    "cost_of_materials", "purchases", "changes_in_inventory", "employee_benefit_expense",
    "finance_cost", "depreciation", "amortization", "other_expenses", "total_expenses",
    "operating_profit", "profit_before_tax", "current_tax", "deferred_tax", "total_tax",
    "profit_after_tax", "profit_for_period", "other_comprehensive_income",
    "total_comprehensive_income", "basic_eps", "diluted_eps", "eps_face_value",
]


def upsert_income_statement(session, period_id: int, fields: dict, unit: str):
    columns = [c for c in _INCOME_STATEMENT_COLUMNS]
    params = {c: fields.get(c) for c in columns}
    params["period_id"] = period_id
    params["unit"] = unit

    col_list = ", ".join(columns)
    val_list = ", ".join(f":{c}" for c in columns)
    update_list = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)

    sql = text(
        f"""
        INSERT INTO income_statement (period_id, {col_list}, unit)
        VALUES (:period_id, {val_list}, :unit)
        ON CONFLICT (period_id) DO UPDATE SET
            {update_list}, unit = EXCLUDED.unit, updated_at = now()
        """
    )
    session.execute(sql, params)


_BALANCE_SHEET_COLUMNS = [
    "equity_share_capital", "other_equity", "long_term_borrowings",
    "other_long_term_liabilities", "deferred_tax_liabilities", "long_term_provisions",
    "short_term_borrowings", "trade_payables", "other_current_liabilities",
    "short_term_provisions", "total_liabilities", "property_plant_equipment",
    "capital_work_in_progress", "goodwill", "intangible_assets",
    "non_current_investments", "deferred_tax_assets", "other_non_current_assets",
    "current_investments", "inventories", "trade_receivables",
    "cash_and_cash_equivalents", "bank_balances", "other_current_assets",
    "total_assets",
]


def upsert_balance_sheet(session, period_id: int, fields: dict, unit: str):
    """
    ISSUE 2 (mapping-coverage fix): only ever called by the pipeline when
    NormalizationResult.balance_sheet_coverage == AVAILABLE (i.e. the
    filing actually contains a total_assets figure) — this function itself
    does not enforce that, so callers must check coverage first. It never
    fabricates zeros: any column not present in `fields` is written as NULL.
    """
    columns = list(_BALANCE_SHEET_COLUMNS)
    params = {c: fields.get(c) for c in columns}
    params["period_id"] = period_id
    params["unit"] = unit

    col_list = ", ".join(columns)
    val_list = ", ".join(f":{c}" for c in columns)
    update_list = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)

    sql = text(
        f"""
        INSERT INTO balance_sheet (period_id, {col_list}, unit)
        VALUES (:period_id, {val_list}, :unit)
        ON CONFLICT (period_id) DO UPDATE SET
            {update_list}, unit = EXCLUDED.unit, updated_at = now()
        """
    )
    session.execute(sql, params)


_CASHFLOW_COLUMNS = [
    "cfo", "cfi", "cff", "net_change_in_cash", "opening_cash_balance",
    "closing_cash_balance", "depreciation_addback", "working_capital_changes",
    "interest_paid", "tax_paid", "purchase_of_ppe", "sale_of_ppe",
    "investments_net", "borrowings", "repayments", "dividends_paid",
]


def upsert_cashflow_statement(session, period_id: int, fields: dict, unit: str):
    """Same contract as upsert_balance_sheet: caller must gate this on
    cashflow_statement_coverage == AVAILABLE; never fabricates zeros."""
    columns = list(_CASHFLOW_COLUMNS)
    params = {c: fields.get(c) for c in columns}
    params["period_id"] = period_id
    params["unit"] = unit

    col_list = ", ".join(columns)
    val_list = ", ".join(f":{c}" for c in columns)
    update_list = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)

    sql = text(
        f"""
        INSERT INTO cashflow_statement (period_id, {col_list}, unit)
        VALUES (:period_id, {val_list}, :unit)
        ON CONFLICT (period_id) DO UPDATE SET
            {update_list}, unit = EXCLUDED.unit, updated_at = now()
        """
    )
    session.execute(sql, params)


def insert_ratio(session, period_id: int, ratio: dict):
    """
    ISSUE 6: `ratio` may include a `needs_validation` key (set by the
    caller from validators.financial_validator.validate_ratio_plausibility
    — see nse_financials_pipeline.process_filing). Defaults to False when
    absent so every existing call site that doesn't know about this flag
    still works unchanged. The raw `value` is never altered based on this
    flag — see the needs_validation column comment in db/schema.sql.
    """
    sql = text(
        """
        INSERT INTO ratios (period_id, ratio_name, value, unit, source_concept, needs_validation)
        VALUES (:period_id, :ratio_name, :value, :unit, :source_concept, :needs_validation)
        ON CONFLICT (period_id, ratio_name) DO UPDATE SET
            value = EXCLUDED.value, unit = EXCLUDED.unit, source_concept = EXCLUDED.source_concept,
            needs_validation = EXCLUDED.needs_validation
        """
    )
    params = {"period_id": period_id, "needs_validation": False, **ratio}
    session.execute(sql, params)


def get_validated_ratios(session, period_id: int):
    """
    ISSUE 6: "prevent unvalidated ratios from being used by downstream
    fundamental/ML features" — this is the enforcement point. Any future
    derived-metrics/ML feature pipeline MUST read ratios through this
    function (or an equivalent explicit `WHERE needs_validation = FALSE`
    filter), never through a bare `SELECT * FROM ratios`, which would
    silently include ratios flagged as scale-suspicious
    (see validators.financial_validator.validate_ratio_plausibility).
    """
    sql = text(
        """
        SELECT ratio_name, value, unit, source_concept
        FROM ratios
        WHERE period_id = :period_id AND needs_validation = FALSE
        """
    )
    return session.execute(sql, {"period_id": period_id}).mappings().all()


def insert_data_quality_log(session, record: dict):
    sql = text(
        """
        INSERT INTO data_quality_log (filing_id, period_id, check_name, severity, message, details_json)
        VALUES (:filing_id, :period_id, :check_name, :severity, :message, CAST(:details_json AS JSONB))
        """
    )
    import json

    session.execute(sql, {**record, "details_json": json.dumps(record.get("details_json") or {})})
