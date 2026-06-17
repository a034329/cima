"""Fixtures pytest comunes."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Mode, settings
from app.db import models  # noqa: F401 — registra modelos en Base
from app.db.base import Base


@pytest.fixture(autouse=True)
def _settings_aislados(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla los tests de la configuración del entorno del desarrollador.

    Settings carga `.env` (p.ej. CIMA_MODE=owner en la máquina de Angel) y
    los tests asumían los defaults de SaaS → test_proponer_estrategia_mock
    fallaba según QUIÉN ejecutara la suite (auditoría Cima 2026-06-11, T1).
    Cada test arranca con los defaults de producto; el que necesite otro
    modo lo monkeypatchea explícitamente."""
    monkeypatch.setattr(settings, "mode", Mode.SAAS)


@pytest.fixture(autouse=True)
def _cartera_override() -> Generator[None, None, None]:
    """Override de `get_current_cartera` para los tests de endpoints (Fase B).

    En producción `get_current_cartera` scopa por usuario autenticado (token en
    saas). Los tests son SINGLE-TENANT (una cartera) y llaman sin token, así que
    aquí lo resolvemos a "la única cartera de la BD del test" usando el `get_db`
    que cada test override-ea. Mantiene verdes los ~23 ficheros de endpoints sin
    autenticarlos. El test de AISLAMIENTO (IDOR) quita este override y usa la
    dependencia real con tokens."""
    from fastapi import Depends, HTTPException, status
    from sqlalchemy import select

    from app.auth.deps import get_current_cartera
    from app.db import get_db, models
    from app.main import app

    def _primera_cartera(db: Session = Depends(get_db)) -> models.Cartera:
        c = db.execute(select(models.Cartera)).scalars().first()
        if c is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="No hay cartera (test).")
        return c

    app.dependency_overrides[get_current_cartera] = _primera_cartera
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_cartera, None)


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
