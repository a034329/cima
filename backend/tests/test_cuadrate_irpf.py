"""Tests del orquestador de generación XLSX IRPF (Roadmap 1.9 MVP).

Smoke tests: reconstrucción de operaciones desde la BD + generación del XLSX
estilo Cuádrate. No validan contenido fino del XLSX (cubierto por la suite
de Cuádrate), solo que el fichero se genera y abre.
"""
from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services import cuadrate_irpf as svc


@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    """TestClient con BD SQLite compartida en memoria (StaticPool)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

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


def _seed(SL: sessionmaker, isin: str = "US0378331005") -> tuple[str, str]:
    """Crea user + cartera + broker + posición con BUY/SELL. (cartera_id, isin)."""
    s: Session = SL()
    try:
        u = models.User(email="t@cima.local", modo="owner"); s.add(u); s.flush()
        c = models.Cartera(user_id=u.id, nombre="T"); s.add(c); s.flush()
        b = models.Broker(user_id=u.id, broker_tipo="ibkr", alias="IBKR"); s.add(b); s.flush()
        p = models.Posicion(cartera_id=c.id, isin=isin, nombre="Apple", divisa_local="EUR")
        s.add(p); s.flush()
        for fecha, tipo, qty, importe, gastos in (
            (date(2024, 3, 1), "BUY", "10", "1500", "1.5"),
            (date(2026, 4, 15), "SELL", "5", "1000", "1"),
        ):
            s.add(models.Transaccion(
                cartera_id=c.id, broker_id=b.id, posicion_id=p.id,
                fecha=fecha, tipo=tipo,
                cantidad=Decimal(qty),
                precio_local=Decimal(importe) / Decimal(qty),
                divisa_local="EUR", importe_local=Decimal(importe),
                fx_rate=Decimal("1"), importe_eur=Decimal(importe),
                gastos_eur=Decimal(gastos),
                estado="confirmada", origen="manual",
                external_id=f"{tipo[:3]}-{isin}-{fecha.isoformat()}",
            ))
        s.commit()
        return c.id, isin
    finally:
        s.close()


def _crear_broker(db: Session, cartera: models.Cartera, tipo: str = "ibkr") -> models.Broker:
    b = models.Broker(user_id=cartera.user_id, broker_tipo=tipo, alias=tipo.upper())
    db.add(b); db.flush()
    return b


def _crear_posicion(db: Session, cartera: models.Cartera, isin: str, nombre: str
                    ) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    return p


def _add_tx(db: Session, cartera: models.Cartera, broker: models.Broker,
            pos: models.Posicion, fecha: date, tipo: str, cantidad: str,
            importe: str, gastos: str = "0", notas: str | None = None) -> None:
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=pos.id,
        fecha=fecha, tipo=tipo,
        cantidad=Decimal(cantidad),
        precio_local=Decimal("0") if cantidad == "0" else (Decimal(importe) / Decimal(cantidad)),
        divisa_local="EUR",
        importe_local=Decimal(importe),
        fx_rate=Decimal("1"),
        importe_eur=Decimal(importe),
        gastos_eur=Decimal(gastos),
        estado="confirmada", origen="manual",
        external_id=f"{tipo[:3]}-{pos.isin}-{fecha.isoformat()}-{importe}",
        notas=notas,
    ))


def test_construir_operaciones_excluye_dividendos_e_intereses(
    db: Session, cartera: models.Cartera,
) -> None:
    """BUY/SELL/SP entran al FIFO; DIVIDEND/INTEREST/STAKING_REWARD NO."""
    broker = _crear_broker(db, cartera)
    pos = _crear_posicion(db, cartera, "ES0123456789", "Acme SA")

    _add_tx(db, cartera, broker, pos, date(2024, 1, 15), "BUY", "10", "1000", "1")
    _add_tx(db, cartera, broker, pos, date(2025, 3, 1), "DIVIDEND", "0", "50")
    _add_tx(db, cartera, broker, pos, date(2025, 6, 1), "INTEREST", "0", "5")
    _add_tx(db, cartera, broker, pos, date(2026, 4, 1), "SELL", "5", "750", "1")
    db.commit()

    ops = svc.construir_operaciones(db, cartera.id)
    tipos = [op["tipo"] for op in ops]
    assert tipos == ["A", "T"]                  # solo BUY + SELL → A + T
    assert ops[0]["broker"] == "IBKR"
    assert ops[0]["isin"] == "ES0123456789"
    assert ops[0]["instrument_type"] == "STOCK"
    assert ops[1]["cantidad"] == Decimal("5")


def test_construir_operaciones_split_serializa_qty_old_qty_new(
    db: Session, cartera: models.Cartera,
) -> None:
    """CORPORATE_SPLIT con metadatos JSON en notas → dict con qty_old/qty_new."""
    broker = _crear_broker(db, cartera, "degiro")
    pos = _crear_posicion(db, cartera, "US0231351067", "Amazon")
    _add_tx(db, cartera, broker, pos, date(2022, 6, 6), "CORPORATE_SPLIT",
            "0", "0", notas='{"split":{"qty_old":"1","qty_new":"20","nominal_old":"1"}}')
    db.commit()

    ops = svc.construir_operaciones(db, cartera.id)
    assert len(ops) == 1
    sp = ops[0]
    assert sp["tipo"] == "SP"
    assert sp["cantidad"] == Decimal("1")        # qty_old
    assert sp["importe_eur"] == Decimal("20")    # qty_new
    assert sp["gastos_eur"] == Decimal("1")      # nominal_old


def test_construir_operaciones_split_sin_notas_no_revienta(
    db: Session, cartera: models.Cartera,
) -> None:
    """Split sin meta (corner case): qty_old/qty_new = 0; el motor lo
    descartará o emitirá warning pero no debe levantar excepción aquí."""
    broker = _crear_broker(db, cartera, "degiro")
    pos = _crear_posicion(db, cartera, "US1234567890", "Corner")
    _add_tx(db, cartera, broker, pos, date(2023, 1, 1), "CORPORATE_SPLIT", "0", "0",
            notas=None)
    db.commit()

    ops = svc.construir_operaciones(db, cartera.id)
    assert len(ops) == 1
    assert ops[0]["cantidad"] == Decimal("0")
    assert ops[0]["importe_eur"] == Decimal("0")


def test_generar_xlsx_minimo_buy_sell(db: Session, cartera: models.Cartera) -> None:
    """End-to-end MVP: una compra previa + una venta del ejercicio
    generan un XLSX no vacío. No verificamos contenido fino — sólo que el
    fichero se materializa y tiene tamaño razonable."""
    broker = _crear_broker(db, cartera)
    pos = _crear_posicion(db, cartera, "US0378331005", "Apple")
    _add_tx(db, cartera, broker, pos, date(2024, 3, 1), "BUY", "10", "1500", "1.5")
    _add_tx(db, cartera, broker, pos, date(2026, 4, 15), "SELL", "5", "1000", "1")
    db.commit()

    out_path = svc.generar_xlsx(db, cartera.id, 2026)
    assert out_path.exists()
    assert out_path.stat().st_size > 5_000      # XLSX de Cuádrate ≈ 15-30 KB mínimo
    assert out_path.name == "cartera_valores_irpf_2026.xlsx"

    # Limpieza
    import shutil
    shutil.rmtree(out_path.parent, ignore_errors=True)


def test_endpoint_genera_y_devuelve_xlsx(client_y_db) -> None:
    """El router devuelve 200 con Content-Type xlsx y cabecera ZIP/OOXML (PK)."""
    client, SL = client_y_db
    _seed(SL)
    r = client.get("/api/cuadrate/irpf/2026.xlsx")
    assert r.status_code == 200, r.text
    ctype = r.headers.get("content-type", "")
    assert "spreadsheetml" in ctype, ctype
    assert len(r.content) > 5_000
    assert r.content[:2] == b"PK"     # firma de fichero ZIP/OOXML


def test_endpoint_ejercicio_fuera_de_rango(client_y_db) -> None:
    """Ejercicios <2000 o futuro → 400 antes de tocar BD."""
    client, SL = client_y_db
    _seed(SL)
    r = client.get("/api/cuadrate/irpf/1999.xlsx")
    assert r.status_code == 400
    r = client.get("/api/cuadrate/irpf/2999.xlsx")
    assert r.status_code == 400


def test_endpoint_sin_cartera_devuelve_404(client_y_db) -> None:
    """Sin cartera bootstrap → 404 (la BD existe pero está vacía)."""
    client, SL = client_y_db
    # No llamamos a _seed → cartera ausente
    r = client.get("/api/cuadrate/irpf/2026.xlsx")
    assert r.status_code == 404
