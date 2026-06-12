"""Tests de la calibración por retención observada (caso Francia 2026-06-12).

La estatutaria del vendor para FR es 12,8% (lo que aplica TR a personas
físicas) pero DeGiro/IBKR retienen el 25% general → el modelo debe usar lo
observado en los dividendos del usuario y caer a la estatutaria sin datos.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db import models
from app.services.dividendo_neto import (
    exceso_observado_pct, tasa_origen_observada,
)


@pytest.fixture()
def pos_fr(db, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="FR0000121014",
                        nombre="LVMH Moet Hennessy", divisa_local="EUR")
    db.add(p); db.flush()
    return p


def _div(cartera, broker, pos, fecha, bruto, ret, pais):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=pos.id,
        fecha=fecha, tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"), divisa_local="EUR",
        importe_local=Decimal(str(bruto)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal(str(ret)),
        retencion_pais=pais, estado="confirmada", origen="extracto",
    )


def test_tasa_observada_por_isin_y_pais(db, cartera, broker_degiro, pos_fr) -> None:
    hoy = date.today().year
    db.add_all([
        _div(cartera, broker_degiro, pos_fr, date(hoy, 4, 1), 100, 25, "FR"),
        _div(cartera, broker_degiro, pos_fr, date(hoy - 1, 4, 1), 200, 50, "FR"),
    ])
    db.commit()
    obs = tasa_origen_observada(db, cartera.id)
    assert obs["FR0000121014"] == Decimal("0.25")
    assert obs["pais:FR"] == Decimal("0.25")


def test_exceso_fr_observado_25_vs_estatutaria_128(db, cartera, broker_degiro,
                                                   pos_fr) -> None:
    """Sin historial la estatutaria FR (12,8%) da exceso 0; con DeGiro
    reteniendo 25% el exceso observado es 10 puntos."""
    assert exceso_observado_pct("FR", "FR0000121014", {}) == Decimal("0")
    db.add(_div(cartera, broker_degiro, pos_fr,
                date(date.today().year, 4, 1), 100, 25, "FR"))
    db.commit()
    obs = tasa_origen_observada(db, cartera.id)
    assert exceso_observado_pct("FR", "FR0000121014", obs) == Decimal("0.10")
    # Otra empresa FR sin historial propio hereda el agregado del país
    assert exceso_observado_pct("FR", "FR0000120321", obs) == Decimal("0.10")


def test_retencion_es_no_contamina_observada(db, cartera, broker_degiro,
                                             pos_fr) -> None:
    """Un dividendo con retención española (0591) no debe entrar en la tasa
    de ORIGEN observada."""
    hoy = date.today().year
    db.add_all([
        _div(cartera, broker_degiro, pos_fr, date(hoy, 4, 1), 100, 25, "FR"),
        _div(cartera, broker_degiro, pos_fr, date(hoy, 5, 1), 100, 19, "ES"),
    ])
    db.commit()
    obs = tasa_origen_observada(db, cartera.id)
    assert obs["FR0000121014"] == Decimal("0.25")


def test_ventana_excluye_dividendos_viejos(db, cartera, broker_degiro,
                                           pos_fr) -> None:
    db.add(_div(cartera, broker_degiro, pos_fr,
                date(date.today().year - 5, 4, 1), 100, 25, "FR"))
    db.commit()
    assert tasa_origen_observada(db, cartera.id) == {}


def test_fugas_proyeccion_usa_observada(db, cartera, broker_degiro, pos_fr,
                                        monkeypatch) -> None:
    """Proyección FR: 100 acc × 10 € × yield 3% × exceso OBSERVADO (25−15=10%)
    = 3 € — con la estatutaria (12,8% < 15%) habría salido 0."""
    from app.services import estimaciones, precios
    from app.services import fugas_fiscales as ff
    from app.services.fifo import rebuild_for_posicion

    hoy = date.today().year
    db.add(_div(cartera, broker_degiro, pos_fr, date(hoy, 4, 1), 100, 25, "FR"))
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, posicion_id=pos_fr.id,
        fecha=date(hoy - 1, 1, 2), tipo="BUY", cantidad=Decimal("100"),
        precio_local=Decimal("10"), divisa_local="EUR",
        importe_local=Decimal("1000"), fx_rate=Decimal("1"),
        importe_eur=Decimal("1000"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), estado="confirmada", origen="extracto",
    ))
    db.commit()
    rebuild_for_posicion(db, pos_fr.id)
    db.commit()

    class _Calc:
        isin = "FR0000121014"
        div_yield_pct = Decimal("0.03")
    monkeypatch.setattr(estimaciones, "calcular_estimaciones",
                        lambda db, cid: [_Calc()])
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid: ({"FR0000121014": Decimal("10")}, None))
    r = ff.calcular_fugas(db, cartera.id)
    p = next(x for x in r.por_pais if x.pais == "FR")
    assert p.posiciones[0].fuga_anual_estimada_eur == Decimal("3.00")
    assert p.exceso_pct == Decimal("0.10")
