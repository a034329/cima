"""Tests de la fricción conductual (avisa, rebate 2 veces, te deja, captura)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services import friccion, plan


def _offline(monkeypatch) -> None:
    """Evita red: estimaciones usa precios_nativos."""
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {})
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {})


def _pos(db, cartera, isin, nombre, bloque_id=None, coste="5000") -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre,
                        divisa_local="EUR", bloque_id=bloque_id)
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
        coste_unit_eur=Decimal(coste) / Decimal("10"),
        coste_total_eur=Decimal(coste), gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def _bloque(db, cartera, nombre, cat, en_estrategia=True) -> models.Bloque:
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=cat,
                      orden=1, es_base=True, en_estrategia=en_estrategia)
    db.add(b); db.flush()
    return b


def test_friccion_colchon_es_alta_y_regla_absoluta(db, cartera, monkeypatch) -> None:
    _offline(monkeypatch)
    col = _bloque(db, cartera, "Colchón", "colchon", en_estrategia=False)
    _pos(db, cartera, "US_F", "MinVol ETF", bloque_id=col.id)
    db.commit()
    r = friccion.evaluar_friccion(db, cartera.id, "US_F", "VENDER")
    assert r is not None
    assert r.severidad == "ALTA"
    assert "colchón" in r.rebate2.lower()         # regla absoluta del colchón


def test_friccion_compounder_con_plan_protector(db, cartera, monkeypatch) -> None:
    _offline(monkeypatch)
    g = _bloque(db, cartera, "Compounders", "growth")
    _pos(db, cartera, "US_MSFT", "Microsoft", bloque_id=g.id)
    db.commit()
    plan.crear_paso(db, cartera.id, "US_MSFT", "COMPRAR", "ALTA")
    r = friccion.evaluar_friccion(db, cartera.id, "US_MSFT", "RECORTAR")
    assert r is not None
    assert "Compounder" in r.rebate1
    assert "Plan" in r.rebate2                     # "tu Plan dice «Comprar»"


def test_friccion_deja_pasar_sin_senales(db, cartera, monkeypatch) -> None:
    _offline(monkeypatch)
    _pos(db, cartera, "US_X", "Cualquiera")        # sin bloque, sin plan, sin estimación
    db.commit()
    assert friccion.evaluar_friccion(db, cartera.id, "US_X", "VENDER") is None


def test_friccion_no_dispara_en_decision_no_peligrosa(db, cartera, monkeypatch) -> None:
    _offline(monkeypatch)
    col = _bloque(db, cartera, "Colchón", "colchon", en_estrategia=False)
    _pos(db, cartera, "US_F", "MinVol", bloque_id=col.id)
    db.commit()
    assert friccion.evaluar_friccion(db, cartera.id, "US_F", "COMPRAR") is None


def test_crear_paso_con_friccion_registra_evento() -> None:
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

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as c:
            c.post("/api/bootstrap")
            with TS() as s:
                cartera = s.query(models.Cartera).first()
                _pos(s, cartera, "US1", "Alpha")
                s.commit()
            r = c.post("/api/plan", json={
                "isin": "US1", "decision": "VENDER", "prioridad": "ALTA",
                "friccion_severidad": "ALTA", "friccion_motivo": "necesito liquidez",
            })
            assert r.status_code == 201, r.text
            with TS() as s:
                ev = s.execute(select(models.EventoFriccion)).scalars().all()
                assert len(ev) == 1
                assert ev[0].isin == "US1"
                assert ev[0].rebatido is True
                assert ev[0].motivo == "necesito liquidez"
    finally:
        app.dependency_overrides.clear()
