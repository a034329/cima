"""Tests de la pestaña Dividendos (espeja Excel Cuádrate)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services.fiscal_dividendos import calcular_dividendos


def _div(
    *, cartera, broker, posicion, fecha, bruto, retencion=Decimal("0"),
    pais=None,
) -> models.Transaccion:
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"), divisa_local="EUR",
        importe_local=Decimal(str(bruto)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal(str(retencion)),
        retencion_pais=pais, estado="confirmada", origen="extracto",
    )


@pytest.fixture()
def pos_us(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US5007541064",
                        nombre="Kraft Heinz Co", divisa_local="USD")
    db.add(p); db.flush()
    return p


@pytest.fixture()
def pos_es(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="ES0178430E18",
                        nombre="Telefonica SA", divisa_local="EUR")
    db.add(p); db.flush()
    return p


def test_dividendo_us_cdi_recuperable_limitado(
    db, cartera, broker_degiro, pos_us,
) -> None:
    """Dividendo US con retención > 15% → recuperable limitado al tope CDI (15%)."""
    # bruto 100, retención 30 (30%). CDI US-ES máx 15% → recuperable 15, exceso 15.
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2026, 5, 1), bruto=100, retencion=30, pais="US"))
    db.commit()
    r = calcular_dividendos(db, cartera.id, 2026)
    assert r.bruto_total == Decimal("100")
    kraft = next(p for p in r.resumen if p["isin"] == "US5007541064")
    assert kraft["recuperable"] == Decimal("15.00")
    assert kraft["exceso"] == Decimal("15.00")
    assert r.cdi_recuperable_total == Decimal("15.00")


def test_dividendo_es_retencion_nacional_integra(
    db, cartera, broker_degiro, pos_es,
) -> None:
    """Dividendo español: retención nacional 19% va a su campo `retencion_es`
    (casilla 0591, 100% acreditable directa), NO a CDI/0588. recuperable=0."""
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_es,
                fecha=date(2026, 6, 1), bruto=100, retencion=19, pais="ES"))
    db.commit()
    r = calcular_dividendos(db, cartera.id, 2026)
    tef = next(p for p in r.resumen if p["isin"] == "ES0178430E18")
    assert tef["es_nacional"] is True
    assert tef["retencion_es"] == Decimal("19")  # → casilla 0591
    assert tef["recuperable"] == Decimal("0")    # no es crédito CDI (0588)
    assert tef["exceso"] == Decimal("0")
    assert r.ret_es_total == Decimal("19")
    assert r.cdi_recuperable_total == Decimal("0")   # ES no va a 0588


def test_dividendos_filtra_por_anio(
    db, cartera, broker_degiro, pos_us,
) -> None:
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2025, 5, 1), bruto=50, pais="US"))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2026, 5, 1), bruto=80, pais="US"))
    db.commit()
    assert calcular_dividendos(db, cartera.id, 2025).bruto_total == Decimal("50")
    assert calcular_dividendos(db, cartera.id, 2026).bruto_total == Decimal("80")
    assert calcular_dividendos(db, cartera.id, None).bruto_total == Decimal("130")


# ── Endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_endpoint_dividendos_sin_cartera_404(client_y_db) -> None:
    client, _ = client_y_db
    assert client.get("/api/dividendos/2026").status_code == 404


def test_endpoint_dividendos_estructura(client_y_db) -> None:
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="d@d.d", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    b = models.Broker(user_id=u.id, broker_tipo="degiro", alias="DG"); s.add(b); s.flush()
    pos = models.Posicion(cartera_id=cart.id, isin="US5007541064",
                          nombre="Kraft", divisa_local="USD"); s.add(pos); s.flush()
    s.add(_div(cartera=cart, broker=b, posicion=pos,
               fecha=date(2026, 5, 1), bruto=100, retencion=15, pais="US"))
    s.commit(); s.close()

    r = client.get("/api/dividendos/2026")
    assert r.status_code == 200, r.text
    data = r.json()
    for campo in ("bruto_total", "ret_es_total", "cdi_recuperable_total",
                  "exceso_total", "bruto_ext_con_ret", "pagadores", "n_pagadores"):
        assert campo in data
    assert data["n_pagadores"] == 1
    p = data["pagadores"][0]
    for campo in ("isin", "nombre", "pais", "bruto", "ret_origen", "limite_cdi",
                  "recuperable", "exceso", "es_nacional", "brokers", "eventos"):
        assert campo in p


@pytest.fixture()
def pos_etf_irlandes(db: Session, cartera: models.Cartera) -> models.Posicion:
    # JEQP: ETF irlandés (domicilio IE) negociado en LSE, retención a cuenta
    # aplicada por broker español.
    p = models.Posicion(cartera_id=cartera.id, isin="IE000U9J8HX9",
                        nombre="JPM Nasdaq Equity Premium Income", divisa_local="GBX")
    db.add(p); db.flush()
    return p


def test_etf_extranjero_con_retencion_pais_es_no_es_nacional(
    db, cartera, broker_degiro, pos_etf_irlandes,
) -> None:
    """Regresión: un ETF irlandés con `retencion_pais='ES'` (retención a cuenta
    de broker español) NO debe contar como pagador nacional ni inflar la
    retención ES. El país se deriva del domicilio del ISIN (IE), como Cuádrate."""
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_etf_irlandes,
                fecha=date(2025, 3, 7), bruto=Decimal("1000"),
                retencion=Decimal("121"), pais="ES"))
    db.commit()
    r = calcular_dividendos(db, cartera.id, 2025)
    pagador = r.resumen[0]
    assert pagador["pais"] == "IE"
    assert pagador["es_nacional"] is False
    assert r.ret_es_total == Decimal("0")     # no es retención de pagador ES


def test_serie_dividendos_por_anio_y_mes(db, cartera, broker_degiro, pos_us) -> None:
    """La serie agrupa por año (bruto/neto) y por mes (bruto)."""
    from app.services.fiscal_dividendos import serie_dividendos
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2024, 3, 1), bruto=100, retencion=15, pais="US"))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2024, 9, 1), bruto=50, retencion=10, pais="US"))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2025, 3, 1), bruto=200, retencion=30, pais="US"))
    db.commit()
    s = serie_dividendos(db, cartera.id)
    anual = {p.anio: p for p in s.anual}
    assert anual[2024].bruto == Decimal("150.00")
    assert anual[2024].neto == Decimal("125.00")     # 150 − 25 retención
    assert anual[2025].bruto == Decimal("200.00")
    # meses: 2024-03, 2024-09, 2025-03
    meses = {(p.anio, p.mes): p.bruto for p in s.mensual}
    assert meses[(2024, 3)] == Decimal("100.00")
    assert meses[(2024, 9)] == Decimal("50.00")
    assert meses[(2025, 3)] == Decimal("200.00")


def test_diversificacion_por_empresa_pais_sector(db, cartera, broker_degiro, pos_us, pos_es, monkeypatch) -> None:
    """Reparto del dividendo bruto por empresa, país (ISIN/retención) y sector."""
    from app.services import fiscal_dividendos as fd
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2025, 3, 1), bruto=300, retencion=45, pais="US"))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_es,
                fecha=date(2025, 6, 1), bruto=100, retencion=19, pais="ES"))
    db.commit()
    # Sector inyectado (evita la red).
    from app.services import precios
    monkeypatch.setattr(precios, "sector_por_isin", lambda db, cid: {
        pos_us.isin: "Consumer Defensive", pos_es.isin: "Communication Services",
    })
    d = fd.diversificacion_dividendos(db, cartera.id, 2025)
    assert d.bruto_total == Decimal("400.00")
    assert d.por_empresa[0].clave == "Kraft Heinz Co"     # mayor pagador
    assert d.por_empresa[0].bruto == Decimal("300.00")
    paises = {t.clave: t.bruto for t in d.por_pais}
    assert paises["US"] == Decimal("300.00") and paises["ES"] == Decimal("100.00")
    sectores = {t.clave: t.bruto for t in d.por_sector}
    assert sectores["Consumer Defensive"] == Decimal("300.00")


def test_diversificacion_sin_sector_cae_en_sin_clasificar(db, cartera, broker_degiro, pos_us, monkeypatch) -> None:
    from app.services import fiscal_dividendos as fd
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(2025, 3, 1), bruto=100, retencion=15, pais="US"))
    db.commit()
    from app.services import precios
    monkeypatch.setattr(precios, "sector_por_isin", lambda db, cid: {})   # nada resuelve
    d = fd.diversificacion_dividendos(db, cartera.id, 2025)
    assert d.por_sector[0].clave == "Sin clasificar"
    assert d.por_sector[0].bruto == Decimal("100.00")
