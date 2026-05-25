"""Tests del seguimiento/watchlist — offline (mocks de red)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services import estimaciones as svc


def test_calcular_seguimiento_usa_ticker(db: Session, cartera, monkeypatch) -> None:
    """La valoración de seguimiento toma el precio nativo por ticker y aplica la
    misma fórmula que cartera."""
    db.add(models.Seguimiento(cartera_id=cartera.id, isin="US_WL", ticker="WLCO",
                              nombre="WatchCo", divisa="USD"))
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_WL", tipo_val="PER",
                             multiplo_objetivo=Decimal("20"), metrica_base_4y=Decimal("10"),
                             dividendo_share=Decimal("3")))
    db.commit()

    import app.services.precios as precios
    monkeypatch.setattr(precios, "precio_nativo_simbolo",
                        lambda sim: (Decimal("100"), "USD") if sim == "WLCO" else None)

    calcs = svc.calcular_estimaciones_seguimiento(db, cartera.id)
    c = [x for x in calcs if x.isin == "US_WL"][0]
    assert c.nombre == "WatchCo"
    assert c.precio_objetivo == Decimal("200")            # 20 × 10
    assert abs(c.cagr4_pct - Decimal("0.1892")) < Decimal("0.001")
    assert abs(c.div_yield_pct - Decimal("0.03")) < Decimal("0.0001")


def test_endpoint_seguimiento_e2e(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

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

    import app.services.precios as precios
    monkeypatch.setattr(precios, "resolver_ticker", lambda t: {
        "ticker": "TESTCO", "isin": "US_TEST", "nombre": "Test Co", "divisa": "USD",
    })
    monkeypatch.setattr(precios, "consenso_simbolo", lambda sim: {
        "precio_obj_consenso": 200.0, "eps_forward": 5.0, "eps_consenso_4y": 10.0,
        "num_analistas_eps": 8, "anio_consenso_4y": 2030,
    })
    monkeypatch.setattr(precios, "fundamentales_simbolo",
                        lambda sim: {"eps": 4.0, "dividend": 2.0})
    monkeypatch.setattr(precios, "precio_nativo_simbolo", lambda sim: (Decimal("100"), "USD"))

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            c.post("/api/bootstrap")
            r = c.post("/api/seguimiento", json={"ticker": "TESTCO"})
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["isin"] == "US_TEST"
            assert body["nombre"] == "Test Co"
            est = body["estimacion"]
            assert est["multiplo_objetivo"] == "40.0000"     # 200 / 5
            assert est["metrica_base_4y"] == "10.0000"       # EPS consenso 4A
            assert est["precio_objetivo"] == "400.0000"      # 40 × 10

            lst = c.get("/api/seguimiento").json()
            assert [x["isin"] for x in lst] == ["US_TEST"]

            assert c.delete("/api/seguimiento/US_TEST").status_code == 204
            assert c.get("/api/seguimiento").json() == []
    finally:
        app.dependency_overrides.clear()


def test_seguimiento_no_duplica_posicion(monkeypatch) -> None:
    """No se puede seguir una empresa que ya está en cartera (409)."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

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

    import app.services.precios as precios
    monkeypatch.setattr(precios, "resolver_ticker", lambda t: {
        "ticker": "DUP", "isin": "US_DUP", "nombre": "Dup Co", "divisa": "USD",
    })

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            c.post("/api/bootstrap")
            with TS() as s:
                cart = s.query(models.Cartera).first()
                p = models.Posicion(cartera_id=cart.id, isin="US_DUP",
                                    nombre="Dup Co", divisa_local="USD")
                s.add(p); s.flush()
                s.add(models.Lot(
                    posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                    cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                    coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                    gastos_eur=Decimal("0"),
                ))
                s.commit()
            r = c.post("/api/seguimiento", json={"ticker": "DUP"})
            assert r.status_code == 409, r.text
    finally:
        app.dependency_overrides.clear()


def test_seguimiento_permite_posicion_cerrada(monkeypatch) -> None:
    """Una posición vendida del todo (cantidad 0) SÍ se puede seguir: la tuviste,
    pero no está en la foto actual."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

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

    import app.services.precios as precios
    monkeypatch.setattr(precios, "resolver_ticker", lambda t: {
        "ticker": "OLD", "isin": "US_OLD", "nombre": "Old Co", "divisa": "USD",
    })
    monkeypatch.setattr(precios, "consenso_simbolo", lambda sim: None)
    monkeypatch.setattr(precios, "fundamentales_simbolo", lambda sim: {})
    monkeypatch.setattr(precios, "precio_nativo_simbolo", lambda sim: (Decimal("50"), "USD"))

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            c.post("/api/bootstrap")
            with TS() as s:
                cart = s.query(models.Cartera).first()
                p = models.Posicion(cartera_id=cart.id, isin="US_OLD",
                                    nombre="Old Co", divisa_local="USD")
                s.add(p); s.flush()
                s.add(models.Lot(   # lote agotado → posición cerrada
                    posicion_id=p.id, fecha_compra=date(2023, 1, 1),
                    cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("0"),
                    coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                    gastos_eur=Decimal("0"),
                ))
                s.commit()
            r = c.post("/api/seguimiento", json={"ticker": "OLD"})
            assert r.status_code == 201, r.text
    finally:
        app.dependency_overrides.clear()
