"""Pérdidas pendientes de años anteriores — entrada manual.

La auto-detección desde matches FIFO no puede saber qué se compensó en
declaraciones previas. Estas entradas manuales, cuando existen, son la fuente
de verdad para `perdidas_previas` de la compensación (sustituyen al
auto-detect). Caducan a los 4 años (ejercicio_origen + 4).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import get_compensacion_perdidas
from app.db import models


@dataclass
class PerdidaManual:
    ejercicio_origen: int
    importe_eur: Decimal
    expira: int


def listar(db: Session, cartera_id: str) -> list[PerdidaManual]:
    filas = db.execute(
        select(models.PerdidaPendienteManual)
        .where(models.PerdidaPendienteManual.cartera_id == cartera_id)
        .order_by(models.PerdidaPendienteManual.ejercicio_origen)
    ).scalars()
    return [
        PerdidaManual(p.ejercicio_origen, Decimal(str(p.importe_eur)), p.ejercicio_origen + 4)
        for p in filas
    ]


def set_perdida(
    db: Session, cartera_id: str, ejercicio_origen: int, importe_eur: Decimal | None
) -> None:
    """Upsert. importe None o <= 0 → elimina la entrada de ese año."""
    fila = db.execute(
        select(models.PerdidaPendienteManual)
        .where(models.PerdidaPendienteManual.cartera_id == cartera_id)
        .where(models.PerdidaPendienteManual.ejercicio_origen == ejercicio_origen)
    ).scalars().first()
    if importe_eur is None or importe_eur <= 0:
        if fila is not None:
            db.delete(fila)
            db.commit()
        return
    if not (2000 <= ejercicio_origen <= 2100):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ejercicio_origen fuera de rango")
    if fila is None:
        db.add(models.PerdidaPendienteManual(
            cartera_id=cartera_id, ejercicio_origen=ejercicio_origen,
            importe_eur=importe_eur,
        ))
    else:
        fila.importe_eur = importe_eur
    db.commit()


def perdidas_previas_motor(db: Session, cartera_id: str):
    """Convierte las entradas manuales a `cp.PerdidaPendiente` del motor.
    Devuelve [] si no hay entradas (entonces el llamador puede auto-detectar)."""
    manuales = listar(db, cartera_id)
    if not manuales:
        return []
    cp = get_compensacion_perdidas()
    return [
        cp.PerdidaPendiente(
            ejercicio_origen=m.ejercicio_origen,
            importe_original_eur=m.importe_eur,
            compensado_eur=Decimal("0"),
            pendiente_eur=m.importe_eur,
            expira=m.expira,
            detalle="manual",
        )
        for m in manuales
    ]
