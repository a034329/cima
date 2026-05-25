"""Tests de las pestañas Forex, Intereses, Bills (T-Bills) y Productos complejos.

Unit con datos sintéticos + end-to-end con el IBKR.csv real (forex realized
≈ -90.50, t-bill +7.24, intereses debit, 0 complejos).
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services.fiscal_bills import calcular_bills
from app.services.fiscal_complejos import calcular_complejos
from app.services.fiscal_forex import calcular_forex
from app.services.fiscal_intereses import calcular_intereses


IBKR_CSV = Path("/app/cima/test_data/IBKR.csv")


@pytest.fixture()
def broker_ibkr(db: Session, cartera: models.Cartera) -> models.Broker:
    b = models.Broker(user_id=cartera.user_id, broker_tipo="ibkr", alias="IBKR test")
    db.add(b)
    db.flush()
    return b


# ── Forex (Art. 33.5.e) ────────────────────────────────────────────────────


def test_forex_solo_realized_declarable(db: Session, cartera, broker_ibkr) -> None:
    for divisa, real, unreal in [("USD", "-98.05", "48.67"), ("AED", "11.01", "0")]:
        db.add(models.ResultadoIbkr(
            cartera_id=cartera.id, broker_id=broker_ibkr.id, categoria="FOREX",
            ejercicio=2026, clave=divisa, realized_eur=Decimal(real),
            unrealized_eur=Decimal(unreal), external_id=f"fx-{divisa}"))
    db.commit()
    r = calcular_forex(db, cartera.id, 2026)
    assert r.realized_total == Decimal("-87.04")     # -98.05 + 11.01
    assert r.unrealized_total == Decimal("48.67")    # latente, informativo
    assert len(r.lineas) == 2


def test_forex_acumulado_suma_por_divisa(db: Session, cartera, broker_ibkr) -> None:
    db.add(models.ResultadoIbkr(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, categoria="FOREX",
        ejercicio=2025, clave="USD", realized_eur=Decimal("10"),
        unrealized_eur=Decimal("0"), external_id="fx-2025-USD"))
    db.add(models.ResultadoIbkr(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, categoria="FOREX",
        ejercicio=2026, clave="USD", realized_eur=Decimal("5"),
        unrealized_eur=Decimal("0"), external_id="fx-2026-USD"))
    db.commit()
    r = calcular_forex(db, cartera.id, None)   # acumulado
    assert len(r.lineas) == 1
    assert r.lineas[0].divisa == "USD"
    assert r.realized_total == Decimal("15")


# ── Bills / T-Bills (RCM) ───────────────────────────────────────────────────


def test_bills_rcm_total(db: Session, cartera, broker_ibkr) -> None:
    db.add(models.ResultadoIbkr(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, categoria="TBILL",
        ejercicio=2026, clave="912797LW5", realized_eur=Decimal("7.24"),
        unrealized_eur=Decimal("0"), external_id="tb-1"))
    db.commit()
    r = calcular_bills(db, cartera.id, 2026)
    assert r.realized_total == Decimal("7.24")
    assert len(r.lineas) == 1


def test_bills_no_mezcla_con_forex(db: Session, cartera, broker_ibkr) -> None:
    db.add(models.ResultadoIbkr(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, categoria="FOREX",
        ejercicio=2026, clave="USD", realized_eur=Decimal("100"),
        unrealized_eur=Decimal("0"), external_id="fx-x"))
    db.commit()
    assert calcular_bills(db, cartera.id, 2026).realized_total == Decimal("0")


# ── Intereses (RCM 0023 vs debit no deducible) ──────────────────────────────


def _interest_tx(db, cartera, broker, isin_pos, fecha, importe, meta) -> None:
    pos = db.execute(
        models.Posicion.__table__.select().where(
            models.Posicion.isin == isin_pos)
    ).first()
    if pos is None:
        p = models.Posicion(cartera_id=cartera.id, isin=isin_pos,
                            nombre="Intereses IBKR", divisa_local="EUR")
        db.add(p); db.flush()
        pid = p.id
    else:
        pid = pos.id
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=pid,
        fecha=fecha, tipo="INTEREST", cantidad=Decimal("0"),
        precio_local=Decimal("0"), divisa_local="EUR", importe_local=importe,
        fx_rate=Decimal("1"), importe_eur=importe, gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="extracto",
        external_id=f"int-{fecha}-{importe}",
        notas=json.dumps({"interes": meta}, separators=(",", ":"))))


def test_intereses_clasifica_credit_bond_debit(db: Session, cartera, broker_ibkr) -> None:
    _interest_tx(db, cartera, broker_ibkr, "CASH-INTEREST-IBKR", date(2026, 1, 5),
                 Decimal("12.00"), {"tipo": "credit", "casilla": "0023",
                                    "descripcion": "USD Credit Interest", "divisa": "USD"})
    _interest_tx(db, cartera, broker_ibkr, "CASH-INTEREST-IBKR", date(2026, 2, 5),
                 Decimal("8.00"), {"tipo": "bond_interest", "casilla": "0023",
                                   "descripcion": "Bond coupon", "divisa": "EUR"})
    _interest_tx(db, cartera, broker_ibkr, "CASH-INTEREST-IBKR", date(2026, 3, 5),
                 Decimal("-5.00"), {"tipo": "debit", "casilla": None,
                                    "descripcion": "USD Debit Interest", "divisa": "USD"})
    db.commit()
    r = calcular_intereses(db, cartera.id, 2026)
    assert r.rcm_total == Decimal("20.00")     # credit + bond → casilla 0023
    assert r.debit_total == Decimal("-5.00")   # informativo no deducible
    assert r.neto_total == Decimal("15.00")
    assert len(r.lineas) == 3


def test_intereses_notas_no_json_infiere_por_signo(db: Session, cartera, broker_ibkr) -> None:
    # Formato antiguo: notas texto plano → infiere debit por signo negativo.
    p = models.Posicion(cartera_id=cartera.id, isin="CASH-INTEREST-IBKR",
                        nombre="Intereses IBKR", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, posicion_id=p.id,
        fecha=date(2026, 1, 1), tipo="INTEREST", cantidad=Decimal("0"),
        precio_local=Decimal("0"), divisa_local="EUR", importe_local=Decimal("-3"),
        fx_rate=Decimal("1"), importe_eur=Decimal("-3"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="extracto", external_id="int-old",
        notas="IBKR interés"))
    db.commit()
    r = calcular_intereses(db, cartera.id, 2026)
    assert r.lineas[0].tipo == "debit"
    assert r.debit_total == Decimal("-3.00")


# ── Productos complejos (detección) ─────────────────────────────────────────


def test_complejos_listado(db: Session, cartera, broker_ibkr) -> None:
    db.add(models.ProductoComplejo(
        cartera_id=cartera.id, broker_id=broker_ibkr.id, ejercicio=2026,
        fecha=date(2026, 3, 1), simbolo="ESH6", nombre="E-mini S&P fut",
        asset_category="Futures", cantidad=Decimal("1"),
        importe_eur=Decimal("5000"), external_id="cplx-1"))
    db.commit()
    r = calcular_complejos(db, cartera.id, 2026)
    assert r.n == 1
    assert r.lineas[0].asset_category == "Futures"


# ── End-to-end con IBKR.csv real ────────────────────────────────────────────


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="IBKR.csv no presente")
def test_import_ibkr_pobla_forex_bills_intereses() -> None:
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
            with open(IBKR_CSV, "rb") as f:
                r = c.post("/api/import", data={"broker_tipo": "ibkr"},
                           files={"fichero": ("IBKR.csv", f, "text/csv")})
            assert r.status_code == 200, r.text

            fx = c.get("/api/forex/2026").json()
            assert fx["realized_total"] == "-90.50"    # AED+DKK+USD realized
            assert fx["unrealized_total"] == "48.67"   # latente
            assert {l["divisa"] for l in fx["lineas"]} == {"AED", "DKK", "USD"}

            bl = c.get("/api/bills/2026").json()
            assert bl["realized_total"] == "7.24"
            assert len(bl["lineas"]) == 1

            it = c.get("/api/intereses/acumulado").json()
            assert it["n_lineas"] == 8                 # todos debit en este extracto
            assert Decimal(it["rcm_total"]) == Decimal("0")
            assert Decimal(it["debit_total"]) < 0

            cp = c.get("/api/complejos/acumulado").json()
            assert cp["n"] == 0                        # sin complejos en el extracto

            # Idempotencia: reimportar no duplica forex
            with open(IBKR_CSV, "rb") as f:
                c.post("/api/import", data={"broker_tipo": "ibkr"},
                       files={"fichero": ("IBKR.csv", f, "text/csv")})
            assert len(c.get("/api/forex/2026").json()["lineas"]) == 3
    finally:
        app.dependency_overrides.clear()
