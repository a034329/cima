"""Tests del filtro fiscal de rotación (umbrales R-U del modelo WG).

El umbral del origen es la rentabilidad anual (CAGR4+Div) que el DESTINO debe
batir para que rotar compense, dado que aflorar la plusvalía latente cuesta
impuestos. Fórmula: umbral = (1 + r_origen) · (V/(V−t))^(1/N) − 1.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services import precios
from app.services.fiscal_rotacion import (
    calcular_rotacion,
    cuota_ahorro,
    efecto_fiscal_incremental,
)


# ── Funciones puras: escala de la base del ahorro (Art. 66 LIRPF) ──────────

def test_cuota_ahorro_por_tramos() -> None:
    assert cuota_ahorro(Decimal("0")) == Decimal("0")
    assert cuota_ahorro(Decimal("-500")) == Decimal("0")        # negativo → 0
    assert cuota_ahorro(Decimal("6000")) == Decimal("1140.00")  # 6000·0,19
    # 6000·0,19 + 4000·0,21 = 1140 + 840 = 1980
    assert cuota_ahorro(Decimal("10000")) == Decimal("1980.00")
    # 1140 + 44000·0,21 + 10000·0,23 = 1140 + 9240 + 2300 = 12680
    assert cuota_ahorro(Decimal("60000")) == Decimal("12680.00")


def test_efecto_fiscal_incremental_con_signo() -> None:
    # Ganancia → coste positivo. Sobre base 0, los primeros 6000 al 19%.
    assert efecto_fiscal_incremental(Decimal("0"), Decimal("6000")) == Decimal("1140.00")
    # Ganancia que cruza el primer escalón: 1000 desde base 5500.
    # cuota(6500) − cuota(5500) = (1140 + 500·0,21) − (5500·0,19) = 1245 − 1045 = 200
    assert efecto_fiscal_incremental(Decimal("5500"), Decimal("1000")) == Decimal("200.00")
    # gp = 0 → sin efecto.
    assert efecto_fiscal_incremental(Decimal("10000"), Decimal("0")) == Decimal("0")
    # Pérdida → CRÉDITO (negativo): cuota(7000) − cuota(10000).
    # cuota(10000) = 1140 + 4000·0,21 = 1980; cuota(7000) = 1140 + 1000·0,21 = 1350.
    # efecto = 1350 − 1980 = −630.
    assert efecto_fiscal_incremental(Decimal("10000"), Decimal("-3000")) == Decimal("-630.00")


# ── Integración: posición con plusvalía vs posición sin plusvalía ──────────

def _pos(db, cartera, isin, nombre) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    return p


def _lote(db, pos, qty, coste) -> None:
    db.add(models.Lot(
        posicion_id=pos.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))


def _est(db, cartera, isin, mult, base) -> None:
    db.add(models.Estimacion(
        cartera_id=cartera.id, isin=isin, tipo_val="PER",
        eps_actual=Decimal("10"), multiplo_objetivo=Decimal(str(mult)),
        metrica_base_4y=Decimal(str(base)), dividendo_share=Decimal("0"),
    ))


def test_umbral_escala_con_ancla_fiscal(db: Session, cartera, monkeypatch) -> None:
    # Ganadora: PM 50, precio 100 → V=1000, G=500. precio_obj = 20·10 = 200.
    g = _pos(db, cartera, "US_G", "Ganadora"); _lote(db, g, 10, 500)
    _est(db, cartera, "US_G", 20, 10)
    # Sin plusvalía: PM 100, precio 100 → V=1000, G=0. precio_obj = 15·10 = 150.
    l = _pos(db, cartera, "US_L", "Plana"); _lote(db, l, 10, 1000)
    _est(db, cartera, "US_L", 15, 10)
    db.commit()

    # Precio nativo para Estimaciones (= precio actual, EUR).
    monkeypatch.setattr(
        precios, "precios_nativos",
        lambda db, cid: {"US_G": (Decimal("100"), "EUR"), "US_L": (Decimal("100"), "EUR")},
    )
    # Precio actual EUR inyectado al optimizador (sin red).
    r = calcular_rotacion(
        db, cartera.id, 2026,
        precios={"US_G": Decimal("100"), "US_L": Decimal("100")},
    )

    # Sin ventas en el ejercicio → base del ahorro de partida = 0.
    assert r.base_ahorro_actual_eur == Decimal("0.00")
    assert r.sin_estimacion == []

    items = {it.isin: it for it in r.items}
    # Mayor ancla fiscal primero (umbral 4Y desc): la ganadora encabeza.
    assert r.items[0].isin == "US_G"

    gan = items["US_G"]
    assert gan.valor_eur == Decimal("1000.00")
    assert gan.gp_latente_eur == Decimal("500.00")
    # t = cuota(500) = 500·0,19 = 95.
    assert gan.coste_fiscal_eur == Decimal("95.00")
    assert gan.tipo_efectivo_pct == Decimal("0.1900")          # 95/500
    # r_origen = (200/100)^(1/4) − 1 ≈ 18,92 %.
    assert gan.cagr4_div_origen_pct == pytest.approx(Decimal("0.1892"), abs=Decimal("0.0001"))
    # El ancla fiscal eleva el umbral por encima de r_origen, y decrece con N
    # (el coste se amortiza en más años).
    assert gan.umbral_1y_pct > gan.umbral_2y_pct > gan.umbral_3y_pct > gan.umbral_4y_pct
    assert gan.umbral_4y_pct > gan.cagr4_div_origen_pct
    assert gan.umbral_1y_pct == pytest.approx(Decimal("0.3140"), abs=Decimal("0.0002"))
    assert gan.umbral_4y_pct == pytest.approx(Decimal("0.2193"), abs=Decimal("0.0002"))

    # Posición sin plusvalía: no hay ancla fiscal → umbral = r_origen en todos
    # los horizontes (rotar solo exige batir su propio retorno esperado).
    plana = items["US_L"]
    assert plana.coste_fiscal_eur == Decimal("0.00")
    assert plana.tipo_efectivo_pct is None
    assert plana.umbral_1y_pct == plana.cagr4_div_origen_pct
    assert plana.umbral_4y_pct == plana.cagr4_div_origen_pct


def _tx(db, cartera, pos, fecha, tipo, qty, importe) -> None:
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=None, posicion_id=pos.id, fecha=fecha,
        tipo=tipo, cantidad=Decimal(str(qty)), precio_local=Decimal("0"),
        divisa_local="EUR", importe_local=Decimal(str(importe)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(importe)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual", external_id=f"{tipo}-{pos.isin}-{fecha}",
    ))


def test_perdida_baja_el_umbral_y_sube_con_n(db: Session, cartera, monkeypatch) -> None:
    """En pérdida, vender ADELANTA un crédito fiscal → umbral POR DEBAJO del
    r_origen, subiendo hacia él con el horizonte (comportamiento del Excel)."""
    ej = date.today().year
    # Plusvalía realizada este año (+1000) para que exista base del ahorro.
    cerr = _pos(db, cartera, "US_C", "Cerrada")
    _tx(db, cartera, cerr, date(ej, 1, 10), "BUY", 10, 1000)
    _tx(db, cartera, cerr, date(ej, 6, 10), "SELL", 10, 2000)
    # Posición ABIERTA en pérdida: PM 100, precio 70 → G = −300.
    perd = _pos(db, cartera, "US_L2", "Perdedora"); _lote(db, perd, 10, 1000)
    _est(db, cartera, "US_L2", 15, 10)   # precio_obj 150; con precio 70 → r≈20,9%
    db.commit()

    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_L2": (Decimal("70"), "EUR")})
    r = calcular_rotacion(db, cartera.id, ej, precios={"US_L2": Decimal("70")})

    assert r.base_ahorro_actual_eur == Decimal("1000.00")   # plusvalía realizada
    it = [x for x in r.items if x.isin == "US_L2"][0]
    assert it.gp_latente_eur == Decimal("-300.00")
    # Crédito fiscal (efecto negativo): cuota(700) − cuota(1000) = −57.
    assert it.coste_fiscal_eur == Decimal("-57.00")
    # Umbral por debajo de r_origen y creciente con N hacia él.
    assert it.umbral_1y_pct < it.umbral_2y_pct < it.umbral_3y_pct < it.umbral_4y_pct
    assert it.umbral_4y_pct < it.cagr4_div_origen_pct


def test_sin_estimacion_no_calcula_umbral(db: Session, cartera, monkeypatch) -> None:
    g = _pos(db, cartera, "US_X", "Sin estimación"); _lote(db, g, 10, 500)
    db.commit()  # sin fila Estimacion

    monkeypatch.setattr(
        precios, "precios_nativos", lambda db, cid: {"US_X": (Decimal("100"), "EUR")},
    )
    r = calcular_rotacion(db, cartera.id, 2026, precios={"US_X": Decimal("100")})

    assert "US_X" in r.sin_estimacion
    it = r.items[0]
    assert it.coste_fiscal_eur == Decimal("95.00")   # el coste fiscal sí se calcula
    assert it.cagr4_div_origen_pct is None           # pero sin retorno esperado…
    assert it.umbral_1y_pct is None                  # …no hay umbral
    assert it.umbral_4y_pct is None
