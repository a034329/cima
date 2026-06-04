"""FIFO sobre `Lot` — cross-broker por posición (ISIN).

Reglas:
  - BUY confirmada → crea un Lot con cantidad_inicial == cantidad_restante.
  - SELL confirmada → consume lots de la misma posición por orden de
    `fecha_compra` (FIFO), decrementando `cantidad_restante`. Si una venta
    intenta consumir más de lo disponible, lanza error.
  - CORPORATE_SPLIT (incluye contra-splits, factor < 1) → aplica un factor
    `qty_new/qty_old` a todos los lotes existentes de la posición a la
    fecha del evento, multiplicando `cantidad_restante` y `cantidad_inicial`
    y dividiendo `coste_unit_eur` para que `coste_total_eur` se preserve.

**Cross-broker** por construcción: `Posicion` es única por (cartera, ISIN),
así que todos los lots de cualquier broker conviven en la misma cola FIFO
(Norma 9ª PGC + Art. 37.1.a LIRPF — el patrimonio es del contribuyente,
no del broker).

**Rebuild on every import**: la función pública `rebuild_for_posicion`
borra todos los lots de una posición y los reconstruye desde sus
transacciones confirmadas ordenadas por fecha. Esto garantiza que el
orden temporal correcto vence al orden de importación: si subes DEGIRO
primero y luego IBKR, los lots IBKR antiguos se intercalan correctamente
en la cola sin necesidad de reimportar nada.

NO se aplica regla 2 meses (eso es del motor fiscal de Cuádrate al generar
informes IRPF — fuera del scope de este servicio). Tampoco hay tratamiento
de opciones ejercidas, scrip dividends, etc. en esta versión.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import Session as _SASession

from app.db import models


class FIFOInsuficiente(RuntimeError):
    """Se intenta vender más cantidad de la que hay en el inventario."""


def aplicar_buy(db: Session, tx: models.Transaccion) -> models.Lot:
    """Crea el Lot correspondiente a una compra confirmada.

    Idempotente: si ya existe Lot para esta transacción, lo devuelve.
    """
    existente = db.execute(
        select(models.Lot).where(
            models.Lot.transaccion_origen_id == tx.id
        )
    ).scalar_one_or_none()
    if existente is not None:
        return existente

    coste_total = tx.importe_eur + tx.gastos_eur + tx.tasas_externas_eur
    coste_unit = coste_total / tx.cantidad if tx.cantidad > 0 else Decimal("0")

    lot = models.Lot(
        posicion_id=tx.posicion_id,
        transaccion_origen_id=tx.id,
        fecha_compra=tx.fecha,
        cantidad_inicial=tx.cantidad,
        cantidad_restante=tx.cantidad,
        coste_unit_eur=coste_unit.quantize(Decimal("0.0000000001")),
        coste_total_eur=coste_total.quantize(Decimal("0.0001")),
        gastos_eur=tx.gastos_eur + tx.tasas_externas_eur,
        broker_id=tx.broker_id,
    )
    db.add(lot)
    db.flush()
    return lot


def aplicar_sell(db: Session, tx: models.Transaccion) -> list[tuple[models.Lot, Decimal]]:
    """Consume lots FIFO para una venta confirmada.

    Devuelve la lista de (lot, cantidad_consumida) para auditar.
    """
    if tx.cantidad <= 0:
        return []

    lots = list(db.execute(
        select(models.Lot)
        .where(models.Lot.posicion_id == tx.posicion_id)
        .where(models.Lot.cantidad_restante > 0)
        .order_by(models.Lot.fecha_compra, models.Lot.id)
    ).scalars())

    # Verificación PREVIA atómica: si no hay inventario suficiente, fallar SIN
    # mutar los lots. Antes el bucle consumía parcialmente y solo entonces
    # lanzaba — al capturarse la excepción aguas arriba, los lots quedaban
    # vacíos sin reflejar nada (estado inconsistente, no detectable).
    inventario_total = sum((lot.cantidad_restante for lot in lots), Decimal("0"))
    if inventario_total < tx.cantidad:
        raise FIFOInsuficiente(
            f"Venta de {tx.cantidad} para posicion {tx.posicion_id} excede el "
            f"inventario disponible ({inventario_total}). Posible operación "
            f"huérfana o desfase con extractos previos."
        )

    cantidad_pendiente = tx.cantidad
    consumidos: list[tuple[models.Lot, Decimal]] = []
    for lot in lots:
        if cantidad_pendiente <= 0:
            break
        consume = min(lot.cantidad_restante, cantidad_pendiente)
        lot.cantidad_restante = (lot.cantidad_restante - consume).quantize(
            Decimal("0.0000000001")
        )
        consumidos.append((lot, consume))
        cantidad_pendiente -= consume

    db.flush()
    return consumidos


def aplicar_split(db: Session, tx: models.Transaccion) -> Decimal | None:
    """Aplica un evento CORPORATE_SPLIT a los lotes existentes de la posición.

    Multiplica `cantidad_inicial` y `cantidad_restante` por `factor =
    qty_new / qty_old`, y divide `coste_unit_eur` por el mismo factor,
    preservando `coste_total_eur`. Funciona también para contra-splits
    (factor < 1).

    Sólo afecta a lotes con `fecha_compra <= tx.fecha`. Si la transacción
    está mal-formada (sin meta JSON parseable) devuelve None sin tocar nada.
    """
    if not tx.notas:
        return None
    try:
        meta = json.loads(tx.notas)
        sp = meta["split"]
        qty_old = Decimal(str(sp["qty_old"]))
        qty_new = Decimal(str(sp["qty_new"]))
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    if qty_old <= 0:
        return None
    factor = qty_new / qty_old

    lots = list(db.execute(
        select(models.Lot)
        .where(models.Lot.posicion_id == tx.posicion_id)
        .where(models.Lot.fecha_compra <= tx.fecha)
    ).scalars())
    for lot in lots:
        lot.cantidad_inicial = (lot.cantidad_inicial * factor).quantize(
            Decimal("0.0000000001")
        )
        lot.cantidad_restante = (lot.cantidad_restante * factor).quantize(
            Decimal("0.0000000001")
        )
        if factor != 0:
            lot.coste_unit_eur = (lot.coste_unit_eur / factor).quantize(
                Decimal("0.0000000001")
            )
        # coste_total_eur se preserva (cantidad_restante × coste_unit_eur
        # mantiene el valor original tras split).
    db.flush()
    return factor


def aplicar_fifo(db: Session, tx: models.Transaccion) -> None:
    """Aplica el efecto FIFO de una transacción confirmada (modo incremental).

    BUY → aplicar_buy.
    SELL → aplicar_sell.
    CORPORATE_SPLIT → aplicar_split.
    Resto de tipos (DIVIDEND, INTEREST, STAKING, CORPORATE_*) → no-op por ahora.

    PREFIERE usar `rebuild_for_posicion` cuando proceses lotes de
    transacciones (importación de extractos): el modo incremental asume
    que las tx llegan en orden temporal, lo cual NO se cumple cuando
    importas brokers en orden distinto a su cronología.
    """
    if tx.estado != "confirmada":
        return
    if tx.tipo == "BUY":
        aplicar_buy(db, tx)
    elif tx.tipo == "SELL":
        aplicar_sell(db, tx)
    elif tx.tipo == "CORPORATE_SPLIT":
        aplicar_split(db, tx)
    # Otros tipos no afectan inventario en esta versión.


# ── Rebuild (cross-broker FIFO correcto independientemente del orden) ──

@dataclass
class RebuildResultado:
    """Resumen del rebuild de una posición."""
    posicion_id: str
    n_transacciones: int = 0
    n_lots_creados: int = 0
    n_ventas_aplicadas: int = 0
    avisos: list[str] = field(default_factory=list)
    """Avisos no-fatales (ej. SELL sin suficiente inventario porque falta
    importar un broker). El rebuild continúa con las tx siguientes."""


def rebuild_for_posicion(db: Session, posicion_id: str) -> RebuildResultado:
    """Borra todos los lots de la posición y los reconstruye desde cero
    procesando sus transacciones confirmadas ordenadas por (fecha, created_at).

    Idempotente: el estado final depende únicamente del conjunto de
    transacciones confirmadas. Llamar dos veces seguidas produce el mismo
    resultado.

    Si una venta no encuentra inventario suficiente (porque falta importar
    otro broker), se registra un aviso pero el rebuild NO falla — la venta
    queda sin lots consumidos y se podrá reprocesar al importar el broker
    que falta llamando otra vez a `rebuild_for_posicion`.
    """
    resultado = RebuildResultado(posicion_id=posicion_id)

    # 1. Borrar lots existentes
    db.execute(
        delete(models.Lot).where(models.Lot.posicion_id == posicion_id)
    )
    db.flush()

    # 2. Leer todas las tx confirmadas ordenadas cronológicamente.
    #    `created_at` desempata cuando dos tx caen en el mismo día.
    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.posicion_id == posicion_id)
        .where(models.Transaccion.estado == "confirmada")
        .order_by(models.Transaccion.fecha, models.Transaccion.created_at)
    ).scalars())
    resultado.n_transacciones = len(txs)

    # 3. Re-aplicar FIFO en orden cronológico
    for tx in txs:
        if tx.tipo == "BUY":
            aplicar_buy(db, tx)
            resultado.n_lots_creados += 1
        elif tx.tipo == "SELL":
            try:
                aplicar_sell(db, tx)
                resultado.n_ventas_aplicadas += 1
            except FIFOInsuficiente as e:
                resultado.avisos.append(
                    f"Venta {tx.fecha} cantidad={tx.cantidad} sin inventario "
                    f"suficiente. Falta importar broker anterior? Detalle: {e}"
                )
        elif tx.tipo == "CORPORATE_SPLIT":
            aplicar_split(db, tx)
        # DIVIDEND/INTEREST/STAKING/otros CORPORATE_*: no afectan inventario.

    db.flush()
    return resultado


def rebuild_for_posiciones(
    db: Session, posicion_ids: set[str] | list[str]
) -> list[RebuildResultado]:
    """Conveniencia: rebuild varias posiciones (ej. tras importar un CSV
    que toca múltiples ISINs)."""
    return [rebuild_for_posicion(db, pid) for pid in posicion_ids]


_ESTADO_CACHE = "_estado_posicion_cache"


@event.listens_for(_SASession, "after_flush")
def _invalidar_estado_cache(session: Session, flush_context: object) -> None:  # noqa: ARG001
    session.info.pop(_ESTADO_CACHE, None)


@event.listens_for(_SASession, "after_rollback")
def _invalidar_estado_cache_rb(session: Session) -> None:
    session.info.pop(_ESTADO_CACHE, None)


def estado_posicion(db: Session, posicion_id: str) -> dict[str, Decimal]:
    """Cantidad y coste agregado actual de una posición desde sus lots.

    Memoizado POR SESIÓN (`db.info`): en una petición se llama decenas de veces por
    posición (dashboard, fiscal, estimaciones…) y el resultado no cambia mientras no
    haya escrituras. La caché se invalida en cualquier flush/rollback → siempre fresco
    tras un cambio. Pasó de ~1.500 queries por dashboard a ~1 por posición."""
    cache = db.info.setdefault(_ESTADO_CACHE, {})
    if posicion_id in cache:
        return cache[posicion_id]
    lots = list(db.execute(
        select(models.Lot)
        .where(models.Lot.posicion_id == posicion_id)
        .where(models.Lot.cantidad_restante > 0)
    ).scalars())
    cantidad = sum((l.cantidad_restante for l in lots), Decimal("0"))
    coste = sum(
        (l.cantidad_restante * l.coste_unit_eur for l in lots),
        Decimal("0"),
    )
    pm = (coste / cantidad).quantize(Decimal("0.0001")) if cantidad > 0 else Decimal("0")
    resultado = {
        "cantidad": cantidad,
        "coste_total_eur": coste.quantize(Decimal("0.01")),
        "pm_real_eur": pm,
        "n_lots_abiertos": Decimal(len(lots)),
    }
    cache[posicion_id] = resultado
    return resultado
