"""Tests del informe mensual (V3)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db import models
from app.services.informe_mensual import calcular_informe


@pytest.fixture()
def pos(db, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US0378331005",
                        nombre="Apple", divisa_local="USD")
    db.add(p); db.flush()
    return p


def _tx(cartera, broker, pos, fecha, tipo, importe, *, cantidad=0, precio=0,
        gastos=0, retencion=0, pais=None):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=pos.id,
        fecha=fecha, tipo=tipo, cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(precio)), divisa_local="EUR",
        importe_local=Decimal(str(importe)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(importe)), gastos_eur=Decimal(str(gastos)),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal(str(retencion)),
        retencion_pais=pais, estado="confirmada", origen="extracto",
    )


def test_flujos_del_mes(db, cartera, broker_degiro, pos, monkeypatch) -> None:
    # Sin red en tests: anula la foto IF (depende de precios).
    from app.services import impacto_if
    monkeypatch.setattr(impacto_if, "parametros_proyeccion_if",
                        lambda db, cid: (_ for _ in ()).throw(RuntimeError("sin red")))
    db.add_all([
        _tx(cartera, broker_degiro, pos, date(2026, 5, 5), "BUY", 1000,
            cantidad=10, precio=100, gastos=2),
        _tx(cartera, broker_degiro, pos, date(2026, 5, 20), "DIVIDEND", 50,
            retencion=7.5, pais="US"),
        _tx(cartera, broker_degiro, pos, date(2026, 4, 10), "BUY", 999,
            cantidad=9, precio=111),   # otro mes: fuera
    ])
    db.commit()
    r = calcular_informe(db, cartera.id, 2026, 5)
    assert r.compras_eur == Decimal("1000.00") and r.n_compras == 1
    assert r.gastos_eur == Decimal("2.00")
    assert r.dividendos_bruto_eur == Decimal("50.00")
    assert r.dividendos_neto_eur == Decimal("42.50")
    assert r.ventas_eur == Decimal("0.00")
    assert len(r.destacados) == 2          # compra + dividendo de mayo
    assert r.capital_estrategia_eur is None  # foto IF anulada


def test_gp_realizada_del_mes(db, cartera, broker_degiro, pos, monkeypatch) -> None:
    from app.services import impacto_if
    monkeypatch.setattr(impacto_if, "parametros_proyeccion_if",
                        lambda db, cid: (_ for _ in ()).throw(RuntimeError("sin red")))
    db.add_all([
        _tx(cartera, broker_degiro, pos, date(2026, 1, 10), "BUY", 1000,
            cantidad=10, precio=100),
        _tx(cartera, broker_degiro, pos, date(2026, 5, 12), "SELL", 1300,
            cantidad=10, precio=130),
    ])
    db.commit()
    r = calcular_informe(db, cartera.id, 2026, 5)
    assert r.gp_realizada_eur == Decimal("300.00")
    assert r.ventas_detalle[0].isin == "US0378331005"
    assert r.ventas_detalle[0].gp_eur == Decimal("300.00")
    # En enero no hay venta → sin G/P
    assert calcular_informe(db, cartera.id, 2026, 1).gp_realizada_eur == Decimal("0.00")


def test_mes_vacio(db, cartera, monkeypatch) -> None:
    from app.services import impacto_if
    monkeypatch.setattr(impacto_if, "parametros_proyeccion_if",
                        lambda db, cid: (_ for _ in ()).throw(RuntimeError("sin red")))
    r = calcular_informe(db, cartera.id, 2026, 3)
    assert r.n_compras == 0 and r.destacados == [] and r.ventas_detalle == []
