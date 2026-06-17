"""Aislamiento multi-tenant (ADR-003, Fase B).

Verifica que `get_current_cartera` scopa por el usuario autenticado: el usuario A
no puede leer/escribir la cartera del usuario B. A diferencia del resto de tests
de endpoints (que usan el override autouse de conftest para resolver "la primera
cartera"), aquí QUITAMOS ese override y usamos la dependencia real con tokens
Bearer, que es exactamente el camino de producción en modo saas.
"""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.deps import get_current_cartera
from app.config import Mode, settings
from app.db import get_db
from app.db.base import Base
from app.main import app


@pytest.fixture()
def saas_client(monkeypatch) -> Generator[TestClient, None, None]:
    # Modo saas: la auth NO se puentea, el token manda.
    monkeypatch.setattr(settings, "mode", Mode.SAAS)
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    # Quitamos el override autouse de conftest: aquí queremos la dependencia REAL.
    app.dependency_overrides.pop(get_current_cartera, None)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


def _alta(client: TestClient, email: str) -> str:
    r = client.post("/api/auth/signup", json={"email": email, "password": "12345678"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def test_cada_usuario_ve_su_propio_regimen(saas_client):
    tok_a = _alta(saas_client, "a@b.com")
    tok_b = _alta(saas_client, "b@b.com")
    ha = {"Authorization": f"Bearer {tok_a}"}
    hb = {"Authorization": f"Bearer {tok_b}"}

    # A fija sus indicadores
    payload_a = {"ciclo": "VERDE", "inflacion": "VERDE",
                 "geopolitica": "VERDE", "mercado": "VERDE"}
    assert saas_client.put("/api/regimen", json=payload_a, headers=ha).status_code == 200

    # B fija los suyos, distintos
    payload_b = {"ciclo": "ROJA", "inflacion": "ROJA",
                 "geopolitica": "ROJA", "mercado": "ROJA"}
    assert saas_client.put("/api/regimen", json=payload_b, headers=hb).status_code == 200

    # Cada uno lee lo suyo, no lo del otro (sin fuga IDOR)
    ra = saas_client.get("/api/regimen", headers=ha).json()
    rb = saas_client.get("/api/regimen", headers=hb).json()
    assert ra["indicadores"]["ciclo"] == "VERDE"
    assert rb["indicadores"]["ciclo"] == "ROJA"


def test_sin_token_en_saas_401(saas_client):
    assert saas_client.get("/api/regimen").status_code == 401
