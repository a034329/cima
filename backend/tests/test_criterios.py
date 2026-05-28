"""Tests del chequeo de encaje: criterios medibles + evaluar_candidato."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.ia.base import ContextoEmpresa
from app.db import models
from app.services import clasificador as clasif
from app.services import criterios


# ── evaluar_criterios (puro, sin BD) ─────────────────────────────────────────

def _por_etiqueta(ctx, cat) -> dict[str, criterios.CriterioCheck]:
    return {c.etiqueta: c for c in criterios.evaluar_criterios(ctx, cat)}


def test_growth_ejemplo_limpio() -> None:
    ctx = ContextoEmpresa(isin="x", nombre="x", yield_pct=0.01,
                          crecimiento_eps_pct=0.20, roe=0.30)
    checks = criterios.evaluar_criterios(ctx, "growth")
    assert len(checks) == 3
    assert all(c.cumple for c in checks)


def test_growth_falla_por_yield_alto() -> None:
    ctx = ContextoEmpresa(isin="x", nombre="x", yield_pct=0.08,
                          crecimiento_eps_pct=0.20, roe=0.30)
    chk = _por_etiqueta(ctx, "growth")
    assert chk["Yield"].cumple is False          # 8% > 3%
    assert chk["Crecimiento BPA"].cumple is True


def test_defensivo_respeta_beta() -> None:
    ok = _por_etiqueta(ContextoEmpresa(isin="x", nombre="x", yield_pct=0.04, beta=0.7), "defensivo")
    assert ok["Beta"].cumple is True and ok["Yield"].cumple is True
    alto = _por_etiqueta(ContextoEmpresa(isin="x", nombre="x", yield_pct=0.04, beta=1.3), "defensivo")
    assert alto["Beta"].cumple is False          # beta 1,3 > 0,9


def test_aggressive_yield_minimo() -> None:
    assert criterios.evaluar_criterios(
        ContextoEmpresa(isin="x", nombre="x", yield_pct=0.08), "aggressive")[0].cumple is True
    assert criterios.evaluar_criterios(
        ContextoEmpresa(isin="x", nombre="x", yield_pct=0.03), "aggressive")[0].cumple is False


def test_dato_no_disponible_no_cuenta() -> None:
    # yield presente, crecimiento y ROE ausentes → None (no se puntúan).
    chk = _por_etiqueta(ContextoEmpresa(isin="x", nombre="x", yield_pct=0.01), "growth")
    assert chk["Yield"].cumple is True
    assert chk["Crecimiento BPA"].cumple is None
    assert chk["ROE"].cumple is None


def test_categoria_sin_umbrales_no_tiene_checks() -> None:
    assert criterios.evaluar_criterios(ContextoEmpresa(isin="x", nombre="x"), "indice") == []


# ── evaluar_candidato (servicio, con BD + IA mock) ───────────────────────────

def _mock_feed(monkeypatch, isin: str, precio: float) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {isin: {"sector": "Technology", "industry": "Software",
                                                "pe": 30.0, "dividend": 0.0,
                                                "beta": 1.1, "roe": 0.42}})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {isin: (Decimal(str(precio)), "USD")})


def _seed_growth(db, cartera) -> None:
    p = models.Posicion(cartera_id=cartera.id, isin="US_MSFT", nombre="Microsoft",
                        divisa_local="USD", precio_manual_eur=Decimal("100"))
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Bloque(cartera_id=cartera.id, nombre="Compounders",
                         categoria_base="growth", orden=0, es_base=True))
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_MSFT", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("30"),
                            metrica_base_4y=Decimal("12")))
    db.commit()


def test_evaluar_candidato_ejemplo_limpio(db: Session, cartera, monkeypatch) -> None:
    _seed_growth(db, cartera)
    _mock_feed(monkeypatch, "US_MSFT", 100)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")

    ev = clasif.evaluar_candidato(db, cartera.id, "US_MSFT")
    assert ev.categoria_sugerida == "growth"     # yield 0, crecimiento alto → mock
    assert ev.n_medibles >= 2 and ev.n_cumplidos == ev.n_medibles
    assert "limpio" in ev.veredicto.lower()
    assert ev.criterios_texto                     # texto humano de la ficha
    assert ev.cubre_target is None                # sin target


def test_evaluar_candidato_avisa_si_no_cubre_target(db: Session, cartera, monkeypatch) -> None:
    _seed_growth(db, cartera)
    _mock_feed(monkeypatch, "US_MSFT", 100)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")

    # Buscabas High Yield, pero la IA lo ve como Compounder → no cubre el déficit.
    ev = clasif.evaluar_candidato(db, cartera.id, "US_MSFT", target_categoria="aggressive")
    assert ev.categoria_sugerida == "growth"
    assert ev.cubre_target is False
    assert "no cubre" in ev.veredicto.lower()
