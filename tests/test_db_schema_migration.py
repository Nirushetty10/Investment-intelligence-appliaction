"""
tests/test_db_schema_migration.py

Real PostgreSQL integration tests for the schema-initialization fix:
  - fresh/empty database initialization
  - repeated --init-db (idempotency)
  - migrating an existing (pre-fix) database forward safely
  - ratios.needs_validation column creation
  - discovery idempotency (no duplicate filing_id on reclassification)

These connect to an ACTUAL Postgres server — there is no mocking here,
because the bug this is regression-testing (migration statement ordering,
a real UNIQUE constraint, a real auto-generated constraint name) can only
be genuinely exercised against a real database engine. If no Postgres is
reachable (checked once, cheaply, at module load), every test in this
module is skipped rather than failed, so `pytest -q` stays green in
environments without a configured database — set NSE_DB_HOST / NSE_DB_PORT
/ NSE_DB_USER / NSE_DB_PASSWORD / NSE_TEST_DB_ADMIN (a database to connect
to for CREATE DATABASE / DROP DATABASE, default "postgres") to enable them.

Each test creates and drops its own throwaway database for isolation.
"""
import os
import uuid

import pytest

try:
    import psycopg2
    from sqlalchemy import create_engine, text
except ImportError:
    psycopg2 = None


DB_HOST = os.environ.get("NSE_DB_HOST", "localhost")
DB_PORT = os.environ.get("NSE_DB_PORT", "5432")
DB_USER = os.environ.get("NSE_DB_USER", "postgres")
DB_PASSWORD = os.environ.get("NSE_DB_PASSWORD", "testpass")
ADMIN_DB = os.environ.get("NSE_TEST_DB_ADMIN", "postgres")


def _postgres_available() -> bool:
    if psycopg2 is None:
        return False
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=ADMIN_DB,
            connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason="No reachable PostgreSQL server configured (set NSE_DB_HOST/PORT/USER/PASSWORD) — skipping real-DB tests.",
)


@pytest.fixture
def temp_db_name():
    name = f"nse_pytest_{uuid.uuid4().hex[:12]}"
    admin_conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=ADMIN_DB,
    )
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    admin_conn.close()

    yield name

    admin_conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=ADMIN_DB,
    )
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin_conn.close()


def _engine_for(db_name):
    url = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{db_name}"
    return create_engine(url, future=True)


def _init_schema_against(db_name):
    """Runs the real db.connection.init_schema() against a specific
    database name, without relying on config.settings' module-level
    engine caching (which is keyed to whatever NSE_DB_* was set at import
    time) — rebuilds a fresh engine pointed at db_name every call."""
    import db.connection as connection_module

    engine = _engine_for(db_name)
    original_get_engine = connection_module.get_engine
    connection_module.get_engine = lambda: engine
    try:
        connection_module.init_schema()
    finally:
        connection_module.get_engine = original_get_engine
    return engine


# --------------------------------------------------------------------------
# 1. Fresh/empty database initialization
# --------------------------------------------------------------------------

def test_fresh_database_init_creates_all_tables(temp_db_name):
    engine = _init_schema_against(temp_db_name)
    with engine.connect() as conn:
        tables = {
            row[0] for row in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            )
        }
    expected = {
        "companies", "nse_filing_registry", "raw_filings", "xbrl_contexts",
        "xbrl_units", "xbrl_facts", "financial_periods", "income_statement",
        "balance_sheet", "cashflow_statement", "ratios", "data_quality_log",
        "ingestion_log",
    }
    missing = expected - tables
    assert not missing, f"Tables missing after fresh init: {missing}"


# --------------------------------------------------------------------------
# 2. Repeated --init-db (idempotency on an already-initialized database)
# --------------------------------------------------------------------------

def test_repeated_init_db_does_not_error(temp_db_name):
    _init_schema_against(temp_db_name)   # first run
    engine = _init_schema_against(temp_db_name)   # second run — must not raise
    engine = _init_schema_against(temp_db_name)   # third, for good measure
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM nse_filing_registry")).scalar()
    assert count == 0  # still empty, nothing duplicated/corrupted by re-running


# --------------------------------------------------------------------------
# 3. ratios.needs_validation column creation
# --------------------------------------------------------------------------

def test_ratios_needs_validation_column_exists(temp_db_name):
    engine = _init_schema_against(temp_db_name)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT column_name, data_type, column_default, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'ratios' AND column_name = 'needs_validation'"
            )
        ).fetchone()
    assert row is not None, "ratios.needs_validation column was not created"
    assert row.data_type == "boolean"
    assert row.is_nullable == "NO"
    assert "false" in row.column_default.lower()


# --------------------------------------------------------------------------
# 4. Existing (pre-fix) database migration — the actual bug scenario
# --------------------------------------------------------------------------

def test_existing_old_schema_database_migrates_safely(temp_db_name):
    """Builds a genuinely faithful 'old' database (correct init_schema(),
    then deliberately downgraded: needs_validation dropped, and the old
    6-column period_type-included UNIQUE constraint restored under its
    own distinct name) WITH a real pre-existing row, then re-runs
    init_schema() and confirms: no error, the column reappears, the
    constraint is corrected, and the pre-existing row is untouched."""
    engine = _init_schema_against(temp_db_name)

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE ratios DROP COLUMN needs_validation"))
        conn.execute(text("ALTER TABLE nse_filing_registry DROP CONSTRAINT nse_filing_registry_natural_key"))
        conn.execute(
            text(
                "ALTER TABLE nse_filing_registry ADD CONSTRAINT nse_filing_registry_old_6col_key "
                "UNIQUE (symbol, period_end_date, period_type, statement_type, broadcast_date, source_kind)"
            )
        )
        conn.execute(
            text("INSERT INTO companies (nse_symbol, company_name, exchange, status) VALUES ('RELIANCE', 'Reliance Industries Limited', 'NSE', 'ACTIVE')")
        )
        conn.execute(
            text(
                "INSERT INTO nse_filing_registry "
                "(symbol, period_end_date, period_type, statement_type, source_kind, broadcast_date) "
                "VALUES ('RELIANCE', '2026-03-31', 'quarterly', 'consolidated', 'INTEGRATED_FILING_IXBRL', '2026-07-20 10:00:00+00')"
            )
        )

    # Re-run the real migration path against this now-"old-style" database
    engine = _init_schema_against(temp_db_name)

    with engine.connect() as conn:
        col = conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'ratios' AND column_name = 'needs_validation'"
            )
        ).fetchone()
        assert col is not None, "needs_validation was not re-added to an existing database"

        constraints = {
            row[0] for row in conn.execute(
                text(
                    "SELECT conname FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "WHERE rel.relname = 'nse_filing_registry' AND con.contype = 'u'"
                )
            )
        }
        assert "nse_filing_registry_natural_key" in constraints
        assert "nse_filing_registry_old_6col_key" not in constraints

        row = conn.execute(text("SELECT symbol, period_type FROM nse_filing_registry")).fetchone()
        assert row is not None, "pre-existing data was lost during migration"
        assert row.symbol == "RELIANCE"
        assert row.period_type == "quarterly"


# --------------------------------------------------------------------------
# 4b. reporting_quarter must hold real NSE free text, not just a 4-char code
#     (found while testing #5 below: the raw catalog text "First Quarter"
#     overflowed the original VARCHAR(4) column and crashed discovery).
# --------------------------------------------------------------------------

def test_reporting_quarter_column_accepts_real_nse_text(temp_db_name):
    engine = _init_schema_against(temp_db_name)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'nse_filing_registry' AND column_name = 'reporting_quarter'"
            )
        ).fetchone()
    assert row is not None
    assert row.data_type == "text"


# --------------------------------------------------------------------------
# 5. Duplicate discovery: reclassifying period_type must update the
#    existing row, never insert a new filing_id
# --------------------------------------------------------------------------

def test_discovery_reclassification_does_not_create_duplicate(temp_db_name):
    from datetime import datetime, timezone
    import db.connection as connection_module
    from repositories.filing_repository import upsert_filing_registry_entry

    engine = _init_schema_against(temp_db_name)
    original_get_engine = connection_module.get_engine
    original_sessionmaker = connection_module._SessionLocal
    connection_module.get_engine = lambda: engine
    connection_module._SessionLocal = None  # force sessionmaker rebuild against the new engine
    try:
        broadcast = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
        base_entry = {
            "company_id": None, "symbol": "RELIANCE", "company_name": "Reliance Industries Limited",
            "period_start_date": None, "period_end_date": "2026-03-31",
            "period_type": "quarterly", "reporting_quarter": None, "financial_year": "2025-26",
            "statement_type": "consolidated", "submission_type": "Audited", "cumulative": False,
            "accounting_standard": "IND_AS", "broadcast_date": broadcast, "revision_date": None,
            "revision_remarks": None, "xbrl_url": "https://nsearchives.nseindia.com/x.xml",
            "ixbrl_url": "https://nsearchives.nseindia.com/x.html", "detail_url": None,
            "source_url": "https://www.nseindia.com/api/x", "source_kind": "INTEGRATED_FILING_IXBRL",
            "filing_hash": None, "catalog_json": {"raw": "run1"}, "discovery_status": "DISCOVERED",
        }

        with connection_module.session_scope() as session:
            id1 = upsert_filing_registry_entry(session, base_entry)

        reclassified_entry = dict(base_entry)
        reclassified_entry["period_type"] = "annual"
        reclassified_entry["catalog_json"] = {"raw": "run2"}

        with connection_module.session_scope() as session:
            id2 = upsert_filing_registry_entry(session, reclassified_entry)

        assert id1 == id2, f"Duplicate filing_id created on reclassification: {id1} != {id2}"

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT filing_id, period_type FROM nse_filing_registry")).fetchall()
        assert len(rows) == 1, f"Expected exactly 1 row, found {len(rows)}"
        assert rows[0].period_type == "annual"  # updated to the latest classification
    finally:
        connection_module.get_engine = original_get_engine
        connection_module._SessionLocal = original_sessionmaker
