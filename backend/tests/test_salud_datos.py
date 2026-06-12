"""Tests del endpoint de frescura de datos (U8)."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app


@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_salud_datos_sin_cartera(client_y_db) -> None:
    client, _ = client_y_db
    assert client.get("/api/salud-datos").status_code == 404


def test_salud_datos_basico(client_y_db) -> None:
    client, SessionLocal = client_y_db
    with SessionLocal() as s:
        user = models.User(email="t@cima.local", modo="owner")
        s.add(user); s.flush()
        s.add(models.Cartera(user_id=user.id, nombre="Test"))
        s.commit()
    r = client.get("/api/salud-datos")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "precios_ts", "fx_ts", "fundamentales_ts",
        "ultimo_import_ts", "ultimo_import_desc", "ultima_transaccion",
    }
    assert body["ultimo_import_ts"] is None
    assert body["ultima_transaccion"] is None
