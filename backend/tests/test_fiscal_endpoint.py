"""Tests del endpoint GET /api/fiscal/{ejercicio} con TestClient.

Validan:
  - Status codes (400 fuera de rango, 404 sin cartera, 200 normal).
  - Forma del JSON (matches, positions, compensación).
  - Que la serialización Pydantic no pierde campos del motor.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app


@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    """TestClient con BD SQLite en memoria fresh por test.

    Usa StaticPool para que TODAS las conexiones compartan el mismo
    `:memory:` (de lo contrario cada Session abre un DB nuevo y vacío).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True,
    )

    def override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_basico(SessionLocal: sessionmaker) -> str:
    """Crea user + cartera + broker + posición. Devuelve cartera_id."""
    s: Session = SessionLocal()
    user = models.User(email="t@cima.local", modo="owner")
    s.add(user); s.flush()
    cart = models.Cartera(user_id=user.id, nombre="Test")
    s.add(cart); s.flush()
    broker = models.Broker(user_id=user.id, broker_tipo="degiro", alias="DG")
    s.add(broker); s.flush()
    pos = models.Posicion(
        cartera_id=cart.id, isin="US5949181045",
        nombre="Microsoft", divisa_local="USD",
    )
    s.add(pos); s.flush()
    cart_id = cart.id
    s.commit()
    s.close()
    return cart_id


def _add_tx(
    SessionLocal: sessionmaker,
    cartera_id: str,
    fecha: date,
    tipo: str,
    cantidad: Decimal | int,
    precio: Decimal | int,
) -> None:
    s: Session = SessionLocal()
    broker = s.query(models.Broker).first()
    pos = s.query(models.Posicion).first()
    cant = Decimal(str(cantidad))
    prec = Decimal(str(precio))
    importe = cant * prec
    s.add(models.Transaccion(
        cartera_id=cartera_id, broker_id=broker.id, posicion_id=pos.id,
        fecha=fecha, tipo=tipo, cantidad=cant,
        precio_local=prec, divisa_local="EUR", importe_local=importe,
        fx_rate=Decimal("1"), importe_eur=importe,
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    ))
    s.commit()
    s.close()


# ── Status codes ───────────────────────────────────────────────────────────


def test_get_fiscal_sin_cartera_devuelve_404(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, _ = client_y_db
    r = client.get("/api/fiscal/2025")
    assert r.status_code == 404
    assert "cartera" in r.json()["detail"].lower()


def test_get_fiscal_ejercicio_fuera_rango_devuelve_400(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, _ = client_y_db
    r = client.get("/api/fiscal/2010")
    assert r.status_code == 400
    r2 = client.get("/api/fiscal/2099")
    assert r2.status_code == 400


def test_get_fiscal_cartera_vacia_devuelve_200_con_estructura(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, SessionLocal = client_y_db
    _seed_basico(SessionLocal)
    r = client.get("/api/fiscal/2025")
    assert r.status_code == 200
    data = r.json()
    assert data["ejercicio"] == 2025
    assert data["n_matches"] == 0
    assert data["matches"] == []
    assert data["positions"] == []
    # Compensación siempre presente, aunque vacía
    assert data["compensacion"]["ejercicio"] == 2025
    assert Decimal(data["compensacion"]["saldo_gp_final"]) == Decimal("0")


# ── Forma del JSON con datos ───────────────────────────────────────────────


def test_get_fiscal_match_basico_serializa_correctamente(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, SessionLocal = client_y_db
    cart_id = _seed_basico(SessionLocal)
    _add_tx(SessionLocal, cart_id, date(2025, 1, 15), "BUY", 10, 200)
    _add_tx(SessionLocal, cart_id, date(2025, 11, 20), "SELL", 10, 300)

    r = client.get("/api/fiscal/2025")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["n_matches"] == 1
    assert Decimal(data["gp_bruto"]) == Decimal("1000")

    m = data["matches"][0]
    # Campos obligatorios de FifoMatchOut presentes
    for campo in (
        "isin", "nombre", "fecha_compra", "fecha_venta", "cantidad",
        "coste_adquisicion", "importe_transmision", "ganancia_perdida",
        "ejercicio_fiscal", "regla_2_meses", "regla_2_meses_detalle",
        "es_scrip", "es_corto", "broker_compra", "broker_venta",
        "instrument_type", "lote_id", "perdida_diferida_aflorada_eur",
    ):
        assert campo in m, f"Falta campo '{campo}' en match serializado"

    assert m["regla_2_meses"] is False
    assert m["fecha_compra"] == "2025-01-15"
    assert m["fecha_venta"] == "2025-11-20"
    assert Decimal(m["ganancia_perdida"]) == Decimal("1000")


def test_get_fiscal_con_regla_2m_serializa_flag_y_detalle(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, SessionLocal = client_y_db
    cart_id = _seed_basico(SessionLocal)
    _add_tx(SessionLocal, cart_id, date(2025, 1, 15), "BUY", 100, 300)
    _add_tx(SessionLocal, cart_id, date(2025, 3, 15), "SELL", 100, 200)
    _add_tx(SessionLocal, cart_id, date(2025, 4, 1), "BUY", 100, 210)

    r = client.get("/api/fiscal/2025")
    data = r.json()

    m = data["matches"][0]
    assert m["regla_2_meses"] is True
    assert m["regla_2_meses_detalle"] != ""
    assert Decimal(data["gp_no_deducible_2m"]) == Decimal("10000")
    # Hay al menos una pérdida diferida latente
    assert len(data["perdidas_diferidas_latentes"]) >= 1


def test_get_fiscal_estructura_compensacion_completa(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    """Verifica que el bloque de compensación incluye todos los campos
    requeridos para la UI."""
    client, SessionLocal = client_y_db
    _seed_basico(SessionLocal)
    r = client.get("/api/fiscal/2025")
    data = r.json()
    comp = data["compensacion"]
    for campo in (
        "ejercicio", "gp_bruto", "gp_no_deducible_2m", "gp_deducible",
        "rcm_neto", "opciones_pl", "gp_total",
        "saldo_gp_tras_intra", "cruce_gp_a_rcm", "cruce_rcm_a_gp",
        "saldo_gp_tras_cruce", "saldo_rcm_tras_cruce",
        "perdidas_anteriores", "aplicadas_de_anteriores",
        "saldo_gp_final", "nuevo_saldo_negativo",
        "perdidas_actualizadas", "perdidas_expiradas",
        "perdidas_proximas_expirar",
        "base_ahorro_gp", "base_ahorro_rcm",
    ):
        assert campo in comp, f"Falta campo '{campo}' en compensación"


def test_get_fiscal_positions_serializa_posicion_abierta(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    """Compra sin venta → posición abierta con cantidad y PM correctos."""
    client, SessionLocal = client_y_db
    cart_id = _seed_basico(SessionLocal)
    _add_tx(SessionLocal, cart_id, date(2025, 1, 15), "BUY", 25, 320)

    r = client.get("/api/fiscal/2025")
    data = r.json()

    assert data["n_matches"] == 0
    assert len(data["positions"]) == 1
    p = data["positions"][0]
    assert Decimal(p["cantidad_total"]) == Decimal("25")
    assert Decimal(p["pm_ponderado_eur"]) == Decimal("320")
    assert p["num_lotes"] == 1
