"""
Thin SQLAlchemy engine/session wrapper. Deliberately not an ORM model layer
for the whole schema — repositories/ use plain parameterized SQL via this
connection so that every insert/upsert is explicit about NULL handling
(SQLAlchemy ORM defaults can silently coerce None -> column default, which
is exactly the "guessing" behavior the spec forbids).
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.settings import DB

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


def init_schema():
    """Run db/schema.sql against the configured database. Idempotent
    (schema.sql uses CREATE TABLE IF NOT EXISTS throughout)."""
    from pathlib import Path

    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text()
    engine = get_engine()
    with engine.begin() as conn:
        for statement in sql.split(";\n"):
            statement = statement.strip()
            if statement:
                conn.exec_driver_sql(statement)
