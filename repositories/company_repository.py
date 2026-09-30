"""
repositories/company_repository.py

ISSUE 9 requires persisting normalized financial_periods rows, which have
a NOT NULL company_id foreign key. This is the minimal find-or-create
needed to satisfy that for a Phase-1 controlled single-symbol run — it is
NOT the full company-master ingestion described in spec §22 (no ISIN
resolution, no symbol-change history). That remains future work.
"""
from sqlalchemy import text


def get_or_create_company(session, symbol: str, company_name: str = None) -> int:
    symbol = symbol.strip().upper()

    existing = session.execute(
        text("SELECT company_id FROM companies WHERE nse_symbol = :symbol LIMIT 1"),
        {"symbol": symbol},
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    result = session.execute(
        text(
            """
            INSERT INTO companies (nse_symbol, company_name, exchange, status)
            VALUES (:symbol, :company_name, 'NSE', 'ACTIVE')
            RETURNING company_id
            """
        ),
        {"symbol": symbol, "company_name": company_name or symbol},
    )
    return result.scalar_one()
