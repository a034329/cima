"""Tests de GET /api/import/estado — fecha del último registro por broker."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app


def _setup():  # type: ignore[no-untyped-def]
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng)

    def override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    return eng, TS, override


def test_estado_devuelve_ultima_fecha_por_broker() -> None:
    eng, TS, override = _setup()
    s = TS()
    user = models.User(email="t@cima.local", modo="owner")
    s.add(user)
    s.flush()
    cartera = models.Cartera(user_id=user.id, nombre="C")
    s.add(cartera)
    s.flush()
    bdg = models.Broker(user_id=user.id, broker_tipo="degiro",
                        saldo_reportado_eur=Decimal("100"), saldo_fecha=date(2026, 5, 14))
    btr = models.Broker(user_id=user.id, broker_tipo="tr")
    s.add_all([bdg, btr])
    s.flush()
    pos = models.Posicion(cartera_id=cartera.id, isin="US_X", nombre="X")
    s.add(pos)
    s.flush()
    # DEGIRO: dos transacciones; la más reciente manda. TR: una opción posterior.
    tx = dict(cartera_id=cartera.id, posicion_id=pos.id, broker_id=bdg.id,
              cantidad=Decimal("1"), precio_local=Decimal("10"),
              importe_local=Decimal("10"), importe_eur=Decimal("10"))
    s.add(models.Transaccion(fecha=date(2026, 1, 10), tipo="BUY", **tx))
    s.add(models.Transaccion(fecha=date(2026, 5, 14), tipo="SELL", **tx))
    s.add(models.Opcion(cartera_id=cartera.id, broker_id=btr.id, simbolo="AAPL 250C",
                        subyacente="AAPL", tipo_op="C", accion="venta",
                        cantidad=Decimal("1"), importe_eur=Decimal("42"),
                        fecha=date(2026, 5, 18)))
    s.commit()
    s.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            r = c.get("/api/import/estado")
            assert r.status_code == 200, r.text
            data = {b["broker_tipo"]: b for b in r.json()}

            assert data["degiro"]["ultima_fecha"] == "2026-05-14"
            assert data["degiro"]["num_registros"] == 2
            assert data["degiro"]["saldo_reportado_eur"] == "100.0000"
            assert data["degiro"]["saldo_fecha"] == "2026-05-14"

            assert data["tr"]["ultima_fecha"] == "2026-05-18"
            assert data["tr"]["num_registros"] == 1
            assert data["tr"]["saldo_fecha"] is None
    finally:
        app.dependency_overrides.clear()
        eng.dispose()


def test_estado_broker_sin_registros() -> None:
    eng, TS, override = _setup()
    s = TS()
    user = models.User(email="t2@cima.local", modo="owner")
    s.add(user)
    s.flush()
    s.add(models.Cartera(user_id=user.id, nombre="C"))
    s.add(models.Broker(user_id=user.id, broker_tipo="ibkr"))
    s.commit()
    s.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            r = c.get("/api/import/estado")
            assert r.status_code == 200, r.text
            data = {b["broker_tipo"]: b for b in r.json()}
            assert data["ibkr"]["ultima_fecha"] is None
            assert data["ibkr"]["num_registros"] == 0
    finally:
        app.dependency_overrides.clear()
        eng.dispose()
