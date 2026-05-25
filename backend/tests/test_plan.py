"""Tests del plan por valor (decisión por posición)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services import plan as svc


def _pos_con_lote(db: Session, cartera, isin: str, nombre: str, coste: Decimal) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2025, 1, 1),
        cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
        coste_unit_eur=coste / Decimal("10"), coste_total_eur=coste,
        gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def _seg(db: Session, cartera, isin: str, ticker: str, nombre: str,
         bloque_id: str | None = None) -> models.Seguimiento:
    s = models.Seguimiento(cartera_id=cartera.id, isin=isin, ticker=ticker,
                           nombre=nombre, bloque_id=bloque_id)
    db.add(s); db.flush()
    return s


def _bloque(db: Session, cartera, nombre: str, cat: str,
            peso_objetivo=None, en_estrategia: bool = True) -> models.Bloque:
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=cat,
                      orden=1, es_base=True, en_estrategia=en_estrategia,
                      peso_objetivo=(Decimal(str(peso_objetivo)) if peso_objetivo else None))
    db.add(b); db.flush()
    return b


def test_crear_paso_valida_isin_y_enum(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    # ISIN inexistente
    with pytest.raises(HTTPException) as e1:
        svc.crear_paso(db, cartera.id, "NOEXISTE", "COMPRAR", "ALTA")
    assert e1.value.status_code == 404
    # decisión inválida
    with pytest.raises(HTTPException) as e2:
        svc.crear_paso(db, cartera.id, "US1", "FOO", "ALTA")
    assert e2.value.status_code == 400


def test_decision_activa_default_mantener(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    activas = svc.decisiones_activas(db, cartera.id)
    assert "US1" not in activas
    pos = svc.posiciones_con_plan(db, cartera.id)
    assert pos[0].decision == "MANTENER"


def test_decision_activa_gana_mayor_prioridad(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    svc.crear_paso(db, cartera.id, "US1", "MONITORIZAR", "MEDIA")
    svc.crear_paso(db, cartera.id, "US1", "COMPRAR", "ALTA")
    activas = svc.decisiones_activas(db, cartera.id)
    assert activas["US1"].decision == "COMPRAR"   # ALTA gana a MEDIA


def test_completar_paso_lo_saca_de_activas(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    p = svc.crear_paso(db, cartera.id, "US1", "COMPRAR", "ALTA")
    assert svc.decisiones_activas(db, cartera.id)["US1"].decision == "COMPRAR"
    svc.actualizar_paso(db, cartera.id, p.id, estado="COMPLETADO")
    assert "US1" not in svc.decisiones_activas(db, cartera.id)


def test_eliminar_paso(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    p = svc.crear_paso(db, cartera.id, "US1", "VENDER", "ALTA")
    svc.eliminar_paso(db, cartera.id, p.id)
    assert db.get(models.PlanPaso, p.id) is None


def test_posiciones_con_plan_incluye_bloque_y_decision(db, cartera) -> None:
    b = models.Bloque(cartera_id=cartera.id, nombre="Growth", categoria_base="growth",
                      orden=1, es_base=True)
    db.add(b); db.flush()
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    p.bloque_id = b.id
    db.commit()
    svc.crear_paso(db, cartera.id, "US1", "REFORZAR", "ALTA",
                   capital_objetivo_eur=Decimal("500"), razon="DCA mensual")
    res = svc.posiciones_con_plan(db, cartera.id)
    r0 = [x for x in res if x.isin == "US1"][0]
    assert r0.decision == "REFORZAR"
    assert r0.bloque_nombre == "Growth"
    assert r0.capital_objetivo_eur == Decimal("500")
    assert r0.razon == "DCA mensual"


def test_crear_paso_acepta_seguimiento(db, cartera) -> None:
    """Una empresa del watchlist (no poseída) puede tener un paso COMPRAR."""
    _seg(db, cartera, "US_NVDA", "NVDA", "Nvidia")
    db.commit()
    p = svc.crear_paso(db, cartera.id, "US_NVDA", "COMPRAR", "ALTA",
                       capital_objetivo_eur=Decimal("2000"))
    assert p.isin == "US_NVDA"
    assert svc.decisiones_activas(db, cartera.id)["US_NVDA"].decision == "COMPRAR"
    # ISIN que no es ni posición ni seguimiento → 404
    with pytest.raises(HTTPException) as e:
        svc.crear_paso(db, cartera.id, "DESCONOCIDO", "COMPRAR", "ALTA")
    assert e.value.status_code == 404


def test_posiciones_con_plan_incluye_watchlist(db, cartera) -> None:
    growth = _bloque(db, cartera, "Compounders", "growth")
    _seg(db, cartera, "US_NVDA", "NVDA", "Nvidia", bloque_id=growth.id)
    db.commit()
    svc.crear_paso(db, cartera.id, "US_NVDA", "COMPRAR", "ALTA",
                   capital_objetivo_eur=Decimal("2000"))
    res = svc.posiciones_con_plan(db, cartera.id)
    r = [x for x in res if x.isin == "US_NVDA"][0]
    assert r.en_cartera is False          # candidato del watchlist
    assert r.valor_eur == Decimal("0")    # aún no en cartera
    assert r.bloque_nombre == "Compounders"
    assert r.decision == "COMPRAR"


def test_hueco_asignacion(db, cartera) -> None:
    growth = _bloque(db, cartera, "Compounders", "growth", peso_objetivo="0.30")
    income = _bloque(db, cartera, "Dividend Growth", "income", peso_objetivo="0.20")
    defensivo = _bloque(db, cartera, "Estable", "defensivo")   # sin objetivo
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    p.bloque_id = growth.id
    _seg(db, cartera, "US_NVDA", "NVDA", "Nvidia", bloque_id=income.id)
    db.commit()
    # Compra planeada de 4000 en income (watchlist).
    svc.crear_paso(db, cartera.id, "US_NVDA", "COMPRAR", "ALTA",
                   capital_objetivo_eur=Decimal("4000"))

    r = svc.hueco_asignacion(db, cartera.id)
    assert r.total_actual_eur == Decimal("6000")
    assert r.total_planeado_eur == Decimal("4000")
    assert r.total_proyectado_eur == Decimal("10000")

    g = [b for b in r.bloques if b.bloque_id == growth.id][0]
    assert g.actual_pct == Decimal("0.6") and g.planeado_pct == Decimal("0")
    assert g.deficit_pct == Decimal("-0.30")          # sobreponderado vs 30%

    i = [b for b in r.bloques if b.bloque_id == income.id][0]
    assert i.planeado_pct == Decimal("0.4")
    assert i.deficit_pct == Decimal("-0.20")          # 20% objetivo, 40% proyectado

    d = [b for b in r.bloques if b.bloque_id == defensivo.id][0]
    assert d.objetivo_pct is None and d.deficit_pct is None


def test_hueco_excluye_bloques_fuera_de_estrategia(db, cartera) -> None:
    """Un bloque con en_estrategia=False (cripto a largo, colchón) no aparece en el
    hueco ni infla la base del %."""
    growth = _bloque(db, cartera, "Compounders", "growth", peso_objetivo="0.50")
    cripto = _bloque(db, cartera, "Cripto", "cripto", en_estrategia=False)
    pg = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("5000"))
    pg.bloque_id = growth.id
    pc = _pos_con_lote(db, cartera, "XF_BTC", "Bitcoin", Decimal("5000"))
    pc.bloque_id = cripto.id
    db.commit()

    r = svc.hueco_asignacion(db, cartera.id)
    ids = [b.bloque_id for b in r.bloques]
    assert cripto.id not in ids                         # fuera de estrategia → no sale
    assert growth.id in ids
    # Base = solo capital en estrategia (5000), no los 10000 totales.
    assert r.total_actual_eur == Decimal("5000")
    g = [b for b in r.bloques if b.bloque_id == growth.id][0]
    assert g.actual_pct == Decimal("1")                 # 5000/5000 (la cripto no cuenta)


def test_endpoint_plan_e2e() -> None:
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
            # crear una posición con lote vía sesión directa
            with TS() as s:
                cartera = s.query(models.Cartera).first()
                _pos_con_lote(s, cartera, "US1", "Alpha", Decimal("1000"))
                s.commit()
            r = c.post("/api/plan", json={
                "isin": "US1", "decision": "COMPRAR", "prioridad": "ALTA",
                "capital_objetivo_eur": 1000,
            })
            assert r.status_code == 201, r.text
            pos = c.get("/api/plan/posiciones").json()
            d0 = [p for p in pos if p["isin"] == "US1"][0]
            assert d0["decision"] == "COMPRAR"
            assert d0["capital_objetivo_eur"] == "1000.00"
            # y en /api/posiciones
            met = c.get("/api/posiciones").json()
            m0 = [m for m in met["posiciones"] if m["isin"] == "US1"][0]
            assert m0["decision"] == "COMPRAR"
    finally:
        app.dependency_overrides.clear()
