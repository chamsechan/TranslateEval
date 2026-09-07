from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


@event.listens_for(Base.metadata, "after_create")
def install_query_invalidation(_metadata, connection, **_kwargs):
    from .read_model_schema import install_read_model_triggers
    install_read_model_triggers(connection)


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or settings.database_url
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA cache_size=-32768")
            cursor.close()

    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)


def optimize_database(bind_or_session, *, analyze: bool = False) -> None:
    """Refresh SQLite planning statistics after imports or substantial work.

    Normal calls use bounded PRAGMA optimize; migrations and explicit maintenance
    may request a complete ANALYZE. The caller owns a supplied Session transaction.
    """
    def optimize(connection):
        if connection.dialect.name != "sqlite":
            return
        if analyze:
            connection.exec_driver_sql("ANALYZE")
        else:
            previous_limit = connection.exec_driver_sql("PRAGMA analysis_limit").scalar()
            connection.exec_driver_sql("PRAGMA analysis_limit=1000")
            try:
                connection.exec_driver_sql("PRAGMA optimize=0x10002")
            finally:
                connection.exec_driver_sql(f"PRAGMA analysis_limit={int(previous_limit or 0)}")
    if isinstance(bind_or_session, Session):
        connection = bind_or_session.connection()
        optimize(connection)
    elif isinstance(bind_or_session, Engine):
        with bind_or_session.begin() as connection:
            optimize(connection)
    else:
        optimize(bind_or_session)
