"""Tests de la auditoría pre-operación (COMPRA) — offline (IA mock + feed mock)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import auditoria as svc


def _pos(db, cartera, isin, nombre, qty, coste) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0")))
    db.flush()
    return p


def _mock(monkeypatch, isin, precio, dividend=0.0) -> None:
    import app.config as cfg
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {isin: {"sector": "Technology", "industry": "Software",
                                                "pe": 30.0, "dividend": dividend, "beta": 1.1, "roe": 0.42}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {isin: (Decimal(str(precio)), "EUR")})
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: ({isin: Decimal(str(precio))}, []))
    monkeypatch.setattr(precios, "mercado_correccion", lambda: None)
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")


def _growth_est(db, cartera, isin) -> None:
    db.add(models.Estimacion(cartera_id=cartera.id, isin=isin, tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("30"),
                            metrica_base_4y=Decimal("12")))   # yield 0, crecimiento alto → growth


def test_compra_growth_luz_verde(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_MSFT", "Microsoft", 10, 1000)     # valor mercado 10×100 = 1.000
    db.add(models.Bloque(cartera_id=cartera.id, nombre="Compounders", categoria_base="growth",
                         orden=0, es_base=True))
    _growth_est(db, cartera, "US_MSFT")
    db.commit()
    _mock(monkeypatch, "US_MSFT", 100)

    a = svc.auditar_compra(db, cartera.id, "US_MSFT")
    estados = {c.filtro: c.estado for c in a.chequeos}
    assert estados["Fase"] == "OK"                          # growth en acumulación
    assert not any(c.estado == "AVISO" for c in a.chequeos)
    assert "verde" in a.resumen.lower()
    assert any(c.estado == "VERIFICAR" for c in a.chequeos)  # recordatorio cualitativo


def test_compra_high_yield_en_acumulacion_doble_aviso(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_HY", "Un REIT", 10, 1000)
    db.add(models.Bloque(cartera_id=cartera.id, nombre="High Yield", categoria_base="aggressive",
                         orden=0, es_base=True))
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_HY", tipo_val="PER",
                            dividendo_share=Decimal("9"), eps_actual=Decimal("5"),
                            multiplo_objetivo=Decimal("20"), metrica_base_4y=Decimal("6")))
    db.commit()
    _mock(monkeypatch, "US_HY", 100, dividend=9.0)           # yield 9%

    a = svc.auditar_compra(db, cartera.id, "US_HY")
    estados = {c.filtro: c.estado for c in a.chequeos}
    assert estados["Fase"] == "AVISO"                       # High Yield en acumulación
    assert estados.get("Abogado del diablo") == "AVISO"     # yield > 7%
    assert "reserva" in a.resumen.lower()


def test_compra_tamano_excede_rango(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_BIG", "Grande", 200, 14000)       # valor 200×100 = 20.000 > 13.000
    db.add(models.Bloque(cartera_id=cartera.id, nombre="Compounders", categoria_base="growth",
                         orden=0, es_base=True))
    _growth_est(db, cartera, "US_BIG")
    db.commit()
    _mock(monkeypatch, "US_BIG", 100)

    a = svc.auditar_compra(db, cartera.id, "US_BIG")
    estados = {c.filtro: c.estado for c in a.chequeos}
    assert estados["Tamaño"] == "AVISO"                     # ya en el tope del rango growth


# ── auditoría de VENTA ───────────────────────────────────────────────────────

def _con_bloque(db, cartera, isin, cat, en_estrategia=True) -> str:
    b = models.Bloque(cartera_id=cartera.id, nombre=cat, categoria_base=cat,
                      orden=0, es_base=True, en_estrategia=en_estrategia)
    db.add(b); db.flush()
    p = db.query(models.Posicion).filter_by(cartera_id=cartera.id, isin=isin).first()
    p.bloque_id = b.id
    return b.id


def test_venta_anti_churn_y_fiscal_sin_plusvalia(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_C", "Compounder", 10, 1000)
    _con_bloque(db, cartera, "US_C", "growth")
    _growth_est(db, cartera, "US_C")
    db.commit()
    _mock(monkeypatch, "US_C", 100)

    a = svc.auditar_venta(db, cartera.id, "US_C")
    estados = {c.filtro: c.estado for c in a.chequeos}
    assert estados["Anti-churn"] == "AVISO"
    assert estados["Fiscal de rotación"] == "OK"
    assert any(c.estado == "VERIFICAR" for c in a.chequeos)


def test_venta_con_plusvalia_muestra_umbrales(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_G", "Ganadora", 10, 1000)
    _con_bloque(db, cartera, "US_G", "growth")
    _growth_est(db, cartera, "US_G")
    db.commit()
    _mock(monkeypatch, "US_G", 200)

    a = svc.auditar_venta(db, cartera.id, "US_G")
    fiscal = [c for c in a.chequeos if c.filtro == "Fiscal de rotación"][0]
    assert fiscal.estado == "INFO" and "batir" in fiscal.detalle.lower()


def test_venta_colchon_regla_absoluta(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "EUR_CASH", "Monetario", 10, 1000)
    _con_bloque(db, cartera, "EUR_CASH", "colchon", en_estrategia=False)
    db.commit()
    _mock(monkeypatch, "EUR_CASH", 100)

    a = svc.auditar_venta(db, cartera.id, "EUR_CASH")
    estados = {c.filtro: c.estado for c in a.chequeos}
    assert estados["Regla del colchón"] == "AVISO"
