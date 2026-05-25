"""Productos complejos detectados — sólo listado, sin cálculo fiscal.

Lee la tabla `productos_complejos`: instrumentos presentes en el extracto que
el motor NO soporta (CFD, futuro, warrant, estructurado, fondo, cripto IBKR).
Se muestran con un aviso honesto 'detectado, no calculado'. El usuario debe
declararlos aparte hasta que haya soporte específico.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class ComplejoLinea:
    fecha: date | None
    simbolo: str
    isin: str | None
    nombre: str
    asset_category: str
    cantidad: Decimal
    importe_eur: Decimal
    broker: str


@dataclass
class ComplejosResultado:
    ejercicio: int                   # 0 = acumulado
    lineas: list[ComplejoLinea]
    n: int
    fecha_calculo: date


def _broker_alias(db: Session, broker_id: str | None) -> str:
    if broker_id is None:
        return "manual"
    b = db.get(models.Broker, broker_id)
    return (b.alias or b.broker_tipo).upper() if b else "manual"


def calcular_complejos(
    db: Session, cartera_id: str, ejercicio: int | None
) -> ComplejosResultado:
    q = (
        select(models.ProductoComplejo)
        .where(models.ProductoComplejo.cartera_id == cartera_id)
        .order_by(models.ProductoComplejo.fecha)
    )
    if ejercicio is not None:
        q = q.where(models.ProductoComplejo.ejercicio == ejercicio)
    rows = list(db.execute(q).scalars())

    alias_cache: dict[str | None, str] = {}
    lineas: list[ComplejoLinea] = []
    for r in rows:
        if r.broker_id not in alias_cache:
            alias_cache[r.broker_id] = _broker_alias(db, r.broker_id)
        lineas.append(ComplejoLinea(
            fecha=r.fecha,
            simbolo=r.simbolo,
            isin=r.isin,
            nombre=r.nombre,
            asset_category=r.asset_category,
            cantidad=Decimal(str(r.cantidad)),
            importe_eur=Decimal(str(r.importe_eur)),
            broker=alias_cache[r.broker_id],
        ))

    return ComplejosResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        lineas=lineas,
        n=len(lineas),
        fecha_calculo=date.today(),
    )
