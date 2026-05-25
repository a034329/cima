"""Tests del backbone de transacciones — el corazón del producto.

Cubre los 4 caminos del algoritmo:
  1. Idempotencia: importar dos veces el mismo CSV → misma cantidad de tx.
  2. Reconciliación: manual previa + extracto = 1 confirmada (no dos).
  3. Match parcial: precio fuera de tolerancia → conflicto registrado.
  4. Manual huérfana: aparece como aviso, no se elimina.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.transacciones import (
    TxCandidata,
    crear_manual,
    reconciliar_extracto,
)


def _tx_candidata(
    *,
    fecha: date,
    isin: str = "IE000U9J8HX9",
    tipo: str = "BUY",
    cantidad: Decimal = Decimal("100"),
    precio: Decimal = Decimal("24.71"),
    external_id: str | None = "tx-extracto-001",
    broker_id: str,
) -> TxCandidata:
    importe = cantidad * precio
    return TxCandidata(
        fecha=fecha,
        tipo=tipo,
        isin=isin,
        nombre="Test Asset",
        cantidad=cantidad,
        precio_local=precio,
        divisa_local="EUR",
        importe_local=importe,
        fx_rate=Decimal("1"),
        importe_eur=importe,
        gastos_eur=Decimal("1.00"),
        tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        retencion_pais=None,
        external_id=external_id,
        broker_id=broker_id,
    )


# ── 1. Idempotencia (dedup por external_id) ────────────────────────────────

def test_reimportar_mismo_extracto_no_duplica(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    candidata = _tx_candidata(fecha=date(2025, 1, 27), broker_id=broker_tr.id)

    r1 = reconciliar_extracto(db, cartera.id, "tr", [candidata])
    assert r1.insertadas == 1
    assert r1.deduplicadas == 0

    # Reimportar la MISMA fila — debe deduplicar
    r2 = reconciliar_extracto(db, cartera.id, "tr", [candidata])
    assert r2.insertadas == 0
    assert r2.deduplicadas == 1

    # Sólo hay 1 transacción en BD
    n = db.execute(select(models.Transaccion)).scalars().all()
    assert len(n) == 1


# ── 2. Reconciliación: manual previa + extracto exacto ─────────────────────

def test_manual_previa_se_promueve_a_confirmada_con_extracto(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    # Usuario registra una compra manualmente el día 27
    manual = _tx_candidata(
        fecha=date(2025, 1, 27), broker_id=broker_tr.id, external_id=None
    )
    tx_manual = crear_manual(db, cartera.id, manual)
    assert tx_manual.estado == "pendiente_confirmar"
    assert tx_manual.origen == "manual"

    # Días después llega el extracto con la misma operación (con external_id real)
    fila_extracto = _tx_candidata(
        fecha=date(2025, 1, 27),
        broker_id=broker_tr.id,
        external_id="real-tx-id-from-tr",
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [fila_extracto])

    assert r.reconciliadas == 1
    assert r.insertadas == 0
    assert r.deduplicadas == 0

    # Sólo hay 1 transacción, ahora confirmada con el external_id del broker
    todas = db.execute(select(models.Transaccion)).scalars().all()
    assert len(todas) == 1
    assert todas[0].estado == "confirmada"
    assert todas[0].external_id == "real-tx-id-from-tr"
    assert todas[0].origen == "extracto_tr"


def test_reconciliacion_tolerancia_fecha_2_dias(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Manual el 27, extracto el 29 — ±2 días entran en tolerancia."""
    manual = _tx_candidata(
        fecha=date(2025, 1, 27), broker_id=broker_tr.id, external_id=None
    )
    crear_manual(db, cartera.id, manual)

    extracto = _tx_candidata(
        fecha=date(2025, 1, 29), broker_id=broker_tr.id, external_id="ext-001"
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [extracto])
    assert r.reconciliadas == 1

    todas = db.execute(select(models.Transaccion)).scalars().all()
    assert len(todas) == 1
    assert todas[0].estado == "confirmada"


def test_reconciliacion_fuera_tolerancia_fecha_inserta_aparte(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Manual el 1, extracto el 10 — 9 días → fuera de tolerancia.
    Se insertan como dos transacciones distintas (la manual queda huérfana)."""
    manual = _tx_candidata(
        fecha=date(2025, 1, 1), broker_id=broker_tr.id, external_id=None
    )
    crear_manual(db, cartera.id, manual)

    extracto = _tx_candidata(
        fecha=date(2025, 1, 10), broker_id=broker_tr.id, external_id="ext-001"
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [extracto])

    assert r.insertadas == 1
    assert r.reconciliadas == 0

    todas = list(db.execute(select(models.Transaccion)).scalars())
    assert len(todas) == 2


# ── 3. Match parcial (precio fuera de tolerancia) → conflicto ──────────────

def test_match_parcial_precio_genera_conflicto(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Manual a 24,71 €, extracto a 30,00 € — precio difiere 21,4% → conflicto."""
    fecha_reciente = date.today() - timedelta(days=2)
    manual = _tx_candidata(
        fecha=fecha_reciente,
        broker_id=broker_tr.id,
        external_id=None,
        precio=Decimal("24.71"),
    )
    crear_manual(db, cartera.id, manual)

    extracto = _tx_candidata(
        fecha=fecha_reciente,
        broker_id=broker_tr.id,
        external_id="ext-001",
        precio=Decimal("30.00"),
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [extracto])

    assert r.conflictos == 1
    assert r.insertadas == 1   # se inserta la del extracto
    assert r.reconciliadas == 0
    assert len(r.avisos) >= 1
    assert "precio" in r.avisos[0].lower()

    # Hay dos transacciones: la manual pendiente y la del extracto confirmada
    todas = list(db.execute(select(models.Transaccion)).scalars())
    assert len(todas) == 2
    pendientes = [t for t in todas if t.estado == "pendiente_confirmar"]
    confirmadas = [t for t in todas if t.estado == "confirmada"]
    assert len(pendientes) == 1
    assert len(confirmadas) == 1


# ── 4. Cross-broker: misma posición, distintos brokers ─────────────────────

def test_cross_broker_misma_isin_no_se_confunden(
    db: Session,
    cartera: models.Cartera,
    broker_tr: models.Broker,
    broker_degiro: models.Broker,
) -> None:
    """Compras del mismo ISIN en TR y DEGIRO no se reconcilian entre sí."""
    ext_tr = _tx_candidata(
        fecha=date(2025, 1, 27),
        broker_id=broker_tr.id,
        external_id="tr-001",
    )
    ext_dg = _tx_candidata(
        fecha=date(2025, 1, 27),
        broker_id=broker_degiro.id,
        external_id="dg-001",
    )

    r1 = reconciliar_extracto(db, cartera.id, "tr", [ext_tr])
    r2 = reconciliar_extracto(db, cartera.id, "degiro", [ext_dg])

    assert r1.insertadas == 1
    assert r2.insertadas == 1

    todas = list(db.execute(select(models.Transaccion)).scalars())
    assert len(todas) == 2
    brokers = {t.broker_id for t in todas}
    assert brokers == {broker_tr.id, broker_degiro.id}

    # Y comparten la misma posición (FIFO global cross-broker por ISIN)
    posiciones = list(db.execute(select(models.Posicion)).scalars())
    assert len(posiciones) == 1


# ── 5. Crear manual via API service ────────────────────────────────────────

def test_crear_manual_queda_pendiente_confirmar(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    candidata = _tx_candidata(
        fecha=date(2025, 5, 16), broker_id=broker_tr.id, external_id=None
    )
    tx = crear_manual(db, cartera.id, candidata)
    assert tx.estado == "pendiente_confirmar"
    assert tx.origen == "manual"
    assert tx.external_id is None

    # Y crea la posición automáticamente
    pos = db.execute(
        select(models.Posicion).where(models.Posicion.isin == "IE000U9J8HX9")
    ).scalar_one()
    assert pos.cartera_id == cartera.id


# ── 6. Huérfana antigua → aviso ───────────────────────────────────────────

def test_manual_huerfana_antigua_genera_aviso(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Manual de hace 40 días sin confirmar → aviso al importar nuevo extracto."""
    fecha_vieja = date.today() - timedelta(days=40)
    manual = _tx_candidata(
        fecha=fecha_vieja, broker_id=broker_tr.id, external_id=None
    )
    crear_manual(db, cartera.id, manual)

    # Extracto trae OTRA operación distinta (otro ISIN), la huérfana sigue
    otra = _tx_candidata(
        fecha=date.today(),
        broker_id=broker_tr.id,
        isin="IE00B14X4S71",
        external_id="ext-new",
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [otra])

    assert r.huerfanas_manuales >= 1
    assert any("huérfana" in a.lower() for a in r.avisos)
