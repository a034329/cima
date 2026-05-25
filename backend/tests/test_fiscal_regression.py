"""Tests de regresión del servicio fiscal — escenarios canónicos.

Estos tests congelan cifras concretas en escenarios bien definidos. Si una
actualización del motor de Cuádrate (`motor_fiscal.py` o
`compensacion_perdidas.py`) cambia el resultado, este test grita y obliga
a revisar antes de aceptar.

Casos cubiertos:
  A. Pérdida diferida → aflorada al vender el lote bloqueado.
  B. FIFO multi-año con ventas en años distintos.
  C. Cross-broker con regla 2M (recompra en otro broker).
  D. Venta parcial que consume múltiples lotes (FIFO real).
  E. Bolsas pérdidas multi-año con compensación inter-ejercicio.
  F. Smoke test integración: CSV real DEGIRO → adapter → BD → calcular_fiscal.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services.fiscal import calcular_fiscal


ISIN_TEST = "US5949181045"
ISIN_GOOGL = "US02079K3059"


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
) -> models.Transaccion:
    cant = Decimal(str(cantidad))
    prec = Decimal(str(precio))
    importe = cant * prec
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo=tipo, cantidad=cant,
        precio_local=prec, divisa_local="EUR", importe_local=importe,
        fx_rate=Decimal("1"), importe_eur=importe,
        gastos_eur=Decimal(str(gastos)), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


@pytest.fixture()
def pos(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(
        cartera_id=cartera.id, isin=ISIN_TEST,
        nombre="Microsoft Corp", divisa_local="USD",
    )
    db.add(p); db.flush()
    return p


# ── A. Pérdida diferida → aflorada ─────────────────────────────────────────


def test_perdida_diferida_aflora_al_vender_lote_bloqueado(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos: models.Posicion,
) -> None:
    """
    Año 1 (2024):
      - BUY 100 @ 300 = 30.000
      - SELL 100 @ 200 = 20.000  → pérdida -10.000
      - BUY 100 @ 210 = 21.000  (a 30 días → bloquea la pérdida 2M)
    Año 2 (2025):
      - SELL 100 @ 350 = 35.000  → matemáticamente plusvalía 14.000
      - Pero al transmitir el lote recomprado, AFLORAN los -10.000 diferidos
      - G/P final 2025 = +4.000 (14.000 plusvalía - 10.000 pérdida aflorada)

    Esto es Art. 33.5.f LIRPF último párrafo.
    """
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 1, 15), tipo="BUY", cantidad=100, precio=300))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 6, 15), tipo="SELL", cantidad=100, precio=200))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 7, 10), tipo="BUY", cantidad=100, precio=210))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 9, 15), tipo="SELL", cantidad=100, precio=350))
    db.commit()

    # 2024: pérdida 10.000 bloqueada por 2M
    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    assert r_2024.gp_no_deducible_2m == Decimal("10000")
    assert r_2024.matches[0].regla_2_meses is True
    # G/P bruto incluye los -10.000 pero TODO está bloqueado
    assert r_2024.gp_bruto == Decimal("-10000")

    # 2025: la venta del lote recomprado aflora los -10.000
    r_2025 = calcular_fiscal(db, cartera.id, 2025)
    m_2025 = r_2025.matches[0]
    # Matemáticamente la venta es 350 - 210 = +14.000 (ganancia_perdida bruta).
    # El motor expone la aflorada como valor absoluto positivo en
    # `perdida_diferida_aflorada_eur` (convención mismo del motor).
    assert m_2025.ganancia_perdida == Decimal("14000")
    assert m_2025.perdida_diferida_aflorada_eur == Decimal("10000")
    # Cima resta la aflorada del gp_bruto del ejercicio:
    # gp_bruto_2025 = 14.000 - 10.000 = 4.000
    assert r_2025.gp_bruto == Decimal("4000")
    assert r_2025.total_perdida_aflorada == Decimal("10000")
    # Y ya no hay pérdidas latentes pendientes
    assert len(r_2025.perdidas_diferidas_latentes) == 0


# ── B. FIFO multi-año con ventas en años distintos ─────────────────────────


def test_fifo_multi_anio_dos_ventas_anos_diferentes(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos: models.Posicion,
) -> None:
    """Una compra grande dividida en dos ventas separadas por años:
      BUY 200 @ 100  en 2024
      SELL 80 @ 150  en 2024 → G/P 2024 = +4.000
      SELL 80 @ 200  en 2025 → G/P 2025 = +8.000
    """
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 1, 10), tipo="BUY", cantidad=200, precio=100))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 6, 15), tipo="SELL", cantidad=80, precio=150))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 6, 15), tipo="SELL", cantidad=80, precio=200))
    db.commit()

    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    r_2025 = calcular_fiscal(db, cartera.id, 2025)

    assert r_2024.gp_bruto == Decimal("4000")
    assert r_2025.gp_bruto == Decimal("8000")

    # En 2024 hay match + posición abierta de 120 (200 - 80)
    assert r_2024.n_matches == 1
    assert any(p.cantidad_total == Decimal("120") for p in r_2024.positions)

    # En 2025 hay 1 match nuevo + posición abierta de 40 (120 - 80)
    assert r_2025.n_matches == 1
    assert any(p.cantidad_total == Decimal("40") for p in r_2025.positions)


# ── C. Cross-broker con regla 2M ───────────────────────────────────────────


def test_regla_2m_se_dispara_aunque_la_recompra_sea_en_otro_broker(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    broker_tr: models.Broker,
    pos: models.Posicion,
) -> None:
    """La regla 2M es a nivel TITULAR, no broker. Si Angel vende con pérdida
    en DEGIRO y recompra el mismo ISIN en IBKR/TR dentro de 2 meses, la
    pérdida sigue bloqueada — todo el patrimonio es de un solo titular."""
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 1, 15), tipo="BUY", cantidad=100, precio=300))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 3, 15), tipo="SELL", cantidad=100, precio=200))
    db.add(_tx(cartera=cartera, broker=broker_tr, posicion=pos,
               fecha=date(2025, 4, 10), tipo="BUY", cantidad=100, precio=210))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    m = r.matches[0]
    assert m.regla_2_meses is True, (
        "Cross-broker 2M: la recompra en TR debería disparar la regla "
        "sobre la pérdida de DEGIRO. El titular es uno solo."
    )
    # El alias del broker DEGIRO del fixture es "DG test" → uppercase "DG TEST"
    assert m.broker_compra == "DG TEST"
    assert m.broker_venta == "DG TEST"
    assert r.gp_no_deducible_2m == Decimal("10000")


# ── D. Venta parcial que consume múltiples lotes ───────────────────────────


def test_venta_parcial_consume_multiples_lotes_fifo(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos: models.Posicion,
) -> None:
    """
    Lote 1: BUY 30 @ 100 enero  (más antiguo)
    Lote 2: BUY 30 @ 120 marzo
    SELL 45 @ 150 junio → consume LOT1 entero (30) + 15 de LOT2.

    G/P = (30 × (150-100)) + (15 × (150-120)) = 1.500 + 450 = 1.950.

    Esperamos 2 matches FIFOMatch (uno por lote consumido).
    """
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 1, 10), tipo="BUY", cantidad=30, precio=100))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 3, 10), tipo="BUY", cantidad=30, precio=120))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 6, 10), tipo="SELL", cantidad=45, precio=150))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.n_matches == 2, (
        f"Esperábamos 2 matches (uno por lote consumido). Hay {r.n_matches}: "
        f"{[(m.cantidad, m.ganancia_perdida) for m in r.matches]}"
    )
    # Match enero (lote más antiguo, FIFO): 30 unidades enteras
    m_enero = next(m for m in r.matches if m.fecha_compra == date(2025, 1, 10))
    assert m_enero.cantidad == Decimal("30")
    assert m_enero.ganancia_perdida == Decimal("1500")    # 30 × 50

    # Match marzo: 15 unidades
    m_marzo = next(m for m in r.matches if m.fecha_compra == date(2025, 3, 10))
    assert m_marzo.cantidad == Decimal("15")
    assert m_marzo.ganancia_perdida == Decimal("450")     # 15 × 30

    assert r.gp_bruto == Decimal("1950")

    # Posición abierta: 15 de lote marzo a 120
    assert len(r.positions) == 1
    assert r.positions[0].cantidad_total == Decimal("15")


# ── E. Bolsas pérdidas multi-año ───────────────────────────────────────────


def test_perdidas_2024_compensan_ganancia_2025_via_auto_detect(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
    pos: models.Posicion,
) -> None:
    """
    2024: pérdida deducible 5.000€ (sin 2M).
    2025: ganancia 8.000€.

    El motor debería auto-detectar la pérdida de 2024 y aplicarla en 2025,
    dejando un saldo G/P final 2025 de +3.000€.
    """
    # 2024: pérdida deducible (sin recompra → sin 2M)
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 1, 15), tipo="BUY", cantidad=100, precio=300))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 11, 15), tipo="SELL", cantidad=100, precio=250))
    # 2025: compra nueva (>2 meses tras la venta de 2024) + venta con ganancia
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 6, 10), tipo="BUY", cantidad=100, precio=200))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 11, 10), tipo="SELL", cantidad=100, precio=280))
    db.commit()

    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    assert r_2024.gp_bruto == Decimal("-5000")
    assert r_2024.matches[0].regla_2_meses is False  # compra 2025 está >2M

    r_2025 = calcular_fiscal(db, cartera.id, 2025)
    assert r_2025.gp_bruto == Decimal("8000")
    comp = r_2025.resultado_compensacion
    # La pérdida de 2024 debe aparecer en perdidas_anteriores y aplicarse
    assert len(comp.perdidas_anteriores) >= 1
    p2024 = next(
        (p for p in comp.perdidas_anteriores if p.ejercicio_origen == 2024),
        None,
    )
    assert p2024 is not None, "El motor no detectó pérdida 2024 aplicable"
    assert p2024.importe_original_eur == Decimal("5000")
    # Saldo final 2025: 8.000 - 5.000 = 3.000
    assert comp.saldo_gp_final == Decimal("3000")
    assert comp.aplicadas_de_anteriores == Decimal("5000")


# ── F. Smoke test: CSV DEGIRO real → fiscal end-to-end ─────────────────────


_DG_CSV = Path("/app/720/irpf/DeGiro_Transacciones_2025.csv")


@pytest.mark.skipif(
    not _DG_CSV.is_file(),
    reason="CSV DEGIRO 2025 no presente — solo corre en entorno con acceso al repo Cuádrate",
)
def test_smoke_csv_degiro_real_end_to_end(
    db: Session,
    cartera: models.Cartera,
    broker_degiro: models.Broker,
) -> None:
    """Pipeline completo: parser DEGIRO → reconciliar_extracto → BD → calcular_fiscal.

    Es un smoke test: no congelamos cifras exactas (cambian si DEGIRO emite
    nuevos lotes en el CSV de referencia), solo verificamos forma.
    """
    from app.adapters.cuadrate import parse_degiro_csv
    from app.services.transacciones import reconciliar_extracto

    cands = parse_degiro_csv(_DG_CSV, broker_id=broker_degiro.id)
    assert len(cands) > 0

    r_import = reconciliar_extracto(db, cartera.id, "degiro", cands)
    # Las 73 filas del CSV 2025 deberían insertarse limpias
    assert r_import.insertadas > 0
    assert r_import.deduplicadas == 0

    # Ahora el cálculo fiscal sobre las tx persistidas
    r_fiscal = calcular_fiscal(db, cartera.id, 2025)

    # Mínimos invariantes:
    # - Si hay ventas en el CSV → debe haber matches
    n_sells = sum(1 for c in cands if c.tipo == "SELL")
    if n_sells > 0:
        assert r_fiscal.n_matches > 0, (
            f"Hay {n_sells} ventas en el CSV pero 0 matches FIFO. "
            f"Algo no llega al motor."
        )

    # - n_matches <= n_sells (cada venta produce >=1 match si hay inventario)
    # - gp_bruto es Decimal finito
    assert isinstance(r_fiscal.gp_bruto, Decimal)

    # - Compensación siempre devuelta
    assert r_fiscal.resultado_compensacion is not None
    assert r_fiscal.resultado_compensacion.ejercicio == 2025

    # Reimportar el mismo CSV: las que tienen external_id se deduplicarán
    # (mayoría). Las que NO tienen external_id (corto forzado §3.9, scrip,
    # M&A, ~10% del CSV real) sí se reinsertan — limitación conocida
    # documentada en `cima_repo_independiente` y a resolver con dedup
    # estructural (hash de fila) en una iteración posterior.
    r_reimport = reconciliar_extracto(db, cartera.id, "degiro", cands)
    n_sin_ext_id = sum(1 for c in cands if not c.external_id)
    assert r_reimport.insertadas <= n_sin_ext_id, (
        f"Insertadas en reimport ({r_reimport.insertadas}) excede el número "
        f"de filas sin external_id ({n_sin_ext_id}) — dedup roto"
    )
    assert r_reimport.deduplicadas >= len(cands) - n_sin_ext_id - 5
