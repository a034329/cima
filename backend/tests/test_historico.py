"""Histórico de cierres mensuales y evolución de cartera (ADR-004).

Sin red: se mockean los fetchers de yfinance (`_fetch_cierres_mensuales`,
`_fetch_fx_mensual`) y la resolución de símbolos.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.db import models
from app.services import historico, precios


@pytest.fixture()
def pos_eur(db, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="ES0000000001",
                        nombre="EurCo", divisa_local="EUR")
    db.add(p); db.flush()
    return p


def _buy(cartera, broker, pos, fecha, cantidad, importe):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=pos.id,
        fecha=fecha, tipo="BUY", cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(importe / cantidad)), divisa_local="EUR",
        importe_local=Decimal(str(importe)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(importe)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


def _fija_fechas(monkeypatch):
    # Hoy determinista para que las series no dependan del reloj.
    monkeypatch.setattr(historico, "_hoy", lambda: date(2026, 3, 15))


def test_poblar_y_serie_eur(db, cartera, broker_degiro, pos_eur, monkeypatch):
    _fija_fechas(monkeypatch)
    db.add(_buy(cartera, broker_degiro, pos_eur, date(2026, 1, 10), 10, 1000))
    db.commit()

    monkeypatch.setattr(precios, "resolver_simbolos", lambda isines: {"ES0000000001": "EURCO"})
    # Cierres EOM en EUR para los meses pedidos.
    cierres = {"2026-01": Decimal("110"), "2026-02": Decimal("120"), "2026-03": Decimal("90")}
    monkeypatch.setattr(historico, "_fetch_cierres_mensuales",
                        lambda sim, ini, fin: [(ym, c, "EUR") for ym, c in cierres.items()])
    monkeypatch.setattr(historico, "_fetch_fx_mensual",
                        lambda base, ini, fin: [])  # no se usa (EUR)

    res = historico.poblar_historico(db, cartera.id)
    assert res["precios"] == 3
    assert db.query(models.PrecioMensual).count() == 3

    serie = historico.serie_cartera(db, cartera.id)
    by = {p.anio_mes: p for p in serie.puntos}
    assert set(by) == {"2026-01", "2026-02", "2026-03"}
    assert by["2026-01"].valor_eur == Decimal("1100")   # 10 × 110
    assert by["2026-02"].valor_eur == Decimal("1200")
    assert by["2026-03"].valor_eur == Decimal("900")
    assert by["2026-01"].aportado_eur == Decimal("1000")
    assert all(p.completo for p in serie.puntos)
    assert serie.meses_pendientes == 0


def test_serie_marca_incompleto_si_falta_cierre(db, cartera, broker_degiro, pos_eur, monkeypatch):
    _fija_fechas(monkeypatch)
    db.add(_buy(cartera, broker_degiro, pos_eur, date(2026, 1, 10), 10, 1000))
    db.commit()
    monkeypatch.setattr(precios, "resolver_simbolos", lambda isines: {"ES0000000001": "EURCO"})
    # Solo cacheamos enero → feb y marzo quedan incompletos.
    db.add(models.PrecioMensual(simbolo="EURCO", anio_mes="2026-01",
                                cierre=Decimal("110"), divisa="EUR"))
    db.commit()
    serie = historico.serie_cartera(db, cartera.id)
    by = {p.anio_mes: p for p in serie.puntos}
    assert by["2026-01"].completo and by["2026-01"].valor_eur == Decimal("1100")
    assert not by["2026-02"].completo and by["2026-02"].valor_eur == Decimal("0")


def test_valoracion_usd_aplica_fx(db, cartera, broker_degiro, monkeypatch):
    _fija_fechas(monkeypatch)
    pos = models.Posicion(cartera_id=cartera.id, isin="US0000000002",
                          nombre="UsCo", divisa_local="USD")
    db.add(pos); db.flush()
    db.add(_buy(cartera, broker_degiro, pos, date(2026, 1, 10), 4, 400))
    db.commit()
    monkeypatch.setattr(precios, "resolver_simbolos", lambda isines: {"US0000000002": "USCO"})
    db.add(models.PrecioMensual(simbolo="USCO", anio_mes="2026-01",
                                cierre=Decimal("100"), divisa="USD"))
    # rate_eur = EUR por 1 USD = 0.90
    db.add(models.FxMensual(divisa="USD", anio_mes="2026-01", rate_eur=Decimal("0.90")))
    db.commit()
    serie = historico.serie_cartera(db, cartera.id)
    by = {p.anio_mes: p for p in serie.puntos}
    # 4 × 100 USD × 0.90 = 360 EUR
    assert by["2026-01"].valor_eur == Decimal("360.0")
    assert by["2026-01"].completo


def test_valor_cartera_mes_y_variacion_informe(db, cartera, broker_degiro, pos_eur, monkeypatch):
    _fija_fechas(monkeypatch)
    db.add(_buy(cartera, broker_degiro, pos_eur, date(2026, 1, 10), 10, 1000))
    db.commit()
    monkeypatch.setattr(precios, "resolver_simbolos", lambda isines: {"ES0000000001": "EURCO"})
    db.add(models.PrecioMensual(simbolo="EURCO", anio_mes="2026-01",
                                cierre=Decimal("100"), divisa="EUR"))
    db.add(models.PrecioMensual(simbolo="EURCO", anio_mes="2026-02",
                                cierre=Decimal("110"), divisa="EUR"))
    db.commit()

    v1, c1 = historico.valor_cartera_mes(db, cartera.id, "2026-01")
    v2, c2 = historico.valor_cartera_mes(db, cartera.id, "2026-02")
    assert v1 == Decimal("1000") and c1
    assert v2 == Decimal("1100") and c2

    # El informe de febrero refleja valor EOM + variación +10% vs enero.
    from app.services import impacto_if
    from app.services.informe_mensual import calcular_informe
    monkeypatch.setattr(impacto_if, "parametros_proyeccion_if",
                        lambda db, cid: (_ for _ in ()).throw(RuntimeError("sin red")))
    r = calcular_informe(db, cartera.id, 2026, 2)
    assert r.valor_mercado_eur == Decimal("1100.00")
    assert r.valor_mercado_var_pct == Decimal("0.1000")    # +10%
    assert r.valor_mercado_completo


def test_endpoint_cartera(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import get_db
    from app.db.base import Base
    from app.main import app

    _fija_fechas(monkeypatch)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng)

    def override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(precios, "resolver_simbolos", lambda isines: {"ES0000000001": "EURCO"})
    with TS() as s:
        u = models.User(email="h@b.com", modo="saas"); s.add(u); s.flush()
        c = models.Cartera(user_id=u.id, nombre="C"); s.add(c); s.flush()
        b = models.Broker(user_id=u.id, broker_tipo="degiro", alias="DEGIRO"); s.add(b); s.flush()
        p = models.Posicion(cartera_id=c.id, isin="ES0000000001", nombre="EurCo",
                            divisa_local="EUR"); s.add(p); s.flush()
        s.add(_buy(c, b, p, date(2026, 1, 10), 10, 1000))
        s.add(models.PrecioMensual(simbolo="EURCO", anio_mes="2026-01",
                                   cierre=Decimal("110"), divisa="EUR"))
        s.commit()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as cli:
            r = cli.get("/api/historico/cartera")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["puntos"][0]["anio_mes"] == "2026-01"
            assert body["puntos"][0]["valor_eur"] == "1100.00"
            assert "job" in body and "meses_pendientes" in body
    finally:
        app.dependency_overrides.clear()
        eng.dispose()
