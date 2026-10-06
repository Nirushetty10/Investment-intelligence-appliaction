"""
Thin SQLAlchemy engine/session wrapper. Deliberately not an ORM model layer
for the whole schema — repositories/ use plain parameterized SQL via this
connection so that every insert/upsert is explicit about NULL handling
(SQLAlchemy ORM defaults can silently coerce None -> column default, which
is exactly the "guessing" behavior the spec forbids).
"""
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.settings import DB

# ISSUE 2: nse_filing_registry's natural-key UNIQUE constraint used to
# include period_type (our own derived classification, not stable NSE
# filing identity), which broke idempotent discovery whenever
# classification changed between runs. See _migrate_filing_registry_natural_key.
_FILING_REGISTRY_NEW_CONSTRAINT_NAME = "nse_filing_registry_natural_key"
_FILING_REGISTRY_NEW_KEY_COLUMNS = {"symbol", "period_end_date", "statement_type", "broadcast_date", "source_kind"}
_FILING_REGISTRY_OLD_KEY_COLUMNS = _FILING_REGISTRY_NEW_KEY_COLUMNS | {"period_type"}

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DB.sqlalchemy_url, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), future=True)
    return _SessionLocal


@contextmanager
def session_scope():
    Session = get_sessionmaker()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_MIGRATIONS_MARKER = "-- MIGRATIONS MARKER --"


def _split_statements(sql: str) -> list:
    """
    Splits a block of plain DDL into individual statements on ';\\n'. Each
    candidate is checked for actual executable content after stripping
    '--' line comments — a trailing comment block (e.g. the section header
    comment right before the MIGRATIONS marker) with no real SQL after it
    would otherwise still pass a bare `.strip()` truthiness check (it's
    non-empty text) and get sent to the database as a "statement", which
    psycopg2/Postgres rejects as an empty query. Does not attempt to
    understand dollar-quoted ($$...$$) blocks — this project's schema.sql
    deliberately avoids those (see init_schema()'s docstring).
    """
    statements = []
    for chunk in sql.split(";\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        without_comments = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if without_comments:
            statements.append(chunk)
    return statements


def init_schema():
    """
    Run db/schema.sql against the configured database. Safe to call on a
    completely empty database and safe to call repeatedly on an existing
    one (every statement in schema.sql is CREATE TABLE/INDEX IF NOT
    EXISTS, or an additive ALTER ... IF NOT EXISTS).

    Ordering contract (this is what actually fixes the
    "relation does not exist" failure): schema.sql is split into two
    parts at the literal marker comment "-- MIGRATIONS MARKER --" —
    everything before it (all CREATE TABLE / CREATE INDEX statements) runs
    first, as one transaction; everything from the marker onward (ALTER
    TABLE migrations) runs second, each statement in its OWN transaction.
    This means:
      - A migration can never execute before its target table exists,
        regardless of where a future migration is accidentally added in
        the file relative to other ALTERs (as long as it's after the
        marker, which is the only part of this contract a future editor
        needs to remember).
      - One failing migration does not prevent unrelated earlier-run table
        creation from being committed (the table-creation phase already
        committed by the time migrations run at all), and does not
        prevent OTHER migrations from being attempted.
      - A real schema error is never hidden: the first failing statement's
        exact text and the database's own error are raised immediately,
        not swallowed or logged-and-continued.
    """
    from pathlib import Path

    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text()

    if _MIGRATIONS_MARKER not in sql:
        raise RuntimeError(
            f"db/schema.sql is missing the {_MIGRATIONS_MARKER!r} marker — "
            "cannot safely determine which statements are table creation "
            "vs. migrations. This marker must exist exactly once."
        )

    tables_sql, migrations_sql = sql.split(_MIGRATIONS_MARKER, 1)

    engine = get_engine()

    # Phase 1: all table/index creation, one transaction. If any statement
    # here fails, nothing from this phase is committed — which is correct,
    # since these are all CREATE ... IF NOT EXISTS and a genuine failure
    # here means a real, unrecoverable schema problem (e.g. a malformed
    # CREATE TABLE), not a "safe to ignore and continue" situation.
    with engine.begin() as conn:
        for statement in _split_statements(tables_sql):
            conn.exec_driver_sql(statement)

    # Phase 2: migrations, each in its own transaction, run only after
    # phase 1 has fully committed — so every migration's target table is
    # guaranteed to already exist.
    for statement in _split_statements(migrations_sql):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(statement)
        except Exception as exc:
            raise RuntimeError(
                f"Migration failed and was NOT silently skipped.\n"
                f"Statement: {statement}\n"
                f"Database error: {exc}"
            ) from exc

    # Phase 3: the one migration that needs real introspection (finding an
    # auto-generated, potentially truncated/hashed constraint name by its
    # column signature) rather than a plain idempotent DDL statement — see
    # _migrate_filing_registry_natural_key's docstring for why this isn't
    # just another line in schema.sql's migrations section.
    _migrate_filing_registry_natural_key(engine)


def _migrate_filing_registry_natural_key(engine):
    """
    ISSUE 2 fix: drop the old nse_filing_registry UNIQUE constraint that
    incorrectly included period_type (our own derived classification, not
    stable NSE filing identity) and replace it with the corrected natural
    key — symbol, period_end_date, statement_type, broadcast_date,
    source_kind — so re-running discovery after a filing's period_type
    classification changes updates the EXISTING registry row instead of
    inserting a duplicate.

    On a fresh database, the CREATE TABLE in schema.sql already creates
    the corrected, explicitly-named constraint directly — this function's
    "does it already exist?" check finds that immediately and no-ops. It
    only does real work on a database created before this fix, where the
    old constraint has an auto-generated name Postgres may have truncated
    and hashed (NAMEDATALEN=63), making it unsafe to hardcode anywhere —
    it has to be found by matching its actual column set instead. That
    dynamic lookup is why this is Python, not a plain SQL statement: doing
    it in raw SQL needs a DO $$ ... $$ PL/pgSQL block, and this module's
    schema.sql statement splitter deliberately only handles plain
    semicolon-terminated DDL (see _split_statements), not dollar-quoted
    blocks. Idempotent and safe to call on every init_schema() run.
    """
    with engine.begin() as conn:
        already_correct = conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
            {"name": _FILING_REGISTRY_NEW_CONSTRAINT_NAME},
        ).fetchone()
        if already_correct:
            return

        rows = conn.execute(
            text(
                """
                SELECT con.conname AS conname, att.attname AS colname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN unnest(con.conkey) AS k(attnum) ON true
                JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                WHERE rel.relname = 'nse_filing_registry' AND con.contype = 'u'
                """
            )
        ).fetchall()

        columns_by_constraint = {}
        for conname, colname in rows:
            columns_by_constraint.setdefault(conname, set()).add(colname)

        old_conname = next(
            (name for name, cols in columns_by_constraint.items() if cols == _FILING_REGISTRY_OLD_KEY_COLUMNS),
            None,
        )

        if old_conname is not None:
            conn.execute(text(f'ALTER TABLE nse_filing_registry DROP CONSTRAINT "{old_conname}"'))

        conn.execute(
            text(
                f"""
                ALTER TABLE nse_filing_registry
                    ADD CONSTRAINT {_FILING_REGISTRY_NEW_CONSTRAINT_NAME}
                    UNIQUE (symbol, period_end_date, statement_type, broadcast_date, source_kind)
                """
            )
        )
