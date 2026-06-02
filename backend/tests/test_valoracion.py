"""Tests de la valoración asistida (parse+cálculo, proponer PER+persistencia, no-PER)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import valoracion as svc

_JSON = ('```json\n{"escenarios": ['
         '{"nombre": "conservador", "multiplo": 15, "eps_4y": 8, "razon": "PER bajo del rango histórico"},'
         '{"nombre": "base", "multiplo": 20, "eps_4y": 10, "razon": "En línea con el consenso"},'
         '{"nombre": "optimista", "multiplo": 25, "eps_4y": 12, "razon": "Expansión de múltiplo"}'
         ']}\n```')


class _FakeIA:
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return _JSON


def test_parse_calcula_precio_objetivo_y_cagr() -> None:
    esc = svc.parse(_JSON, precio_actual=100.0)
    assert len(esc) == 3
    base = [e for e in esc if e.nombre == "base"][0]
    assert base.precio_objetivo == 200.0                     # 20 × 10 (el sistema lo calcula)
    assert base.cagr4_pct is not None and abs(base.cagr4_pct - ((2 ** 0.25) - 1)) < 1e-9


def _pos_per(db, cartera, isin, tipo_val="PER") -> None:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre="Acme", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Estimacion(cartera_id=cartera.id, isin=isin, tipo_val=tipo_val,
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
                            metrica_base_4y=Decimal("10")))
    db.commit()


def _mock_precios(monkeypatch, isin) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {isin: {"sector": "Tech"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {isin: (Decimal("100"), "EUR")})
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA())


def test_proponer_per_persiste_y_relee(db: Session, cartera, monkeypatch) -> None:
    _pos_per(db, cartera, "US_X")
    _mock_precios(monkeypatch, "US_X")

    assert svc.guardado(db, cartera.id, "US_X") is None
    v = svc.proponer(db, cartera.id, "US_X")
    assert v.tipo_val == "PER" and len(v.escenarios) == 3
    assert v.anclas["eps_actual"] == 5.0
    assert v.fecha == date.today().isoformat()

    re = svc.guardado(db, cartera.id, "US_X")
    assert re is not None and len(re.escenarios) == 3
    assert re.escenarios[1].precio_objetivo == 200.0          # base reconstruido del JSON guardado


_JSON_FRE = ('{"escenarios": ['
             '{"nombre": "conservador", "multiplo": 18, "metrica_4y": 4, "razon": "P/FRE bajo del sector"},'
             '{"nombre": "base", "multiplo": 22, "metrica_4y": 5, "razon": "En línea con comparables"},'
             '{"nombre": "optimista", "multiplo": 26, "metrica_4y": 6, "razon": "Expansión de FRE"}'
             ']}')


class _FakeIAFre:
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return _JSON_FRE


def test_proponer_no_per_usa_metrica_4y(db: Session, cartera, monkeypatch) -> None:
    """Para un valor no-PER (P_FRE, p.ej. una gestora tipo BAM) la valoración ya NO
    se rechaza: propone escenarios usando el campo genérico metrica_4y."""
    _pos_per(db, cartera, "US_Y", tipo_val="P_FRE")
    _mock_precios(monkeypatch, "US_Y")
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIAFre())

    v = svc.proponer(db, cartera.id, "US_Y")
    assert v.tipo_val == "P_FRE" and len(v.escenarios) == 3
    base = [e for e in v.escenarios if e.nombre == "base"][0]
    assert base.metrica_base_4y == 5.0 and base.precio_objetivo == 110.0   # 22 × 5


def test_build_prompt_no_per_habla_de_su_multiplo() -> None:
    system, user = svc.build_prompt("BAM", {"multiplo_actual": 22, "metrica_actual": 5}, "P_FRE")
    assert "P/FRE" in system and "metrica_4y" in system
    assert "PER" not in user            # no habla de PER para un valor que no se valora por beneficios
