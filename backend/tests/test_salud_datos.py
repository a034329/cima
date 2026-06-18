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


# ── Papelera: restaurar descartadas (U9) ─────────────────────────────────────

from datetime import date
from decimal import Decimal


def test_restaurar_descartada(client_y_db) -> None:
    client, SessionLocal = client_y_db
    with SessionLocal() as s:
        user = models.User(email="t2@cima.local", modo="owner")
        s.add(user); s.flush()
        cart = models.Cartera(user_id=user.id, nombre="Test")
        s.add(cart); s.flush()
        broker = models.Broker(user_id=user.id, broker_tipo="degiro", alias="D")
        s.add(broker); s.flush()
        pos = models.Posicion(cartera_id=cart.id, isin="US0378331005",
                              nombre="Apple", divisa_local="USD")
        s.add(pos); s.flush()
        tx = models.Transaccion(
            cartera_id=cart.id, broker_id=broker.id, posicion_id=pos.id,
            fecha=date(2026, 1, 2), tipo="BUY", cantidad=Decimal("10"),
            precio_local=Decimal("100"), divisa_local="USD",
            importe_local=Decimal("1000"), fx_rate=Decimal("1"),
            importe_eur=Decimal("1000"), gastos_eur=Decimal("0"),
            tasas_externas_eur=Decimal("0"), estado="descartada",
            origen="extracto",
        )
        s.add(tx); s.commit()
        tx_id, pos_id = tx.id, pos.id

    r = client.post(f"/api/transacciones/{tx_id}/restaurar")
    assert r.status_code == 200
    assert r.json()["estado"] == "confirmada"
    with SessionLocal() as s:
        from app.services.fifo import estado_posicion
        assert estado_posicion(s, pos_id)["cantidad"] == Decimal("10")

    # Restaurar dos veces → 409 (ya confirmada)
    assert client.post(f"/api/transacciones/{tx_id}/restaurar").status_code == 409


def test_refrescar_endpoint(client_y_db, monkeypatch) -> None:
    """POST /salud-datos/refrescar dispara el refresco COMPLETO (prefill: precios,
    FX, fundamentales, consenso y re-siembra — 3B) y devuelve la frescura.
    Simulamos el prefill para no salir a red."""
    client, SessionLocal = client_y_db
    with SessionLocal() as s:
        user = models.User(email="r@cima.local", modo="owner")
        s.add(user); s.flush()
        s.add(models.Cartera(user_id=user.id, nombre="Test"))
        s.commit()

    llamado = {"prefill": False}
    from app.services import estimaciones
    def _fake_prefill(db, cid):
        llamado["prefill"] = True
        return 0
    monkeypatch.setattr(estimaciones, "prefill_estimaciones", _fake_prefill)

    r = client.post("/api/salud-datos/refrescar")
    assert r.status_code == 200
    assert llamado["prefill"] is True   # refresco completo, no solo precio+FX
    assert set(r.json()) == {
        "precios_ts", "fx_ts", "fundamentales_ts",
        "ultimo_import_ts", "ultimo_import_desc", "ultima_transaccion",
    }


def test_refrescar_sin_cartera_404(client_y_db) -> None:
    client, _ = client_y_db
    assert client.post("/api/salud-datos/refrescar").status_code == 404
