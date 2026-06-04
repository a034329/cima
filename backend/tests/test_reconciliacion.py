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

def test_match_parcial_genera_conflicto(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Manual 24,71 €, extracto 30,00 € — el `importe_eur` también difiere 21,4%
    (cant×precio), supera el TOL_IMPORTE_PCT → conflicto. Si fuera solo el precio
    el que difiere (caso GBX↔GBP) y el importe_eur cuadrara, sería match exacto."""
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
    # Acepta el aviso por importe_eur (firma fiscal real) o por precio_local
    assert any(s in r.avisos[0].lower() for s in ("importe", "precio"))

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


# ── 7. Manual CONFIRMADA (confirmar_directo=True) también reconcilia ──────

def test_manual_confirmada_reconcilia_con_extracto_no_duplica(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker,
) -> None:
    """Caso real (Angel, venta de Zegona): el usuario registra la operación a
    mano con `confirmar_directo=True` (la operativa flexible: alta directa +
    rebuild FIFO al instante). Días después llega el extracto del mismo broker
    con la misma operación. Antes: la manual quedaba como "huérfana funcional"
    y el extracto insertaba una segunda fila → duplicado. Ahora: la manual SÍ
    es candidata aunque esté `confirmada`, se reconcilia in-place y el aviso
    `[RECONCILIADA]` la marca."""
    # Necesitamos inventario previo para que el SELL no falle por FIFO insuficiente
    # (validación añadida en crear_manual tras el bug del ACS).
    compra_previa = _tx_candidata(
        fecha=date(2024, 12, 1), tipo="BUY", broker_id=broker_tr.id,
        external_id="extracto-zegona-buy",
    )
    reconciliar_extracto(db, cartera.id, "tr", [compra_previa])
    manual = _tx_candidata(
        fecha=date(2025, 1, 27), tipo="SELL", broker_id=broker_tr.id, external_id=None,
    )
    tx_manual = crear_manual(db, cartera.id, manual, confirmar_directo=True)
    manual_id = tx_manual.id
    assert tx_manual.estado == "confirmada"
    assert tx_manual.origen == "manual"
    assert tx_manual.external_id is None

    fila = _tx_candidata(
        fecha=date(2025, 1, 27), tipo="SELL", broker_id=broker_tr.id,
        external_id="extracto-zegona-real",
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [fila])
    assert r.reconciliadas == 1 and r.insertadas == 0
    assert any("[RECONCILIADA]" in a for a in r.avisos)

    # Hay 2 tx: la compra previa del extracto + la SELL que pasó de manual a
    # extracto_tr in-place (mismo `id`, los datos del extracto la sobrescriben).
    sells = db.execute(select(models.Transaccion).where(
        models.Transaccion.tipo == "SELL")).scalars().all()
    assert len(sells) == 1
    assert sells[0].id == manual_id
    assert sells[0].estado == "confirmada"
    assert sells[0].origen == "extracto_tr"
    assert sells[0].external_id == "extracto-zegona-real"


def test_precio_local_distinto_pero_importe_eur_casa_es_match_exacto(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker,
) -> None:
    """Caso real Zegona: la manual quedó con precio_local=1834 (GBX raro,
    interpretación del usuario) y el extracto trae 21,17 (GBP/EUR del broker).
    El precio difiere 98% pero ambos generan ~2.470 € — son la misma operación
    fiscalmente. Debe ser match exacto, no conflicto."""
    fecha = date(2026, 5, 28)
    # Inventario previo (no se puede vender 117 sin tenerlas).
    compra = _tx_candidata(
        fecha=date(2026, 4, 1), tipo="BUY", broker_id=broker_tr.id,
        external_id="zegona-buy-prev", cantidad=Decimal("117"),
    )
    reconciliar_extracto(db, cartera.id, "tr", [compra])
    # Manual: precio_local "raro" (ej. en peniques) pero importe_eur correcto
    manual = _tx_candidata(
        fecha=fecha, tipo="SELL", broker_id=broker_tr.id, external_id=None,
        cantidad=Decimal("117"), precio=Decimal("1834.00"),
    )
    # Sobreescribimos importe_eur para que represente la verdad fiscal
    manual.importe_eur = Decimal("2470.00")
    manual.importe_local = Decimal("214578.00")
    tx_m = crear_manual(db, cartera.id, manual, confirmar_directo=True)
    manual_id = tx_m.id

    extracto = _tx_candidata(
        fecha=fecha, tipo="SELL", broker_id=broker_tr.id,
        external_id="dg-extracto-zegona",
        cantidad=Decimal("117"), precio=Decimal("21.17"),
    )
    extracto.importe_eur = Decimal("2476.96")    # 0,28% de diferencia → tolerado
    r = reconciliar_extracto(db, cartera.id, "tr", [extracto])

    assert r.reconciliadas == 1 and r.insertadas == 0 and r.conflictos == 0
    sells = db.execute(select(models.Transaccion).where(
        models.Transaccion.tipo == "SELL")).scalars().all()
    assert len(sells) == 1
    assert sells[0].id == manual_id    # se reemplazó in-place
    assert sells[0].precio_local == Decimal("21.17")   # el extracto manda
    assert sells[0].importe_eur == Decimal("2476.96")
    assert sells[0].external_id == "dg-extracto-zegona"


def test_huerfana_incluye_manuales_confirmadas_viejas_no_casadas(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker,
) -> None:
    """Una manual `confirmada` registrada hace >30 días sin contrapartida del
    broker también debe figurar como huérfana — el usuario tiene que saber
    qué piezas siguen sin reconciliarse para que el plan fiscal sea íntegro."""
    fecha_vieja = date.today() - timedelta(days=40)
    manual = _tx_candidata(
        fecha=fecha_vieja, broker_id=broker_tr.id, external_id=None,
    )
    crear_manual(db, cartera.id, manual, confirmar_directo=True)

    otra = _tx_candidata(
        fecha=date.today(), broker_id=broker_tr.id,
        isin="IE00B14X4S71", external_id="ext-otro",
    )
    r = reconciliar_extracto(db, cartera.id, "tr", [otra])
    assert r.huerfanas_manuales >= 1
    assert any("huérfana" in a.lower() and "confirmada" in a.lower()
               for a in r.avisos)
