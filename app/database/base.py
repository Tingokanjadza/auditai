"""SQLAlchemy engine, session factory and declarative base.

Written against SQLAlchemy 2.x with portability in mind: no SQLite-only column
types, no database-native ENUMs, and timestamps stored as timezone-aware
``DateTime``. Moving to PostgreSQL is a change of ``DATABASE_URL`` only.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator

from sqlalchemy import JSON, Engine, MetaData, event, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

#: Explicit naming convention so Alembic can autogenerate reversible migrations
#: when this prototype graduates to PostgreSQL.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    #: ``JSONColumn`` maps to SQLite TEXT and PostgreSQL JSONB-compatible JSON.
    type_annotation_map = {Dict[str, Any]: JSON}

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                value = value.isoformat()
            out[column.name] = value
        return out


def _engine_kwargs(url: str) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"echo": settings.db_echo, "future": True}
    if url.startswith("sqlite"):
        # check_same_thread=False lets FastAPI's threadpool and Streamlit share the file.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
    return kwargs


engine: Engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover - driver hook
    """Enable foreign keys and WAL on SQLite; a no-op on other backends."""
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used for every ``created_at``/``updated_at``."""
    return datetime.now(timezone.utc)


def init_db() -> None:
    """Create any missing tables. Safe to call repeatedly."""
    from app.database import models  # noqa: F401  (registers mappers)

    settings.ensure_directories()
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Destructive: used by tests and by the explicit 'reset demo data' action."""
    from app.database import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts, the Streamlit UI and the seeding routines."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
