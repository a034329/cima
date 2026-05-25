"""Tests de aportaciones (capital externo del usuario)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services.aportaciones import (
    aportaciones_por_anio,
    parse_ibkr_aportaciones,
    reconciliar_aportaciones,
)


IBKR_CSV = Path("/app/cima/test_data/IBKR.csv")


# ── Parser IBKR (validación contra dato real) ─────────────────────────────


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="IBKR de muestra no presente")
def test_ibkr_aportaciones_cuadran_con_dato_real() -> None:
    """Deposits & Withdrawals reales: 2025 = 15.500, 2026 = 5.000."""
    cands = parse_ibkr_aportaciones(IBKR_CSV, broker_id="ibkr")
    por_anio: dict[int, Decimal] = {}
    for c in cands:
        por_anio[c.fecha.year] = por_anio.get(c.fecha.year, Decimal("0")) + c.importe_eur
    assert por_anio.get(2025) == Decimal("15500")
    assert por_anio.get(2026) == Decimal("5000")
    # external_ids únicos → dedup en reimport
    ids = [c.external_id for c in cands]
    assert len(ids) == len(set(ids))


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="IBKR de muestra no presente")
def test_reimport_ibkr_aportaciones_no_duplica(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker,
) -> None:
    cands = parse_ibkr_aportaciones(IBKR_CSV, broker_id=broker_tr.id)
    r1 = reconciliar_aportaciones(db, cartera.id, cands)
    assert r1.insertadas > 0
    r2 = reconciliar_aportaciones(db, cartera.id, parse_ibkr_aportaciones(IBKR_CSV, broker_id=broker_tr.id))
    assert r2.insertadas == 0
    assert r2.deduplicadas == r1.insertadas


# ── Resumen por año ────────────────────────────────────────────────────────


def test_aportaciones_por_anio_neto(
    db: Session, cartera: models.Cartera,
) -> None:
    db.add(models.Aportacion(cartera_id=cartera.id, fecha=date(2025, 3, 1),
                             importe_eur=Decimal("10000"), origen="manual", external_id="a"))
    db.add(models.Aportacion(cartera_id=cartera.id, fecha=date(2025, 9, 1),
                             importe_eur=Decimal("5000"), origen="manual", external_id="b"))
    db.add(models.Aportacion(cartera_id=cartera.id, fecha=date(2025, 11, 1),
                             importe_eur=Decimal("-2000"), origen="manual", external_id="c"))  # retirada
    db.commit()
    por_anio = aportaciones_por_anio(db, cartera.id)
    assert por_anio[2025] == Decimal("13000")   # 10000 + 5000 - 2000


# ── Endpoint manual ────────────────────────────────────────────────────────


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


def test_endpoint_aportacion_manual_y_resumen(client_y_db) -> None:
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="a@a.a", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    s.commit(); s.close()

    r = client.post("/api/aportaciones", json={
        "fecha": "2026-01-15", "importe_eur": "12000", "descripcion": "DEGIRO enero",
    })
    assert r.status_code == 201, r.text

    r2 = client.get("/api/aportaciones")
    assert r2.status_code == 200
    data = r2.json()
    assert Decimal(data["total_neto"]) == Decimal("12000")
    assert any(a["anio"] == 2026 and Decimal(a["neto"]) == Decimal("12000")
               for a in data["por_anio"])
    assert len(data["movimientos"]) == 1


def test_endpoint_aportacion_marker_en_cartera(client_y_db) -> None:
    """El marcador aportacion_neta_anio aparece en /api/cartera."""
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="a@a.a", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    from datetime import date as _d
    s.add(models.Aportacion(cartera_id=cart.id, fecha=_d(_d.today().year, 2, 1),
                            importe_eur=Decimal("8000"), origen="manual", external_id="x"))
    s.commit(); s.close()

    r = client.get("/api/cartera")
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["aportacion_neta_anio"]) == Decimal("8000")
