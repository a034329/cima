"""Tests de /comps: parse de la tabla de comparables + generar (IA web) + persistencia."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import comps as svc

_JSON = ('```json\n{"sector": "Software", "peers": ['
         '{"nombre": "Microsoft", "ticker": "MSFT", "per": 32, "ev_ebitda": 22, "p_fcf": 35,'
         ' "yield_pct": 0.008, "crecimiento_pct": 0.14, "roic_pct": 0.28, "es_objetivo": true},'
         '{"nombre": "Oracle", "ticker": "ORCL", "per": 25, "ev_ebitda": 16, "p_fcf": null,'
         ' "yield_pct": 0.012, "crecimiento_pct": 0.08, "roic_pct": 0.20, "es_objetivo": false},'
         '{"nombre": "SAP", "ticker": "SAP", "per": 28, "ev_ebitda": 18, "p_fcf": 30,'
         ' "yield_pct": 0.011, "crecimiento_pct": 0.10, "roic_pct": 0.15}'
         '], "lectura": "Microsoft cotiza con prima vs pares por mayor calidad y crecimiento.",'
         ' "fuentes": ["https://example.com/comps"]}\n```')


class _FakeIA:
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return _JSON


def _pos(db, cartera, isin, nombre) -> None:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="USD")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.commit()


def _mock(monkeypatch, isin) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {isin: {"sector": "Software", "pe": 32, "roe": 0.28}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {isin: (Decimal("400"), "USD")})
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA())


def test_parse_marca_objetivo_y_normaliza_nulls() -> None:
    c = svc.parse(_JSON, "Microsoft", "US_MSFT")
    assert c.sector == "Software" and len(c.peers) == 3
    obj = [p for p in c.peers if p.es_objetivo]
    assert len(obj) == 1 and obj[0].nombre == "Microsoft" and obj[0].per == 32.0
    orcl = [p for p in c.peers if p.ticker == "ORCL"][0]
    assert orcl.p_fcf is None and orcl.yield_pct == 0.012     # null respetado, fracción preservada
    assert c.lectura.startswith("Microsoft") and c.fuentes


def test_generar_persiste_y_relee(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_MSFT", "Microsoft")
    _mock(monkeypatch, "US_MSFT")

    assert svc.guardado(db, cartera.id, "US_MSFT") is None
    c = svc.generar(db, cartera.id, "US_MSFT")
    assert len(c.peers) == 3 and c.fecha == date.today().isoformat()

    re = svc.guardado(db, cartera.id, "US_MSFT")
    assert re is not None and len(re.peers) == 3
    assert [p for p in re.peers if p.es_objetivo][0].nombre == "Microsoft"   # reconstruido del JSON
