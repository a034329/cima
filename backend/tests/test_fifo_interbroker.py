"""Tests del FIFO cross-broker con rebuild on every import.

Caso crítico que motiva el `rebuild_for_posicion`: si importas DEGIRO antes
que IBKR, los lots IBKR cronológicamente anteriores deben intercalarse
correctamente sin necesidad de reimportar nada.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import (
    estado_posicion,
    rebuild_for_posicion,
)
from app.services.transacciones import (
    TxCandidata,
    reconciliar_extracto,
)


ISIN_MSFT = "US5949181045"


def _tx(
    *,
    fecha: date,
    tipo: str,
    cantidad: Decimal,
    precio: Decimal,
    broker_id: str,
    external_id: str,
) -> TxCandidata:
    importe = cantidad * precio
    return TxCandidata(
        fecha=fecha,
        tipo=tipo,
        isin=ISIN_MSFT,
        nombre="Microsoft Corp",
        cantidad=cantidad,
        precio_local=precio,
        divisa_local="EUR",
        importe_local=importe,
        fx_rate=Decimal("1"),
        importe_eur=importe,
        gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        retencion_pais=None,
        external_id=external_id,
        broker_id=broker_id,
    )


# ── Caso de oro: orden de importación INVERSO al orden cronológico ────────

def test_fifo_cross_broker_corrige_al_importar_segundo_broker(
    db: Session,
    cartera: models.Cartera,
    broker_tr: models.Broker,
    broker_degiro: models.Broker,
) -> None:
    """
    Cronología real:
      Enero  — IBKR compra 50 MSFT @ 200 €  (PM enero más bajo)
      Marzo  — DEGIRO compra 50 MSFT @ 300 €
      Junio  — DEGIRO vende 50 MSFT @ 400 €

    FIFO correcto: la venta consume los 50 lots IBKR de enero
    → plusvalía = 50 × (400 − 200) = 10.000 €.

    Si importamos DEGIRO PRIMERO (sin tener IBKR aún), el rebuild verá:
      - marzo: BUY 50 @ 300
      - junio: SELL 50 → consume los 50 de marzo, plusvalía = 5.000 €

    Después importamos IBKR (la compra enero). El rebuild debe DESHACER
    ese consumo erróneo y re-aplicar FIFO contra el lot enero correcto.
    """
    # ── Importar DEGIRO primero ───────────────────────────────────────
    dg_compra = _tx(
        fecha=date(2025, 3, 15),
        tipo="BUY",
        cantidad=Decimal("50"),
        precio=Decimal("300"),
        broker_id=broker_degiro.id,
        external_id="dg-buy-marzo",
    )
    dg_venta = _tx(
        fecha=date(2025, 6, 15),
        tipo="SELL",
        cantidad=Decimal("50"),
        precio=Decimal("400"),
        broker_id=broker_degiro.id,
        external_id="dg-sell-junio",
    )
    r1 = reconciliar_extracto(db, cartera.id, "degiro", [dg_compra, dg_venta])
    assert r1.insertadas == 2

    # Tras DEGIRO solo: el SELL ha consumido el BUY de marzo (cantidad 0).
    pos = db.execute(
        select(models.Posicion).where(models.Posicion.isin == ISIN_MSFT)
    ).scalar_one()
    est = estado_posicion(db, pos.id)
    assert est["cantidad"] == Decimal("0"), "Tras venta, inventario debe ser 0"

    # ── Importar IBKR (compra enero) → DEBE corregir FIFO ─────────────
    ibkr_compra = _tx(
        fecha=date(2025, 1, 10),
        tipo="BUY",
        cantidad=Decimal("50"),
        precio=Decimal("200"),
        broker_id=broker_tr.id,   # usamos broker_tr como segundo broker para el fixture
        external_id="tr-buy-enero",
    )
    r2 = reconciliar_extracto(db, cartera.id, "tr", [ibkr_compra])
    assert r2.insertadas == 1

    # Tras rebuild: la venta junio consume el lot enero (50 @ 200), no marzo.
    # → enero queda agotado, marzo (50 @ 300) queda intacto.
    lots = list(db.execute(
        select(models.Lot).where(models.Lot.posicion_id == pos.id)
    ).scalars())

    # Esperamos 2 lots: enero agotado y marzo intacto.
    assert len(lots) == 2, f"Esperábamos 2 lots, hay {len(lots)}: {[(l.fecha_compra, l.cantidad_restante) for l in lots]}"

    lot_enero = next(l for l in lots if l.fecha_compra == date(2025, 1, 10))
    lot_marzo = next(l for l in lots if l.fecha_compra == date(2025, 3, 15))

    assert lot_enero.cantidad_restante == Decimal("0"), (
        f"El lot de enero (más antiguo) debe estar consumido por la venta "
        f"de junio. Resta: {lot_enero.cantidad_restante}"
    )
    assert lot_marzo.cantidad_restante == Decimal("50"), (
        f"El lot de marzo (más reciente) debe seguir intacto. "
        f"Resta: {lot_marzo.cantidad_restante}"
    )

    # Y el inventario abierto agregado son los 50 de marzo.
    est_final = estado_posicion(db, pos.id)
    assert est_final["cantidad"] == Decimal("50")
    assert est_final["pm_real_eur"] == Decimal("300.0000")


def test_rebuild_idempotente(
    db: Session, cartera: models.Cartera, broker_tr: models.Broker
) -> None:
    """Llamar rebuild dos veces seguidas produce el mismo resultado."""
    compra = _tx(
        fecha=date(2025, 1, 10),
        tipo="BUY",
        cantidad=Decimal("100"),
        precio=Decimal("250"),
        broker_id=broker_tr.id,
        external_id="tr-1",
    )
    reconciliar_extracto(db, cartera.id, "tr", [compra])

    pos = db.execute(select(models.Posicion)).scalar_one()

    # Estado inicial
    est0 = estado_posicion(db, pos.id)
    n_lots_0 = est0["n_lots_abiertos"]

    # Rebuild dos veces
    r1 = rebuild_for_posicion(db, pos.id)
    r2 = rebuild_for_posicion(db, pos.id)

    est1 = estado_posicion(db, pos.id)
    est2 = estado_posicion(db, pos.id)

    assert est0 == est1 == est2
    assert r1.n_lots_creados == r2.n_lots_creados == 1
    assert r1.n_ventas_aplicadas == r2.n_ventas_aplicadas == 0
    assert est2["n_lots_abiertos"] == n_lots_0


def test_venta_sin_inventario_no_revienta_pero_avisa(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker
) -> None:
    """Si importas una venta sin haber importado aún el broker de la compra,
    el rebuild NO falla — registra aviso y continúa. Al importar después
    el broker que falta, el rebuild siguiente resuelve la venta."""
    venta_huerfana = _tx(
        fecha=date(2025, 6, 15),
        tipo="SELL",
        cantidad=Decimal("50"),
        precio=Decimal("400"),
        broker_id=broker_degiro.id,
        external_id="dg-sell-huerfana",
    )
    r = reconciliar_extracto(db, cartera.id, "degiro", [venta_huerfana])

    # La transacción se inserta confirmada (la fila del extracto es fiscal verdad)
    assert r.insertadas == 1
    # Pero el rebuild detecta que falta inventario y emite aviso
    assert any("[FIFO]" in a and "sin inventario" in a for a in r.avisos), (
        f"Esperábamos aviso de inventario insuficiente. Avisos: {r.avisos}"
    )

    # Ahora importamos la compra previa que faltaba
    compra_tardia = _tx(
        fecha=date(2025, 1, 10),
        tipo="BUY",
        cantidad=Decimal("50"),
        precio=Decimal("200"),
        broker_id=broker_degiro.id,
        external_id="dg-buy-tardia",
    )
    r2 = reconciliar_extracto(db, cartera.id, "degiro", [compra_tardia])
    assert r2.insertadas == 1

    # Tras rebuild: la venta ahora SÍ encuentra inventario y lo consume.
    pos = db.execute(select(models.Posicion)).scalar_one()
    est = estado_posicion(db, pos.id)
    assert est["cantidad"] == Decimal("0"), "Inventario debe quedar a 0 tras venta"

    # Y el aviso de inventario insuficiente ya no aparece.
    assert not any("sin inventario" in a for a in r2.avisos)
