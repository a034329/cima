"""Tests de métricas por posición + selector de columnas persistido."""
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
from app.services.fifo import rebuild_for_posicion
from app.services.posiciones import calcular_metricas_posiciones


def _rebuild_todas(db: Session, cartera_id: str) -> None:
    """Reconstruye los lots de todas las posiciones (los tests insertan tx
    directamente sin pasar por reconciliar_extracto, que es quien rebuildeа)."""
    for pos in db.execute(
        models.Posicion.__table__.select().with_only_columns(models.Posicion.id)
    ).all():
        rebuild_for_posicion(db, pos[0])
    db.commit()


def _buy(*, cartera, broker, posicion, fecha, cantidad, precio, gastos=0):
    importe = Decimal(str(cantidad)) * Decimal(str(precio))
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="BUY", cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(precio)), divisa_local="EUR", importe_local=importe,
        fx_rate=Decimal("1"), importe_eur=importe, gastos_eur=Decimal(str(gastos)),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


def _div(*, cartera, broker, posicion, fecha, bruto):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="DIVIDEND", cantidad=Decimal("0"), precio_local=Decimal("0"),
        divisa_local="EUR", importe_local=Decimal(str(bruto)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


@pytest.fixture()
def pos(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US5949181045",
                        nombre="Microsoft", divisa_local="USD")
    db.add(p); db.flush()
    return p


def test_pm_real_incluye_gastos(db, cartera, broker_degiro, pos) -> None:
    # 10 @ 100 + 5 gastos = 1005 / 10 = 100.5
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 1, 10), cantidad=10, precio=100, gastos=5))
    db.commit()
    _rebuild_todas(db, cartera.id)
    m = calcular_metricas_posiciones(db, cartera.id)
    assert len(m) == 1
    assert m[0].pm_real == Decimal("100.5000")


def test_dividendos_anio_vs_historico(db, cartera, broker_degiro, pos) -> None:
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2024, 1, 10), cantidad=10, precio=100))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 5, 1), bruto=30))
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(date.today().year, 5, 1), bruto=20))
    db.commit()
    _rebuild_todas(db, cartera.id)
    m = calcular_metricas_posiciones(db, cartera.id)[0]
    assert m.dividendos_hist == Decimal("50")
    assert m.dividendos_anio == Decimal("20")   # año en curso
    # PM desc dividendos: (1000 - 50) / 10 = 95
    assert m.pm_desc == Decimal("95.0000")


def test_opcion_ejercida_ajusta_pm_fiscal(db, cartera, broker_degiro, pos) -> None:
    """Una put vendida (prima cobrada) ejercida sobre la posición reduce el
    coste fiscal."""
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 1, 10), cantidad=10, precio=100))  # coste 1000
    # Opción ejercida con subyacente_isin = la posición, prima cobrada 50
    db.add(models.Opcion(
        cartera_id=cartera.id, broker_id=broker_degiro.id, fecha=date(2025, 6, 1),
        simbolo="MSFT P100", tipo_op="P", subyacente="MSFT", strike="100",
        vencimiento="20JUN25", accion="venta", cantidad=Decimal("1"),
        prima_unitaria=Decimal("50"), importe_eur=Decimal("50"), gastos_eur=Decimal("0"),
        expirada=False, ejercida=True, estado="confirmada", origen="extracto",
        external_id="o1", subyacente_isin="US5949181045",
    ))
    db.commit()
    _rebuild_todas(db, cartera.id)
    m = calcular_metricas_posiciones(db, cartera.id)[0]
    # PM fiscal = (1000 - 50) / 10 = 95
    assert m.pm_fiscal_es == Decimal("95.0000")
    assert m.opciones_ejercidas_hist == Decimal("50")


def _sell(*, cartera, broker, posicion, fecha, cantidad, precio):
    importe = Decimal(str(cantidad)) * Decimal(str(precio))
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="SELL", cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(precio)), divisa_local="EUR", importe_local=importe,
        fx_rate=Decimal("1"), importe_eur=importe, gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )


def test_pm_real_es_media_ponderada_no_fifo(db, cartera, broker_degiro, pos) -> None:
    """Con ventas parciales, el PM debe ser MEDIA PONDERADA MÓVIL (como el
    broker/Excel), no el coste FIFO de los lotes restantes. Compra 10@10 y
    10@20 (medio 15), vende 10: quedan 10 acciones a coste medio 15 (FIFO daría
    20, el lote nuevo)."""
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 1, 10), cantidad=10, precio=10))
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2025, 2, 10), cantidad=10, precio=20))
    db.add(_sell(cartera=cartera, broker=broker_degiro, posicion=pos,
                 fecha=date(2025, 3, 10), cantidad=10, precio=18))
    db.commit()
    _rebuild_todas(db, cartera.id)
    m = calcular_metricas_posiciones(db, cartera.id)[0]
    assert m.cantidad == Decimal("10")
    assert m.pm_real == Decimal("15.0000")     # media ponderada (FIFO daría 20)


def test_umbral_rotacion_en_metricas(db, cartera, broker_degiro, pos, monkeypatch) -> None:
    """Posición con plusvalía + estimación → la métrica trae el umbral de
    rotación (CAGR4+Div que el destino debe batir). Reusa fiscal_rotacion."""
    from app.services import precios
    # PM 50, precio actual 100 → V=1000, G=500 (plusvalía).
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2024, 1, 10), cantidad=10, precio=50))
    # Estimación: precio_obj = 20·10 = 200 → r_origen = (200/100)^(1/4)−1 ≈ 18,92 %.
    db.add(models.Estimacion(
        cartera_id=cartera.id, isin=pos.isin, tipo_val="PER",
        eps_actual=Decimal("10"), multiplo_objetivo=Decimal("20"),
        metrica_base_4y=Decimal("10"), dividendo_share=Decimal("0"),
    ))
    db.commit()
    _rebuild_todas(db, cartera.id)
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid: ({pos.isin: Decimal("100")}, []))
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {pos.isin: (Decimal("100"), "EUR")})

    m = calcular_metricas_posiciones(db, cartera.id)[0]
    # Con ancla fiscal, el umbral 4A supera el retorno propio (~18,92 %).
    assert m.umbral_rotacion_4y_pct is not None
    assert m.umbral_rotacion_4y_pct > Decimal("0.1892")
    # Decrece con el horizonte (el coste fiscal se amortiza en más años).
    assert m.umbral_rotacion_1y_pct > m.umbral_rotacion_4y_pct


# ── Endpoint + preferencias ──────────────────────────────────────────────


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


def _seed(SessionLocal):
    s = SessionLocal()
    u = models.User(email="p@p.p", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    s.commit(); s.close()


def test_endpoint_posiciones_defaults(client_y_db) -> None:
    client, SessionLocal = client_y_db
    _seed(SessionLocal)
    r = client.get("/api/posiciones")
    assert r.status_code == 200
    data = r.json()
    assert "columnas_catalogo" in data
    assert "pm_real" in data["columnas_seleccionadas"]
    # defaults esperados
    for col in ("pm_real", "opciones_ejercidas_anio", "dividendos_anio", "importe_diferido_2m"):
        assert col in data["columnas_seleccionadas"]


def test_endpoint_guardar_columnas_persiste(client_y_db) -> None:
    client, SessionLocal = client_y_db
    _seed(SessionLocal)
    # Guardar selección custom
    r = client.put("/api/posiciones/columnas", json={"columnas": ["pm_fiscal_es", "dividendos_hist"]})
    assert r.status_code == 200
    sel = r.json()["columnas_seleccionadas"]
    assert "pm_real" in sel          # se fuerza siempre
    assert "pm_fiscal_es" in sel
    assert "dividendos_hist" in sel
    assert "dividendos_anio" not in sel   # ya no está

    # Persiste entre llamadas
    r2 = client.get("/api/posiciones")
    sel2 = r2.json()["columnas_seleccionadas"]
    assert set(sel2) == set(sel)


def test_endpoint_guardar_columnas_ignora_invalidas(client_y_db) -> None:
    client, SessionLocal = client_y_db
    _seed(SessionLocal)
    r = client.put("/api/posiciones/columnas", json={"columnas": ["pm_real", "inexistente"]})
    assert r.status_code == 200
    assert "inexistente" not in r.json()["columnas_seleccionadas"]


def test_endpoint_posiciones_serializa_con_datos(client_y_db) -> None:
    """Regresión: con posiciones reales, la respuesta debe serializar (el
    dataclass PosicionMetricas necesita from_attributes en el schema)."""
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="z@z.z", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    b = models.Broker(user_id=u.id, broker_tipo="degiro", alias="DG"); s.add(b); s.flush()
    pos = models.Posicion(cartera_id=cart.id, isin="US5949181045",
                          nombre="MSFT", divisa_local="USD"); s.add(pos); s.flush()
    s.add(_buy(cartera=cart, broker=b, posicion=pos,
               fecha=date(2025, 1, 10), cantidad=10, precio=100))
    s.commit()
    rebuild_for_posicion(s, pos.id)
    s.commit(); s.close()

    r = client.get("/api/posiciones")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["posiciones"]) == 1
    p = data["posiciones"][0]
    assert p["isin"] == "US5949181045"
    assert float(p["pm_real"]) == 100.0


def test_tipo_activo_clasifica_stock_etf_crypto() -> None:
    from app.services.posiciones import _tipo_activo
    assert _tipo_activo("XF000BTC0017", "Bitcoin") == "CRYPTO"
    assert _tipo_activo("XF000SOL0012", "Solana") == "CRYPTO"
    assert _tipo_activo("IE00B4L5Y983", "iShares Core MSCI World") == "ETF"
    assert _tipo_activo("US5949181045", "Microsoft Corp") == "STOCK"


def test_rentab_total_incluye_realizada_historica(db, cartera, broker_degiro, pos, monkeypatch) -> None:
    """La 'Rentab. total histórica' incluye TODA la P&L del ISIN: latente +
    dividendos + opciones + REALIZADA en cierres anteriores. Sobre el capital
    TOTAL desplegado (Σ compras). Vender en pérdidas debe tirar del número."""
    # Compras a 100, vendes todo a 50 (pérdida 500), recompras a 50, precio actual 60.
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2024, 1, 10), cantidad=10, precio=100))   # 1000 €
    db.add(_sell(cartera=cartera, broker=broker_degiro, posicion=pos,
                 fecha=date(2024, 6, 1), cantidad=10, precio=50))     # realiza -500
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos,
                fecha=date(2024, 9, 1), cantidad=10, precio=50))     # 500 €
    db.commit()
    _rebuild_todas(db, cartera.id)
    from app.services import precios
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: ({pos.isin: Decimal("60")}, []))
    m = calcular_metricas_posiciones(db, cartera.id)[0]
    # rentab_total_pct (holding actual) NO incluye la realizada anterior:
    #   numerador = latente (60-50)*10 = 100, denominador coste_total = 500 → +20%
    assert m.rentab_total_pct == Decimal("0.2000")
    # rentab_total_hist_pct (TODA la historia del ISIN) SÍ la incluye:
    #   numerador = 100 + realizada hist (-500) = -400
    #   denominador = Σ compras = 1000 + 500 = 1500 → -400/1500 ≈ -26,67%
    assert m.rentab_total_hist_pct == Decimal("-0.2667")
