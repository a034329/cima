"""Tests del onboarding IA (proponer estrategia + firmar plan)."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import onboarding


def _bloque(db, cartera, nombre, cat) -> models.Bloque:
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=cat,
                      orden=1, es_base=True)
    db.add(b); db.flush()
    return b


def test_proponer_estrategia_mock(db: Session, cartera, monkeypatch) -> None:
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")
    for n, c in [("Compounders", "growth"), ("Dividend Growth", "income"),
                 ("Estable", "defensivo"), ("High Yield", "aggressive")]:
        _bloque(db, cartera, n, c)
    db.commit()

    p = onboarding.proponer_estrategia(
        db, cartera.id, {"tolerancia": "moderado", "fase": "acumulacion"})
    cats = {b.categoria_base for b in p.bloques}
    assert "growth" in cats and "income" in cats
    assert all(0.0 <= b.peso_objetivo <= 1.0 for b in p.bloques)
    assert p.disclaimer is not None          # modo saas por defecto → disclaimer


def test_viabilidad_marca_objetivo_a_2_anios_como_irreal() -> None:
    # 300k en 2 años partiendo de poco → retorno requerido desorbitado → no viable.
    v = onboarding._viabilidad(
        {"objetivo_if_eur": 300000, "horizonte_anios": 2, "aportacion_mensual_eur": 1000},
        Decimal("10000"))
    assert v is not None
    assert v.viable is False
    assert "realista" in v.veredicto.lower()
    # Retorno requerido None (imposible) o desorbitado (> 15%).
    assert v.cagr_requerido_pct is None or v.cagr_requerido_pct > 0.15


def test_capital_actual_usa_valor_de_mercado(db: Session, cartera, monkeypatch) -> None:
    """El punto de partida del retorno requerido es valor de MERCADO, no coste
    (el bug: una cartera con plusvalías disparaba el retorno requerido)."""
    from datetime import date

    p = models.Posicion(cartera_id=cartera.id, isin="US_X", nombre="Apreciada",
                        divisa_local="USD")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.commit()
    import app.services.precios as precios
    # Precio de mercado 200 €/acción → valor 2.000 (vs coste 1.000).
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid: ({"US_X": Decimal("200")}, []))

    cap = onboarding._capital_actual(db, cartera.id)
    assert cap == Decimal("2000")          # mercado, no el coste de 1.000


def test_viabilidad_holgada_si_aportaciones_ya_llegan() -> None:
    # Objetivo modesto, horizonte largo, capital+aportes ya cubren → 0% requerido.
    v = onboarding._viabilidad(
        {"objetivo_if_eur": 40000, "horizonte_anios": 10, "aportacion_mensual_eur": 400},
        Decimal("10000"))
    assert v is not None and v.viable is True and v.cagr_requerido_pct == 0.0


def test_firmar_plan_aplica_objetivos_y_versiona(db: Session, cartera) -> None:
    g = _bloque(db, cartera, "Compounders", "growth")
    db.commit()

    plan = onboarding.firmar_plan(
        db, cartera.id, {"objetivo_if_eur": 250000}, {"growth": 0.5})
    assert plan.version == 1
    db.refresh(g)
    assert g.peso_objetivo == Decimal("0.5")                 # objetivo aplicado al bloque
    assert db.get(models.Cartera, cartera.id).objetivo_if_eur == Decimal("250000")

    plan2 = onboarding.firmar_plan(db, cartera.id, {}, {"growth": 0.4})
    assert plan2.version == 2                                 # re-onboarding → nueva versión
    assert onboarding.plan_firmado_actual(db, cartera.id).version == 2
