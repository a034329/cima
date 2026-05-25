"""Test del cálculo de estimaciones (multi-método WG) — offline."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services import estimaciones as svc


def _pos(db, cartera, isin, nombre, qty, coste, precio_manual) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR",
                        precio_manual_eur=Decimal(str(precio_manual)))
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def test_cagr4_y_yield(db: Session, cartera, monkeypatch) -> None:
    p = _pos(db, cartera, "US_A", "Alpha", 10, 1000, 100)   # precio manual 100
    db.add(models.Estimacion(
        cartera_id=cartera.id, isin="US_A", tipo_val="PER",
        eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
        metrica_base_4y=Decimal("10"), dividendo_share=Decimal("3"),
    ))
    db.commit()
    # Precio nativo = precio manual (EUR) inyectado vía monkeypatch del helper.
    monkeypatch.setattr(svc, "calcular_estimaciones", svc.calcular_estimaciones)
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_A": (Decimal("100"), "EUR")})

    calcs = svc.calcular_estimaciones(db, cartera.id)
    c = [x for x in calcs if x.isin == "US_A"][0]
    # precio objetivo = 20 × 10 = 200
    assert c.precio_objetivo == Decimal("200")
    # CAGR4 = (200/100)^(1/4) - 1 ≈ 0.1892
    assert abs(c.cagr4_pct - Decimal("0.1892")) < Decimal("0.001")
    # yield = 3/100 = 0.03
    assert abs(c.div_yield_pct - Decimal("0.03")) < Decimal("0.0001")
    # CAGR4+Div ≈ 0.2192
    assert abs(c.cagr4_div_pct - Decimal("0.2192")) < Decimal("0.001")
    # crecimiento = (10/5)^(1/4)-1 ≈ 0.1892
    assert abs(c.crecimiento_pct - Decimal("0.1892")) < Decimal("0.001")


def test_prefill_multiplo_consenso_y_guardarrail_no_per(db: Session, cartera, monkeypatch) -> None:
    """El prefill siembra multiplo = target_menor / EPS_forward y metrica_4A = EPS
    consenso, pero SOLO para tipo PER. P_FCF/P_BV/P_FRE quedan manuales."""
    _pos(db, cartera, "US_PER", "PerCo", 10, 1000, 100)
    cfcf = _pos(db, cartera, "US_FCF", "FcfCo", 10, 1000, 50)
    # FcfCo se valora por P_FCF → no debe autorrellenarse desde EPS.
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_FCF", tipo_val="P_FCF"))
    db.commit()

    import app.services.precios as precios
    cons = {
        "US_PER": {"precio_obj_consenso": 200.0, "eps_forward": 5.0, "eps_consenso_4y": 10.0,
                   "num_analistas_eps": 12, "anio_consenso_4y": 2030, "per_hist_medio": 18.0},
        "US_FCF": {"precio_obj_consenso": 300.0, "eps_forward": 2.0, "eps_consenso_4y": 3.0},
    }
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {})

    svc.prefill_estimaciones(db, cartera.id)

    per = db.execute(
        select(models.Estimacion).where(models.Estimacion.isin == "US_PER")
    ).scalars().first()
    assert per.multiplo_objetivo == Decimal("40.0000")     # 200/5 (sin histórico → consenso)
    assert per.metrica_base_4y == Decimal("10.0000")       # EPS consenso 4A
    assert per.consenso_json is not None                   # referencia guardada

    fcf = db.execute(
        select(models.Estimacion).where(models.Estimacion.isin == "US_FCF")
    ).scalars().first()
    assert fcf.multiplo_objetivo is None                   # guardarraíl: no PER
    assert fcf.metrica_base_4y is None
    assert fcf.consenso_json is not None                   # pero sí guarda referencia


def test_multiplo_normalizado_min_y_alerta(db: Session, cartera, monkeypatch) -> None:
    """El múltiplo por defecto = min(consenso forward, histórico mediano≥3 años),
    acotado [5,45]; si divergen >30% se marca alerta (posible re-rating)."""
    _pos(db, cartera, "US_RR", "ReRater", 10, 1000, 300)
    db.commit()
    import app.services.precios as precios
    # consenso forward = 360/10 = 36×; histórico mediano = 24× (n=5) → min = 24×
    cons = {"US_RR": {"precio_obj_consenso": 360.0, "eps_forward": 10.0,
                      "eps_consenso_4y": 15.0, "per_hist_mediano": 24.0, "per_hist_n": 5}}
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_RR": (Decimal("300"), "USD")})

    svc.prefill_estimaciones(db, cartera.id)
    e = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_RR")).scalars().first()
    assert e.multiplo_objetivo == Decimal("24.0000")       # min(36, 24)

    c = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_RR"][0]
    assert c.mult_alerta is not None and "re-rating" in c.mult_alerta   # 36 vs 24 → diverge 50%


def test_heuristica_defensiva_metrica_contable(db: Session, cartera, monkeypatch) -> None:
    """Industria de métrica contable (gestora/BDC/REIT) → el prefill NO siembra
    múltiplo/métrica como PER y marca para revisión. Un PER normal sí se siembra.
    Al confirmar el tipo_val, la marca se levanta y el prefill vuelve a sembrar."""
    _pos(db, cartera, "US_AM", "AltMgr", 10, 1000, 50)    # gestora alternativa (P/FRE real)
    _pos(db, cartera, "US_TECH", "TechCo", 10, 1000, 100)  # PER normal
    db.commit()

    import app.services.precios as precios
    funds = {
        "US_AM": {"eps": 2.0, "forward_eps": 2.2, "dividend": 1.5, "pe": 12.0,
                  "industry": "Asset Management"},
        "US_TECH": {"eps": 5.0, "forward_eps": 5.5, "dividend": None, "pe": 25.0,
                    "industry": "Software - Infrastructure"},
    }
    cons = {
        "US_AM": {"precio_obj_consenso": 60.0, "eps_forward": 2.2, "eps_consenso_4y": 3.0},
        "US_TECH": {"precio_obj_consenso": 150.0, "eps_forward": 5.5, "eps_consenso_4y": 8.0},
    }
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: funds)
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_AM": (Decimal("50"), "USD"),
                                         "US_TECH": (Decimal("100"), "USD")})

    svc.prefill_estimaciones(db, cartera.id)

    am = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_AM")).scalars().first()
    assert am.multiplo_objetivo is None      # NO sembrado como PER
    assert am.metrica_base_4y is None
    assert am.dividendo_share == Decimal("1.500000")   # el dividendo sí (yield válido)
    cam = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_AM"][0]
    assert cam.mult_alerta is not None and "tipo_val" in cam.mult_alerta

    tech = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_TECH")).scalars().first()
    assert tech.multiplo_objetivo is not None    # PER normal sí se siembra
    assert tech.metrica_base_4y == Decimal("8.0000")

    # Confirmar el tipo_val (incl. PER) levanta la marca y autoriza el sembrado.
    import json
    meta = json.loads(am.consenso_json)
    meta["tipo_confirmado"] = True
    meta.pop("revisar_tipo_val", None)
    am.consenso_json = json.dumps(meta)
    db.commit()
    svc.prefill_estimaciones(db, cartera.id)
    am2 = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_AM")).scalars().first()
    assert am2.multiplo_objetivo is not None      # ya confirmado → siembra
    assert am2.metrica_base_4y == Decimal("3.0000")


def test_sin_inputs_no_calcula(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_B", "Beta", 10, 1000, 70)
    db.commit()
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_B": (Decimal("70"), "EUR")})
    calcs = svc.calcular_estimaciones(db, cartera.id)
    c = [x for x in calcs if x.isin == "US_B"][0]
    assert c.precio_objetivo is None
    assert c.cagr4_div_pct is None


def test_crecimiento_eps_serie_historico_mas_forward_acotado() -> None:
    """CAGR sobre [histórico + forward], acotado a banda; sin serie → 0%."""
    from app.services.estimaciones import _crecimiento_eps, _BANDA_CAGR_EPS
    lo, hi = _BANDA_CAGR_EPS
    # Otis-like: [2.96,3.39,4.07,3.50] + forward 4.72 → CAGR (4.72/2.96)^(1/4)-1 ≈ 12,4%
    g = _crecimiento_eps([2.96, 3.39, 4.07, 3.50], 4.72)
    assert abs(g - 0.124) < 0.01
    assert lo <= g <= hi
    # Crecimiento disparado → se capa al máximo de la banda.
    assert _crecimiento_eps([10.0], 30.0) == hi      # (30/10)^(1/1)-1 = 200% → cap
    # Sin serie utilizable → 0% (proyección plana).
    assert _crecimiento_eps([], None) == 0.0
    assert _crecimiento_eps([5.0], None) == 0.0
    # Caída fuerte → suelo de la banda.
    assert _crecimiento_eps([10.0], 2.0) == lo
