"""Endpoints de aportaciones (capital del bolsillo del usuario)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services.aportaciones import aportaciones_por_anio


router = APIRouter(prefix="/aportaciones", tags=["aportaciones"])


class AportacionIn(BaseModel):
    fecha: date
    importe_eur: Decimal              # + aportación / − retirada
    descripcion: str | None = None
    broker_id: str | None = None


class AportacionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    fecha: date
    importe_eur: Decimal
    descripcion: str | None
    origen: str
    broker_id: str | None


class AnioAportacion(BaseModel):
    anio: int
    neto: Decimal = Field(decimal_places=2)


class AportacionesResumen(BaseModel):
    total_neto: Decimal = Field(decimal_places=2)
    por_anio: list[AnioAportacion]
    movimientos: list[AportacionOut]


@router.get("", response_model=AportacionesResumen,
            summary="Aportaciones netas por año + movimientos")
def listar(db: Session = Depends(get_db),
           cartera: models.Cartera = Depends(get_current_cartera)) -> AportacionesResumen:
    por_anio = aportaciones_por_anio(db, cartera.id)
    # Cota al listado (los totales se calculan aparte sobre TODO el
    # histórico): sin límite el payload crecía sin tope (auditoría).
    movimientos = list(db.execute(
        select(models.Aportacion)
        .where(models.Aportacion.cartera_id == cartera.id)
        .order_by(models.Aportacion.fecha.desc())
        .limit(500)
    ).scalars())
    return AportacionesResumen(
        total_neto=sum(por_anio.values(), Decimal("0")),
        por_anio=[AnioAportacion(anio=a, neto=n) for a, n in sorted(por_anio.items())],
        movimientos=movimientos,  # type: ignore[arg-type]
    )


@router.post("", response_model=AportacionOut, status_code=status.HTTP_201_CREATED,
             summary="Registrar aportación/retirada manual")
def crear(payload: AportacionIn, db: Session = Depends(get_db),
          cartera: models.Cartera = Depends(get_current_cartera)) -> models.Aportacion:
    if payload.importe_eur == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El importe no puede ser 0.",
        )
    ext_id = f"manual-{payload.fecha.isoformat()}-{int(payload.importe_eur * 100)}"
    # Dedup del doble submit (auditoría Cima 2026-06-11, J7): el external_id
    # ya era determinista pero nadie lo comprobaba — dos clics seguidos creaban
    # dos filas idénticas y corrompían el neto anual y la proyección IF.
    existente = db.execute(
        select(models.Aportacion)
        .where(models.Aportacion.cartera_id == cartera.id)
        .where(models.Aportacion.external_id == ext_id)
    ).scalars().first()
    if existente is not None:
        return existente
    ap = models.Aportacion(
        cartera_id=cartera.id, broker_id=payload.broker_id, fecha=payload.fecha,
        importe_eur=payload.importe_eur, descripcion=payload.descripcion,
        origen="manual",
        external_id=ext_id,
    )
    db.add(ap)
    db.commit()
    return ap


@router.delete("/{aportacion_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Eliminar una aportación")
def eliminar(aportacion_id: str, db: Session = Depends(get_db),
             cartera: models.Cartera = Depends(get_current_cartera)) -> None:
    ap = db.get(models.Aportacion, aportacion_id)
    if ap is not None and ap.cartera_id != cartera.id:
        ap = None   # S2: scoping a la cartera activa
    if ap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No existe")
    db.delete(ap)
    db.commit()
