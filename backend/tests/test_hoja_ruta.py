"""Tests de la hoja de ruta: déficit DETERMINISTA + IA que ordena/aprobable.
La IA solo propone sobre ISINs reales (cartera/watchlist); el resto se descarta."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import hoja_ruta as svc


class _FakeIA:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def completar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return self.reply


def _bloque(db, cartera, nombre, cat, peso_obj):
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=cat,
                      orden=1, es_base=True, peso_objetivo=Decimal(str(peso_obj)))
    db.add(b); db.flush()
    return b


def _pos(db, cartera, isin, nombre, bloque, coste_total):
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre,
                        divisa_local="EUR", bloque_id=bloque.id)
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal(str(coste_total / 10)),
                      coste_total_eur=Decimal(str(coste_total)), gastos_eur=Decimal("0")))


def _mock_precios(monkeypatch, mapping) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: ({k: Decimal(str(v)) for k, v in mapping.items()}, []))
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {k: (Decimal(str(v)), "EUR") for k, v in mapping.items()})


def _setup(db, cartera, monkeypatch):
    g = _bloque(db, cartera, "Compounders", "growth", 0.7)
    i = _bloque(db, cartera, "Dividend Growth", "income", 0.3)
    _pos(db, cartera, "US_G", "GrowthCo", g, 6000)
    _pos(db, cartera, "US_I", "IncomeCo", i, 4000)
    db.commit()
    # precio = valor/acción: 10 acciones → 600€/acc growth (6.000), 400€/acc income (4.000)
    _mock_precios(monkeypatch, {"US_G": 600, "US_I": 400})


def test_analizar_deficit_es_determinista(db: Session, cartera, monkeypatch) -> None:
    _setup(db, cartera, monkeypatch)
    gaps, total = svc.analizar_deficit(db, cartera.id)
    assert total == Decimal("10000")
    by_cat = {g.categoria_base: g for g in gaps}
    # growth: objetivo 70% vs actual 60% → falta 1.000 €; income: 30% vs 40% → exceso 1.000 €
    assert abs(by_cat["growth"].deficit_eur - 1000) < 1.0
    assert abs(by_cat["income"].deficit_eur + 1000) < 1.0
    assert gaps[0].categoria_base == "growth"        # ordenado por déficit desc


def test_proponer_filtra_isins_y_decisiones_invalidas(db: Session, cartera, monkeypatch) -> None:
    _setup(db, cartera, monkeypatch)
    reply = ('{"resumen":"Reforzar growth; income en exceso.","pasos":['
             '{"isin":"US_G","decision":"reforzar","prioridad":"ALTA","capital_objetivo_eur":1000,"razon":"cubrir déficit"},'
             '{"isin":"US_BAD","decision":"COMPRAR","prioridad":"ALTA","razon":"isin inventado"},'
             '{"isin":"US_I","decision":"INVENTADA","prioridad":"ALTA","razon":"decisión inválida"}'
             ']}')
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA(reply))

    hr = svc.proponer(db, cartera.id)
    assert len(hr.pasos) == 1                         # US_BAD (inventado) y decisión inválida descartados
    p = hr.pasos[0]
    assert p.isin == "US_G" and p.decision == "REFORZAR" and p.en_cartera is True
    assert p.capital_objetivo_eur == 1000.0
    assert hr.resumen.startswith("Reforzar")
    assert hr.capital_eur == 10000.0

    # persiste y se relee con los dataclasses reconstruidos
    re = svc.guardado(db, cartera.id)
    assert re is not None and len(re.pasos) == 1 and re.pasos[0].isin == "US_G"
    assert len(re.deficit) == 2
