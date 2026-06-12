"""Tests de las alertas plan↔precio (V4)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import models
from app.services import vigilancia


@pytest.fixture()
def pos(db, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US5949181045",
                        nombre="Microsoft Corp", divisa_local="USD")
    db.add(p); db.flush()
    return p


def _paso(db, cartera, isin, decision, gatillo, estado="PENDIENTE"):
    p = models.PlanPaso(cartera_id=cartera.id, isin=isin, decision=decision,
                        prioridad="ALTA", estado=estado,
                        precio_alerta_eur=Decimal(str(gatillo)))
    db.add(p); db.flush()
    return p


def _con_precio(monkeypatch, isin, px):
    monkeypatch.setattr(vigilancia, "_precios_actuales",
                        lambda db, cid: {isin: Decimal(str(px))})


def test_comprar_dispara_al_caer(db, cartera, pos, monkeypatch) -> None:
    _paso(db, cartera, pos.isin, "COMPRAR", 300)
    db.commit()
    _con_precio(monkeypatch, pos.isin, 295)
    a = vigilancia.evaluar_plan_precio(db, cartera.id)
    assert len(a) == 1 and a[0].decision == "COMPRAR"
    assert a[0].precio_actual_eur == Decimal("295")


def test_comprar_no_dispara_por_encima(db, cartera, pos, monkeypatch) -> None:
    _paso(db, cartera, pos.isin, "COMPRAR", 300)
    db.commit()
    _con_precio(monkeypatch, pos.isin, 310)
    assert vigilancia.evaluar_plan_precio(db, cartera.id) == []


def test_vender_dispara_al_subir(db, cartera, pos, monkeypatch) -> None:
    _paso(db, cartera, pos.isin, "VENDER", 400)
    db.commit()
    _con_precio(monkeypatch, pos.isin, 405)
    a = vigilancia.evaluar_plan_precio(db, cartera.id)
    assert len(a) == 1 and a[0].decision == "VENDER"


def test_paso_completado_no_dispara(db, cartera, pos, monkeypatch) -> None:
    _paso(db, cartera, pos.isin, "COMPRAR", 300, estado="COMPLETADO")
    db.commit()
    _con_precio(monkeypatch, pos.isin, 100)
    assert vigilancia.evaluar_plan_precio(db, cartera.id) == []


def test_sin_gatillo_no_consulta_precios(db, cartera, pos, monkeypatch) -> None:
    # Sin pasos con gatillo no debe ni pedir precios (early return)
    def _boom(db, cid):
        raise AssertionError("no debería pedir precios")
    monkeypatch.setattr(vigilancia, "_precios_actuales", _boom)
    assert vigilancia.evaluar_plan_precio(db, cartera.id) == []
