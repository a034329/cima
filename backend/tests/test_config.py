"""Tests del endpoint de configuración (perfil, objetivo IF, modo, brokers)."""
from __future__ import annotations

from decimal import Decimal
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override() -> Generator:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    s = SessionLocal()
    u = models.User(email="a@a.a", modo="owner"); s.add(u); s.flush()
    s.add(models.Cartera(user_id=u.id, nombre="Cartera principal"))
    s.add(models.Broker(user_id=u.id, broker_tipo="degiro", alias="DEGIRO",
                        saldo_reportado_eur=Decimal("2137.40")))
    s.commit(); s.close()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


def test_get_config_defaults(client) -> None:
    j = client.get("/api/config").json()
    assert j["email"] == "a@a.a"
    assert j["nombre_cartera"] == "Cartera principal"
    assert j["objetivo_if_eur"] == "300000.00"        # default
    assert any(b["broker_tipo"] == "degiro" and b["saldo_reportado_eur"] == "2137.4000"
               for b in j["brokers"])


def test_patch_config_actualiza(client) -> None:
    r = client.patch("/api/config", json={"nombre_cartera": "IF 2028", "objetivo_if_eur": 250000})
    assert r.status_code == 200
    j = r.json()
    assert j["nombre_cartera"] == "IF 2028"
    assert j["objetivo_if_eur"] == "250000.00"
    # persiste
    assert client.get("/api/config").json()["objetivo_if_eur"] == "250000.00"


def test_patch_config_validaciones(client) -> None:
    assert client.patch("/api/config", json={"objetivo_if_eur": 0}).status_code == 400
    assert client.patch("/api/config", json={"objetivo_if_eur": -5}).status_code == 400
    assert client.patch("/api/config", json={"nombre_cartera": "  "}).status_code == 400


def test_patch_aportacion_mensual(client) -> None:
    r = client.patch("/api/config", json={"aportacion_mensual_eur": 1500})
    assert r.status_code == 200
    assert r.json()["aportacion_mensual_eur"] == "1500.00"
    assert client.patch("/api/config", json={"aportacion_mensual_eur": -1}).status_code == 400
