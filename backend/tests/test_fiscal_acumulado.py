"""Tests del modo acumulado en el servicio fiscal.

`calcular_fiscal(db, cartera_id, None)` debe procesar TODOS los años
de tx confirmadas en BD, no filtrar por ejercicio.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services.fiscal import calcular_fiscal


ISIN = "US5949181045"


def _tx(
    *, cartera, broker, posicion, fecha, tipo, cantidad, precio,
):
    importe = Decimal(str(cantidad)) * Decimal(str(precio))
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo=tipo, cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(precio)), divisa_local="EUR",
        importe_local=importe, fx_rate=Decimal("1"), importe_eur=importe,
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


def _div(*, cartera, broker, posicion, fecha, bruto, ret_es=Decimal("0")):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"),
        divisa_local="EUR", importe_local=Decimal(str(bruto)),
        fx_rate=Decimal("1"), importe_eur=Decimal(str(bruto)),
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal(str(ret_es)),
        retencion_pais="ES" if ret_es else None,
        estado="confirmada", origen="manual",
    )


@pytest.fixture()
def pos(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(
        cartera_id=cartera.id, isin=ISIN, nombre="MSFT", divisa_local="USD",
    )
    db.add(p); db.flush()
    return p


# ── Acumulado vs por año ──────────────────────────────────────────────────


def test_acumulado_suma_matches_de_todos_los_anios(
    db: Session, cartera, broker_degiro, pos,
) -> None:
    # 2023: compra
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2023, 1, 10), tipo="BUY", cantidad=100, precio=200))
    # 2024: venta parcial 50 @ 250 → G/P 2.500
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2024, 6, 10), tipo="SELL", cantidad=50, precio=250))
    # 2025: venta resto 50 @ 300 → G/P 5.000
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos,
               fecha=date(2025, 7, 15), tipo="SELL", cantidad=50, precio=300))
    db.commit()

    r_2024 = calcular_fiscal(db, cartera.id, 2024)
    r_2025 = calcular_fiscal(db, cartera.id, 2025)
    r_ac = calcular_fiscal(db, cartera.id, None)

    assert r_2024.gp_bruto == Decimal("2500")
    assert r_2025.gp_bruto == Decimal("5000")
    assert r_ac.gp_bruto == Decimal("7500"), (
        f"Acumulado = suma años: 2500 + 5000 = 7500. Got {r_ac.gp_bruto}"
    )
    assert r_ac.n_matches == r_2024.n_matches + r_2025.n_matches == 2
    # Marcado como ejercicio=0
    assert r_ac.ejercicio == 0


def test_acumulado_suma_dividendos_de_todos_los_anios(
    db: Session, cartera, broker_degiro, pos,
) -> None:
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2023, 5, 1), bruto=100))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2024, 5, 1), bruto=200, ret_es=Decimal("38")))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 5, 1), bruto=300))
    db.commit()

    r = calcular_fiscal(db, cartera.id, None)
    # 100 + (200-38) + 300 = 562
    assert r.rcm_neto == Decimal("562")


def test_acumulado_sin_datos_no_revienta(
    db: Session, cartera,
) -> None:
    r = calcular_fiscal(db, cartera.id, None)
    assert r.n_matches == 0
    assert r.gp_bruto == Decimal("0")
    assert r.rcm_neto == Decimal("0")
    assert r.ejercicio == 0


def test_calcular_fiscal_acepta_None_y_str_acumulado_en_endpoint() -> None:
    """Verifica que el endpoint expone correctamente el modo acumulado.

    Este test usa TestClient en su propio fixture (en test_fiscal_endpoint.py)
    — aquí solo aseguramos que el servicio acepta `None` como contrato.
    """
    # Sanity check del tipo: la firma debe aceptar `int | None`
    import inspect
    sig = inspect.signature(calcular_fiscal)
    ej_param = sig.parameters["ejercicio"]
    annotation = str(ej_param.annotation)
    assert "None" in annotation or "Optional" in annotation, (
        f"calcular_fiscal.ejercicio debe aceptar None — annotation={annotation}"
    )
