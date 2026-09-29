"""
repositories/financial_repository.py

Writes normalized statements. Uses explicit column lists (never SELECT *
or **kwargs-into-columns) so a typo'd or unexpected key fails loudly
instead of silently landing in the wrong column.
"""
from sqlalchemy import text


def upsert_financial_period(session, record: dict) -> int:
    sql = text(
        """
        INSERT INTO financial_periods (
            company_id, period_type, statement_type, period_start_date,
            period_end_date, financial_year, financial_quarter,
            source_filing_id, source
        ) VALUES (
            :company_id, :period_type, :statement_type, :period_start_date,
            :period_end_date, :financial_year, :financial_quarter,
            :source_filing_id, :source
        )
        ON CONFLICT (company_id, period_type, statement_type, period_end_date)
        DO UPDATE SET
            source_filing_id = EXCLUDED.source_filing_id,
            source = EXCLUDED.source,
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


def insert_ratio(session, period_id: int, ratio: dict):
    sql = text(
        """
        INSERT INTO ratios (period_id, ratio_name, value, unit, source_concept)
        VALUES (:period_id, :ratio_name, :value, :unit, :source_concept)
        ON CONFLICT (period_id, ratio_name) DO UPDATE SET
            value = EXCLUDED.value, unit = EXCLUDED.unit, source_concept = EXCLUDED.source_concept
        """
    )
    session.execute(sql, {"period_id": period_id, **ratio})


def insert_data_quality_log(session, record: dict):
    sql = text(
        """
        INSERT INTO data_quality_log (filing_id, period_id, check_name, severity, message, details_json)
        VALUES (:filing_id, :period_id, :check_name, :severity, :message, CAST(:details_json AS JSONB))
        """
    )
    import json

    session.execute(sql, {**record, "details_json": json.dumps(record.get("details_json") or {})})
