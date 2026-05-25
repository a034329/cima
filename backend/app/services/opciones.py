"""Servicio de opciones — persistencia + reconciliación.

Las opciones no encajan en `Transaccion` (orientada a acciones por ISIN):
tienen subyacente, strike, vencimiento y tipo C/P propios. Se almacenan en
la tabla `Opcion` y el cálculo fiscal (casilla 1626, DGT V2172-21) lo hace
`calcular_resumen_opciones` de Cuádrate agrupando por contrato.

Reconciliación: dedup por `(broker_id, external_id)` con external_id
sintético determinista, igual que el resto de parsers — reimportar el mismo
CSV no duplica.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class OpcionCandidata:
    """Una operación de opción normalizada lista para persistir/reconciliar."""
    fecha: date
    simbolo: str
    isin: str | None
    tipo_op: str           # 'C' | 'P' | '?'
    subyacente: str
    strike: str
    vencimiento: str
    accion: str            # 'compra' | 'venta'
    cantidad: Decimal
    prima_unitaria: Decimal
    importe_eur: Decimal
    gastos_eur: Decimal
    expirada: bool
    ejercida: bool
    external_id: str
    broker_id: str
    subyacente_isin: str | None = None   # solo ejercidas: ISIN del subyacente


@dataclass
class ImportOpcionesResultado:
    insertadas: int = 0
    deduplicadas: int = 0


def _existe(db: Session, broker_id: str, external_id: str) -> bool:
    return db.execute(
        select(models.Opcion)
        .where(models.Opcion.broker_id == broker_id)
        .where(models.Opcion.external_id == external_id)
    ).scalar_one_or_none() is not None


def reconciliar_opciones(
    db: Session,
    cartera_id: str,
    candidatas: Iterable[OpcionCandidata],
) -> ImportOpcionesResultado:
    """Inserta opciones nuevas, deduplica por (broker_id, external_id)."""
    res = ImportOpcionesResultado()
    for c in candidatas:
        if c.external_id and _existe(db, c.broker_id, c.external_id):
            res.deduplicadas += 1
            continue
        db.add(models.Opcion(
            cartera_id=cartera_id,
            broker_id=c.broker_id,
            fecha=c.fecha,
            simbolo=c.simbolo,
            isin=c.isin or None,
            subyacente_isin=c.subyacente_isin,
            tipo_op=c.tipo_op or "?",
            subyacente=c.subyacente or "",
            strike=c.strike or "",
            vencimiento=c.vencimiento or "",
            accion=c.accion,
            cantidad=c.cantidad,
            prima_unitaria=c.prima_unitaria,
            importe_eur=c.importe_eur,
            gastos_eur=c.gastos_eur,
            expirada=c.expirada,
            ejercida=c.ejercida,
            estado="confirmada",
            origen="extracto",
            external_id=c.external_id,
        ))
        db.flush()
        res.insertadas += 1
    db.commit()
    return res
