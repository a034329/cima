"""Tests de la vigilancia de cartera (baseline, alertas, marcar visto)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import vigilancia as svc


def _pos(db, cartera, isin, nombre) -> None:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))


def _precios(monkeypatch, mapping) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: ({k2: Decimal(str(v)) for k2, v in mapping.items()}, []))


def test_vigilancia_flujo_completo(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_A", "Alpha")
    _pos(db, cartera, "US_B", "Beta")
    _pos(db, cartera, "US_C", "Gamma")
    db.commit()

    # 1) primer uso: crea baseline a 100, sin alertas
    _precios(monkeypatch, {"US_A": 100, "US_B": 100, "US_C": 100})
    alertas, desde = svc.evaluar(db, cartera.id)
    assert alertas == [] and desde is None

    # 2) A +12% (CRÍTICA), B +6% (ALERTA), C +2% (nada)
    _precios(monkeypatch, {"US_A": 112, "US_B": 106, "US_C": 102})
    alertas, desde = svc.evaluar(db, cartera.id)
    niveles = {a.isin: a.nivel for a in alertas}
    assert niveles == {"US_A": "CRITICA", "US_B": "ALERTA"}      # C no alerta
    assert alertas[0].isin == "US_A"                            # ordenado por |Δ| desc
    assert abs(float(alertas[0].cambio_pct) - 0.12) < 1e-6

    # 3) no se actualizó el baseline: vuelve a alertar igual
    assert len(svc.evaluar(db, cartera.id)[0]) == 2

    # 4) marcar visto → baseline = actuales → sin alertas
    svc.marcar_visto(db, cartera.id)
    assert svc.evaluar(db, cartera.id)[0] == []


def test_asesor_contexto_incluye_alertas(db: Session, cartera, monkeypatch) -> None:
    from app.services import asesor
    _pos(db, cartera, "US_A", "Alpha")
    db.commit()
    # baseline a 100, luego sube a 120 → CRÍTICA
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {"US_A": {"sector": "Tech"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_A": (Decimal("120"), "EUR")})
    _precios(monkeypatch, {"US_A": 100})
    svc.evaluar(db, cartera.id)                 # crea baseline a 100
    _precios(monkeypatch, {"US_A": 120})        # ahora 120 (+20%)
    ctx = asesor._contexto(db, cartera.id)
    assert "ALERTAS DE VIGILANCIA" in ctx and "Alpha" in ctx
