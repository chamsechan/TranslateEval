from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import importers
from app.database import Base, create_db_engine
from app.seed import seed_defaults


@pytest.fixture(autouse=True)
def isolated_import_staging(tmp_path: Path, monkeypatch) -> None:
    class TestSettings:
        import_dir = tmp_path / "imports"

    monkeypatch.setattr(importers, "settings", TestSettings())


@pytest.fixture()
def session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as session:
        seed_defaults(session)
    yield factory
    engine.dispose()
