"""Upsert de resultados de periodo IBKR (forex/tbills) y productos complejos.

Idempotente por (cartera_id, external_id): al reimportar un statement con
cifras actualizadas, se sobrescriben los valores en vez de duplicar filas.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import ComplejoCandidata, ResultadoCandidata
from app.db import models


@dataclass
class UpsertResultado:
    insertadas: int
    actualizadas: int


def upsert_resultados_ibkr(
    db: Session, cartera_id: str, candidatas: list[ResultadoCandidata]
) -> UpsertResultado:
    ins = upd = 0
    for c in candidatas:
        existente = db.execute(
            select(models.ResultadoIbkr)
            .where(models.ResultadoIbkr.cartera_id == cartera_id)
            .where(models.ResultadoIbkr.external_id == c.external_id)
        ).scalars().first()
        if existente is None:
            db.add(models.ResultadoIbkr(
                cartera_id=cartera_id, broker_id=c.broker_id,
                categoria=c.categoria, ejercicio=c.ejercicio, clave=c.clave,
                realized_eur=c.realized_eur, unrealized_eur=c.unrealized_eur,
                periodo_inicio=c.periodo_inicio, periodo_fin=c.periodo_fin,
                origen="extracto", external_id=c.external_id,
            ))
            ins += 1
        else:
            existente.realized_eur = c.realized_eur
            existente.unrealized_eur = c.unrealized_eur
            existente.ejercicio = c.ejercicio
            existente.periodo_inicio = c.periodo_inicio
            existente.periodo_fin = c.periodo_fin
            upd += 1
    db.commit()
    return UpsertResultado(insertadas=ins, actualizadas=upd)


def upsert_complejos(
    db: Session, cartera_id: str, candidatas: list[ComplejoCandidata]
) -> UpsertResultado:
    ins = upd = 0
    for c in candidatas:
        existente = db.execute(
            select(models.ProductoComplejo)
            .where(models.ProductoComplejo.cartera_id == cartera_id)
            .where(models.ProductoComplejo.external_id == c.external_id)
        ).scalars().first()
        if existente is None:
            db.add(models.ProductoComplejo(
                cartera_id=cartera_id, broker_id=c.broker_id,
                ejercicio=c.ejercicio, fecha=c.fecha, simbolo=c.simbolo,
                isin=c.isin, nombre=c.nombre, asset_category=c.asset_category,
                cantidad=c.cantidad, importe_eur=c.importe_eur,
                origen="extracto", external_id=c.external_id,
            ))
            ins += 1
        else:
            upd += 1
    db.commit()
    return UpsertResultado(insertadas=ins, actualizadas=upd)
