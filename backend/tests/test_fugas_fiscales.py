"""Tests del panel de fugas fiscales (exceso CDI no recuperable)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services import fugas_fiscales as ff


def _div(*, cartera, broker, posicion, fecha, bruto, retencion, pais=None):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"), divisa_local="EUR",
        importe_local=Decimal(str(bruto)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal(str(retencion)),
        retencion_pais=pais, estado="confirmada", origen="extracto",
    )


def _buy(*, cartera, broker, posicion, cantidad):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=date(2025, 1, 2), tipo="BUY",
        cantidad=Decimal(str(cantidad)), precio_local=Decimal("10"),
        divisa_local="EUR", importe_local=Decimal(str(cantidad)) * 10,
        fx_rate=Decimal("1"), importe_eur=Decimal(str(cantidad)) * 10,
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        estado="confirmada", origen="extracto",
    )


@pytest.fixture()
def pos_ch(db: Session, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="CH0038863350",
                        nombre="Nestle SA", divisa_local="CHF")
    db.add(p); db.flush()
    return p


@pytest.fixture()
def pos_us(db: Session, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US5949181045",
                        nombre="Microsoft Corp", divisa_local="USD")
    db.add(p); db.flush()
    return p


class _Calc:
    def __init__(self, isin: str, yld: Decimal | None):
        self.isin = isin
        self.div_yield_pct = yld


def _sin_proyeccion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anula la parte de proyección (estimaciones/precios) para aislar el YTD."""
    from app.services import estimaciones, precios
    monkeypatch.setattr(estimaciones, "calcular_estimaciones", lambda db, cid: [])
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid: ({}, None))


def test_exceso_real_ch(db, cartera, broker_degiro, pos_ch, monkeypatch) -> None:
    """Suiza: bruto 100, retención 35 → tope CDI 15% ⇒ fuga real 20."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                fecha=date(date.today().year, 4, 1), bruto=100, retencion=35,
                pais="CH"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    assert r.total_exceso_real_ytd_eur == Decimal("20.00")
    assert r.por_pais[0].pais == "CH"
    assert "85" in r.por_pais[0].mecanismo  # formulario 85 ESTV
    assert r.por_pais[0].posiciones[0].exceso_real_ytd_eur == Decimal("20.00")


def test_retencion_es_no_es_fuga(db, cartera, broker_degiro, pos_ch,
                                 monkeypatch) -> None:
    """La retención española es crédito 0591, nunca fuga."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                fecha=date(date.today().year, 4, 1), bruto=100, retencion=19,
                pais="ES"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    assert r.por_pais == []
    assert r.total_exceso_real_ytd_eur == Decimal("0.00")


def test_us_dentro_de_tope_sin_fuga(db, cartera, broker_degiro, pos_us,
                                    monkeypatch) -> None:
    """US con W-8BEN (15% == tope CDI) → sin exceso real ni proyectado."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(date.today().year, 3, 1), bruto=100, retencion=15,
                pais="US"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    assert r.por_pais == []


def test_proyeccion_anual_ch(db, cartera, broker_degiro, pos_ch,
                             monkeypatch) -> None:
    """Proyección: 100 acc × 10 € × yield 3% × exceso CH (35−15 = 20%) = 6 €."""
    from app.services import estimaciones, precios
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                cantidad=100))
    db.commit()
    from app.services.fifo import rebuild_for_posicion
    rebuild_for_posicion(db, pos_ch.id)
    db.commit()
    monkeypatch.setattr(
        estimaciones, "calcular_estimaciones",
        lambda db, cid: [_Calc("CH0038863350", Decimal("0.03"))])
    monkeypatch.setattr(
        precios, "obtener_precios_eur",
        lambda db, cid: ({"CH0038863350": Decimal("10")}, None))
    r = ff.calcular_fugas(db, cartera.id)
    assert r.total_fuga_anual_estimada_eur == Decimal("6.00")
    p = r.por_pais[0]
    assert p.pais == "CH"
    x = p.posiciones[0]
    assert x.div_anual_estimado_eur == Decimal("30.00")
    assert x.fuga_anual_estimada_eur == Decimal("6.00")
