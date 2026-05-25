"""Tests del fix anti-duplicación: external_id sintético para filas sin
UUID del broker, y dedup retroactiva.

Reproduce el escenario real reportado: re-importar el mismo CSV multi-año
de DEGIRO repetidamente inflaba la BD porque ~10% de las filas no traen
order_id (corto forzado §3.9, scrip, M&A).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.cuadrate import parse_degiro_csv
from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services.transacciones import reconciliar_extracto


DG_CSV = Path("/app/720/irpf/DeGiro_Transacciones_2025.csv")


pytestmark = pytest.mark.skipif(
    not DG_CSV.is_file(),
    reason=f"CSV DEGIRO no presente en {DG_CSV}",
)


# ── Adapter: todas las filas tienen external_id ────────────────────────────


def test_parser_garantiza_external_id_para_todas_las_filas() -> None:
    """Tras el fix, NINGUNA TxCandidata sale con external_id None — las
    filas sin order_id real obtienen un id sintético determinista."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    sin_ext = [c for c in cands if not c.external_id]
    assert sin_ext == [], (
        f"{len(sin_ext)} filas sin external_id — el fallback sintético no aplica"
    )


def test_synthetic_id_estable_entre_invocaciones() -> None:
    """Dos parseos del mismo CSV producen los mismos external_id, incluso
    para las filas sin UUID nativo. Requisito para que la dedup funcione
    en re-imports."""
    c1 = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    c2 = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    ids1 = sorted(c.external_id for c in c1)
    ids2 = sorted(c.external_id for c in c2)
    assert ids1 == ids2


def test_synthetic_id_no_colisiona_con_uuid_real() -> None:
    """Los ids sintéticos llevan prefijo 'dg-noid-' para no chocar con UUIDs
    de DEGIRO."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    sint = [c for c in cands if c.external_id.startswith("dg-noid-")]
    reales = [c for c in cands if not c.external_id.startswith("dg-noid-")]
    assert len(sint) > 0, "Esperábamos al menos 1 fila sin UUID en el CSV"
    assert len(reales) > 0
    # Ningún sintético colisiona con un UUID real
    set_sint = {c.external_id for c in sint}
    set_real = {c.external_id for c in reales}
    assert not (set_sint & set_real)


# ── Reconciliación: re-import no duplica ──────────────────────────────────


def test_reimportar_csv_dos_veces_no_inserta_nada_segunda_vuelta(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """End-to-end del fix: cargar el mismo CSV dos veces produce 0
    insertadas en la segunda pasada."""
    cands1 = parse_degiro_csv(DG_CSV, broker_id=broker_degiro.id)
    r1 = reconciliar_extracto(db, cartera.id, "degiro", cands1)
    n1 = r1.insertadas
    assert n1 > 0

    cands2 = parse_degiro_csv(DG_CSV, broker_id=broker_degiro.id)
    r2 = reconciliar_extracto(db, cartera.id, "degiro", cands2)
    assert r2.insertadas == 0, (
        f"Re-import duplicó {r2.insertadas} filas (era 13 antes del fix). "
        f"Dedup'd: {r2.deduplicadas}"
    )
    assert r2.deduplicadas == n1


def test_reimportar_tres_veces_idempotente(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """3 re-imports mantienen el mismo conteo de tx en BD."""
    for _ in range(3):
        cands = parse_degiro_csv(DG_CSV, broker_id=broker_degiro.id)
        reconciliar_extracto(db, cartera.id, "degiro", cands)

    total = db.execute(
        select(models.Transaccion).where(
            models.Transaccion.cartera_id == cartera.id
        )
    ).scalars().all()
    n_unicos_por_ext_id = len({t.external_id for t in total})
    assert len(total) == n_unicos_por_ext_id, (
        f"Hay {len(total)} tx pero solo {n_unicos_por_ext_id} external_ids únicos — "
        f"duplicación remanente"
    )


# ── Endpoint de dedup retroactiva ─────────────────────────────────────────


def test_endpoint_dedup_limpia_duplicados_existentes() -> None:
    """Simula el escenario heredado: un usuario re-importó el CSV 3 veces
    ANTES del fix → tiene tx duplicadas en BD. El endpoint de mantenimiento
    debe encontrarlas y marcarlas descartada."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            # Setup: user + cartera + broker + posicion + 3 tx idénticas
            s = SessionLocal()
            from decimal import Decimal
            from datetime import date
            user = models.User(email="d@test.local", modo="owner")
            s.add(user); s.flush()
            cart = models.Cartera(user_id=user.id, nombre="t"); s.add(cart); s.flush()
            broker = models.Broker(user_id=user.id, broker_tipo="degiro", alias="DG"); s.add(broker); s.flush()
            pos = models.Posicion(cartera_id=cart.id, isin="US5949181045", nombre="MSFT", divisa_local="USD")
            s.add(pos); s.flush()
            # 3 tx con misma firma natural (simula re-imports antes del fix)
            for i in range(3):
                s.add(models.Transaccion(
                    cartera_id=cart.id, broker_id=broker.id, posicion_id=pos.id,
                    fecha=date(2024, 5, 10), tipo="BUY",
                    cantidad=Decimal("100"), precio_local=Decimal("200"),
                    divisa_local="EUR", importe_local=Decimal("20000"),
                    fx_rate=Decimal("1"), importe_eur=Decimal("20000"),
                    gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
                    retencion_eur=Decimal("0"),
                    estado="confirmada", origen="manual",
                    external_id=None,
                ))
            s.commit()
            s.close()

            r = client.post("/api/maintenance/dedup-sin-external-id")
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["grupos_duplicados"] == 1
            assert data["descartadas"] == 2
            assert data["conservadas"] == 1

            # Idempotente: segunda llamada no hace nada
            r2 = client.post("/api/maintenance/dedup-sin-external-id")
            assert r2.status_code == 200
            assert r2.json()["descartadas"] == 0
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
