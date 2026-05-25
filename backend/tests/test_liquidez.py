"""Tests de liquidez calculada de cash flows + validación contra saldo broker.

El test de oro compara la liquidez calculada de los movimientos contra el
saldo reportado por cada broker (DEGIRO Saldo / IBKR Ending Cash) con datos
reales, y mide la fiabilidad.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models
from app.db.base import Base
from app.services.aportaciones import (
    saldo_degiro_cuenta,
    saldo_ibkr_ending_cash,
)
from app.services.liquidez import calcular_liquidez


DG_CUENTA = Path("/app/cima/test_data/Degiro_cuenta.csv")
IBKR_CSV = Path("/app/cima/test_data/IBKR.csv")


# ── Cálculo de cash flows (unitario) ──────────────────────────────────────


def test_cash_flow_basico(db: Session, cartera, broker_degiro) -> None:
    # Aportación 1000, compra 600 (+5 gastos), dividendo 30 (−5 ret), venta 200 (−2)
    db.add(models.Aportacion(cartera_id=cartera.id, broker_id=broker_degiro.id,
                             fecha=date(2026, 1, 1), importe_eur=Decimal("1000"),
                             origen="manual", external_id="ap1"))
    pos = models.Posicion(cartera_id=cartera.id, isin="US5949181045",
                          nombre="MSFT", divisa_local="USD"); db.add(pos); db.flush()
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, posicion_id=pos.id,
        fecha=date(2026, 1, 5), tipo="BUY", cantidad=Decimal("6"),
        precio_local=Decimal("100"), divisa_local="EUR", importe_local=Decimal("600"),
        fx_rate=Decimal("1"), importe_eur=Decimal("600"), gastos_eur=Decimal("5"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual", external_id="b1"))
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, posicion_id=pos.id,
        fecha=date(2026, 2, 1), tipo="DIVIDEND", cantidad=Decimal("0"),
        precio_local=Decimal("0"), divisa_local="EUR", importe_local=Decimal("30"),
        fx_rate=Decimal("1"), importe_eur=Decimal("30"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("5"),
        estado="confirmada", origen="manual", external_id="d1"))
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, posicion_id=pos.id,
        fecha=date(2026, 3, 1), tipo="SELL", cantidad=Decimal("2"),
        precio_local=Decimal("100"), divisa_local="EUR", importe_local=Decimal("200"),
        fx_rate=Decimal("1"), importe_eur=Decimal("200"), gastos_eur=Decimal("2"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual", external_id="s1"))
    db.commit()

    r = calcular_liquidez(db, cartera.id)
    # 1000 − 605 + 25 + 198 = 618
    assert r.total_calculada == Decimal("618.00")


def test_cash_flow_opciones(db: Session, cartera, broker_degiro) -> None:
    db.add(models.Opcion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, fecha=date(2026, 1, 1),
        simbolo="X P10", tipo_op="P", subyacente="X", strike="10", vencimiento="20JUN26",
        accion="venta", cantidad=Decimal("1"), prima_unitaria=Decimal("50"),
        importe_eur=Decimal("50"), gastos_eur=Decimal("1"), expirada=False,
        ejercida=False, estado="confirmada", origen="extracto", external_id="o1"))
    db.add(models.Opcion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, fecha=date(2026, 1, 2),
        simbolo="Y C5", tipo_op="C", subyacente="Y", strike="5", vencimiento="20JUN26",
        accion="compra", cantidad=Decimal("1"), prima_unitaria=Decimal("30"),
        importe_eur=Decimal("30"), gastos_eur=Decimal("1"), expirada=False,
        ejercida=False, estado="confirmada", origen="extracto", external_id="o2"))
    db.commit()
    r = calcular_liquidez(db, cartera.id)
    # venta +49, compra −31 → 18
    assert r.total_calculada == Decimal("18.00")


# ── Parsers de saldo reportado ────────────────────────────────────────────


@pytest.mark.skipif(not DG_CUENTA.is_file(), reason="cuenta DEGIRO no presente")
def test_saldo_degiro_es_ultima_fila() -> None:
    s = saldo_degiro_cuenta(DG_CUENTA)
    assert s is not None
    saldo, fecha = s
    assert saldo == Decimal("2137.40")     # validado contra el fichero real
    assert fecha == date(2026, 5, 14)


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="IBKR no presente")
def test_saldo_ibkr_ending_cash() -> None:
    s = saldo_ibkr_ending_cash(IBKR_CSV)
    assert s is not None
    assert s.quantize(Decimal("0.01")) == Decimal("6078.98")


# ── Validación calculada vs reportada ─────────────────────────────────────


def test_diferencia_se_calcula_contra_saldo_reportado(
    db: Session, cartera, broker_degiro,
) -> None:
    broker_degiro.saldo_reportado_eur = Decimal("2137.40")
    db.add(models.Aportacion(cartera_id=cartera.id, broker_id=broker_degiro.id,
                             fecha=date(2026, 1, 1), importe_eur=Decimal("2000"),
                             origen="manual", external_id="ap1"))
    db.commit()
    r = calcular_liquidez(db, cartera.id)
    b = next(x for x in r.por_broker if x.broker_id == broker_degiro.id)
    assert b.calculada == Decimal("2000.00")
    assert b.reportada == Decimal("2137.40")
    assert b.diferencia == Decimal("-137.40")   # calculada − reportada


# ── Regresión: el endpoint /api/import debe capturar el saldo del Cuenta ───
# Bug: el tempfile del Cuenta se cerraba (delete=True) justo tras parsear, antes
# de la captura de saldo → saldo_reportado quedaba None y la liquidez caía al
# fallback cash-flow (negativo en histórico DEGIRO sin depósitos SEPA).

DG_TX = Path("/app/cima/test_data/Degiro_transacciones.csv")


@pytest.mark.skipif(not (DG_TX.is_file() and DG_CUENTA.is_file()),
                    reason="extractos DEGIRO de prueba no presentes")
def test_import_degiro_captura_saldo_del_cuenta() -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool

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
            with open(DG_TX, "rb") as ft, open(DG_CUENTA, "rb") as fc:
                r = c.post(
                    "/api/import",
                    data={"broker_tipo": "degiro"},
                    files={
                        "fichero": ("Degiro_transacciones.csv", ft, "text/csv"),
                        "fichero_cuenta": ("Degiro_cuenta.csv", fc, "text/csv"),
                    },
                )
            assert r.status_code == 200, r.text
            liq = c.get("/api/liquidez").json()
            assert liq["total_reportada"] == "2137.40"
            assert liq["total_disponible"] == "2137.40"
    finally:
        app.dependency_overrides.clear()
