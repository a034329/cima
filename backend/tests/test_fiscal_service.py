"""Tests del servicio fiscal — Cima invocando el motor de Cuádrate.

Cubre los escenarios canónicos:
  1. Cartera vacía → resultado vacío sin reventar.
  2. Compra-venta misma cartera dentro del ejercicio → match correcto.
  3. Regla 2M: pérdida + recompra <2M → flag activo, G/P no deducible.
  4. Regla 2M: pérdida + recompra >2M → flag inactivo, G/P deducible.
  5. FIFO multi-año: compra 2024, venta 2025 → match con ejercicio_fiscal=2025.
  6. FIFO cross-broker: compra DEGIRO + compra IBKR + venta → consume el lote
     cronológicamente más antiguo independientemente del broker.
  7. RCM neto: dividendos del ejercicio - retenciones ES, brutos extranjeros
     entran enteros (la retención exterior se deduce en cuota, no en RCM neto).
  8. Compensación: pérdida patrimonial + RCM positivo → cruce 25% intra-año.
  9. Pérdida aflorada: venta del lote bloqueado libera la pérdida diferida
     en el ejercicio en que se transmite.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services.fiscal import calcular_fiscal


ISIN_MSFT = "US5949181045"
ISIN_GOOGL = "US02079K3059"


# ── Helpers ────────────────────────────────────────────────────────────────


def _tx(
    *,
    cartera: models.Cartera,
    broker: models.Broker,
    posicion: models.Posicion,
    fecha: date,
    tipo: str,
    cantidad: Decimal | int,
    precio: Decimal | int,
    gastos: Decimal | int = Decimal("0"),
    external_id: str | None = None,
) -> models.Transaccion:
    cant = Decimal(str(cantidad))
    prec = Decimal(str(precio))
    importe = cant * prec
    return models.Transaccion(
        cartera_id=cartera.id,
        broker_id=broker.id,
        posicion_id=posicion.id,
        fecha=fecha,
        tipo=tipo,
        cantidad=cant,
        precio_local=prec,
        divisa_local="EUR",
        importe_local=importe,
        fx_rate=Decimal("1"),
        importe_eur=importe,
        gastos_eur=Decimal(str(gastos)),
        tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        retencion_pais=None,
        estado="confirmada",
        origen="manual",
        external_id=external_id,
    )


def _div(
    *,
    cartera: models.Cartera,
    broker: models.Broker,
    posicion: models.Posicion,
    fecha: date,
    bruto: Decimal | int,
    retencion_es: Decimal | int = Decimal("0"),
) -> models.Transaccion:
    return models.Transaccion(
        cartera_id=cartera.id,
        broker_id=broker.id,
        posicion_id=posicion.id,
        fecha=fecha,
        tipo="DIVIDEND",
        cantidad=Decimal("0"),
        precio_local=Decimal("0"),
        divisa_local="EUR",
        importe_local=Decimal(str(bruto)),
        fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)),
        gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal(str(retencion_es)),
        retencion_pais="ES" if retencion_es else None,
        estado="confirmada",
        origen="manual",
    )


@pytest.fixture()
def pos_msft(db: Session, cartera: models.Cartera) -> models.Posicion:
    pos = models.Posicion(
        cartera_id=cartera.id,
        isin=ISIN_MSFT,
        nombre="Microsoft Corp",
        divisa_local="USD",
    )
    db.add(pos)
    db.flush()
    return pos


@pytest.fixture()
def pos_googl(db: Session, cartera: models.Cartera) -> models.Posicion:
    pos = models.Posicion(
        cartera_id=cartera.id,
        isin=ISIN_GOOGL,
        nombre="Alphabet Inc.",
        divisa_local="USD",
    )
    db.add(pos)
    db.flush()
    return pos


# ── 1. Cartera vacía ───────────────────────────────────────────────────────


def test_cartera_vacia_devuelve_resultado_vacio(
    db: Session, cartera: models.Cartera
) -> None:
    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.n_matches == 0
    assert r.gp_bruto == Decimal("0")
    assert r.gp_no_deducible_2m == Decimal("0")
    assert r.matches == []
    assert r.positions == []
    assert r.rcm_neto == Decimal("0")


# ── 2. Match básico dentro del ejercicio ───────────────────────────────────


def test_compra_venta_mismo_ejercicio_genera_match(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY",
        cantidad=10, precio=200, gastos=5,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 11, 20), tipo="SELL",
        cantidad=10, precio=300, gastos=5,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.n_matches == 1
    m = r.matches[0]
    assert m.cantidad == Decimal("10")
    assert m.fecha_compra == date(2025, 1, 15)
    assert m.fecha_venta == date(2025, 11, 20)
    # Coste = 10×200 + 5 = 2005; Transmisión = 10×300 = 3000; gastos venta = 5
    # G/P = 3000 - 2005 - 5 = 990
    assert m.ganancia_perdida == Decimal("990")
    assert m.regla_2_meses is False
    assert r.gp_bruto == Decimal("990")


# ── 3. Regla 2M activada ───────────────────────────────────────────────────


def test_perdida_con_recompra_dentro_2m_bloquea_perdida(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Compra 100@300 enero, venta 100@200 marzo (pérdida 10k), recompra
    100@210 a 25 días → regla 2M bloquea la pérdida."""
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY", cantidad=100, precio=300,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 3, 15), tipo="SELL", cantidad=100, precio=200,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 4, 9), tipo="BUY", cantidad=100, precio=210,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.n_matches == 1
    m = r.matches[0]
    assert m.ganancia_perdida == Decimal("-10000")
    assert m.regla_2_meses is True
    assert "2 meses" in m.regla_2_meses_detalle.lower() or "2m" in m.regla_2_meses_detalle.lower()

    # Pérdida 100% bloqueada — gp_no_deducible_2m absorbe los 10k.
    assert r.gp_no_deducible_2m == Decimal("10000")
    # Hay pérdida diferida latente del lote recomprado
    assert len(r.perdidas_diferidas_latentes) >= 1


# ── 4. Recompra fuera de 2M → flag falso ───────────────────────────────────


def test_perdida_con_recompra_fuera_2m_es_deducible(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Misma pérdida pero recompra 70 días después de la venta — fuera 2M.
    La pérdida es deducible."""
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY", cantidad=100, precio=300,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 3, 15), tipo="SELL", cantidad=100, precio=200,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 6, 1), tipo="BUY", cantidad=100, precio=210,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    m = r.matches[0]
    assert m.regla_2_meses is False
    assert m.ganancia_perdida == Decimal("-10000")
    assert r.gp_no_deducible_2m == Decimal("0")
    # La pérdida íntegra entra en G/P bruto deducible
    assert r.gp_bruto == Decimal("-10000")


# ── 5. FIFO multi-año ──────────────────────────────────────────────────────


def test_fifo_multi_anio_ejercicio_de_venta(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Compra 2024, venta 2025 — el ejercicio fiscal del match es el de la
    venta. Llamar calcular_fiscal(2024) NO ve el match; calcular_fiscal(2025) sí."""
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2024, 6, 10), tipo="BUY", cantidad=50, precio=200,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 8, 15), tipo="SELL", cantidad=50, precio=300,
    ))
    db.commit()

    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    assert r_2024.n_matches == 0   # la venta es de 2025

    r_2025 = calcular_fiscal(db, cartera.id, 2025)
    assert r_2025.n_matches == 1
    assert r_2025.matches[0].ejercicio_fiscal == 2025
    assert r_2025.gp_bruto == Decimal("5000")   # 50 × (300 − 200)


# ── 6. FIFO cross-broker ───────────────────────────────────────────────────


def test_fifo_cross_broker_consume_lote_mas_antiguo(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    broker_tr: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Compra IBKR enero @200, compra DEGIRO marzo @300, venta DEGIRO junio @400.
    FIFO global consume el lote IBKR de enero (G/P alta) — no el lote DEGIRO
    de marzo. La plusvalía correcta es 50×(400−200) = 10.000."""
    db.add(_tx(
        cartera=cartera, broker=broker_tr, posicion=pos_msft,
        fecha=date(2025, 1, 10), tipo="BUY", cantidad=50, precio=200,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 3, 15), tipo="BUY", cantidad=50, precio=300,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 6, 20), tipo="SELL", cantidad=50, precio=400,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.n_matches == 1
    m = r.matches[0]
    assert m.fecha_compra == date(2025, 1, 10), (
        f"FIFO debió consumir el lote enero (cross-broker más antiguo), "
        f"no el de marzo. fecha_compra = {m.fecha_compra}"
    )
    assert m.ganancia_perdida == Decimal("10000")
    # Y la posición restante son los 50 lotes de DEGIRO marzo
    assert len(r.positions) == 1
    assert r.positions[0].cantidad_total == Decimal("50")
    assert r.positions[0].pm_ponderado_eur == Decimal("300.0000000000")


# ── 7. RCM neto: brutos - retenciones ES ───────────────────────────────────


def test_rcm_neto_dividendos_descuenta_retencion_es(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """500€ bruto dividendos + 95€ retención ES → RCM neto = 405€.
    Otro dividendo de 300€ bruto sin retención ES → suma 300€ enteros."""
    db.add(_div(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 4, 15),
        bruto=500, retencion_es=95,
    ))
    db.add(_div(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 10, 20),
        bruto=300, retencion_es=0,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.rcm_neto == Decimal("705")    # (500-95) + 300


def test_rcm_neto_ignora_dividendos_de_otro_ejercicio(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    db.add(_div(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2024, 12, 30), bruto=1000,
    ))
    db.add(_div(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 2), bruto=500,
    ))
    db.commit()

    r_2025 = calcular_fiscal(db, cartera.id, 2025)
    assert r_2025.rcm_neto == Decimal("500")
    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    assert r_2024.rcm_neto == Decimal("1000")


# ── 8. Compensación intra-año: pérdida G/P + RCM positivo ──────────────────


def test_compensacion_perdida_gp_cruza_25pct_a_rcm(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Pérdida patrimonial 4.000€ + RCM positivo 10.000€ → puede cruzar hasta
    el 25% del RCM, es decir 2.500€. Restante pérdida G/P arrastrable."""
    # Pérdida patrimonial: 50@300 → 50@220 = -4.000
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY", cantidad=50, precio=300,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 11, 15), tipo="SELL", cantidad=50, precio=220,
    ))
    # RCM positivo: 10.000€ en dividendos
    db.add(_div(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 6, 1), bruto=10000,
    ))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.gp_bruto == Decimal("-4000")
    assert r.rcm_neto == Decimal("10000")

    comp = r.resultado_compensacion
    # 25% RCM = 2.500 → ese tope cruza desde G/P negativo
    assert comp.cruce_gp_a_rcm == Decimal("2500")
    # Saldo RCM tras cruce = 10.000 - 2.500 = 7.500
    assert comp.saldo_rcm_tras_cruce == Decimal("7500")
    # Saldo G/P final tras cruce = -4.000 + 2.500 = -1.500 (a arrastrar)
    assert comp.saldo_gp_tras_cruce == Decimal("-1500")
    assert comp.nuevo_saldo_negativo == Decimal("1500")


# ── 9. Robustez: tx pendiente_confirmar no entra al cálculo ─────────────────


def test_tx_pendiente_confirmar_no_entra_al_fiscal(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    """Solo las transacciones confirmadas afectan al cálculo fiscal —
    una manual pendiente_confirmar debe ignorarse."""
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY", cantidad=10, precio=200,
    ))
    venta_pendiente = _tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 6, 15), tipo="SELL", cantidad=10, precio=300,
    )
    venta_pendiente.estado = "pendiente_confirmar"
    db.add(venta_pendiente)
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    # Ninguna venta confirmada → 0 matches
    assert r.n_matches == 0
    # La posición queda abierta con 10 unidades
    assert len(r.positions) == 1
    assert r.positions[0].cantidad_total == Decimal("10")


# ── 10. Idempotencia: dos llamadas seguidas devuelven lo mismo ─────────────


def test_idempotente_dos_llamadas(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos_msft: models.Posicion,
) -> None:
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 1, 15), tipo="BUY", cantidad=10, precio=200,
    ))
    db.add(_tx(
        cartera=cartera, broker=broker_degiro, posicion=pos_msft,
        fecha=date(2025, 11, 20), tipo="SELL", cantidad=10, precio=300,
    ))
    db.commit()

    r1 = calcular_fiscal(db, cartera.id, 2025)
    r2 = calcular_fiscal(db, cartera.id, 2025)
    assert r1.gp_bruto == r2.gp_bruto
    assert r1.n_matches == r2.n_matches
    assert [m.ganancia_perdida for m in r1.matches] == [
        m.ganancia_perdida for m in r2.matches
    ]
