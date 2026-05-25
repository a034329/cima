"""Fixtures pytest comunes."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import models  # noqa: F401 — registra modelos en Base
from app.db.base import Base


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    """BD SQLite en memoria, schema fresco por test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def cartera(db: Session) -> models.Cartera:
    user = models.User(email="test@cima.local", modo="owner")
    db.add(user)
    db.flush()
    cartera = models.Cartera(user_id=user.id, nombre="Test")
    db.add(cartera)
    db.flush()
    return cartera


@pytest.fixture()
def broker_tr(db: Session, cartera: models.Cartera) -> models.Broker:
    b = models.Broker(user_id=cartera.user_id, broker_tipo="tr", alias="TR test")
    db.add(b)
    db.flush()
    return b


@pytest.fixture()
def broker_degiro(db: Session, cartera: models.Cartera) -> models.Broker:
    b = models.Broker(user_id=cartera.user_id, broker_tipo="degiro", alias="DG test")
    db.add(b)
    db.flush()
    return b
