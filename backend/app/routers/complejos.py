"""Endpoint de Productos complejos — detección, sin cálculo fiscal."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services.fiscal_complejos import calcular_complejos


router = APIRouter(prefix="/complejos", tags=["complejos"])

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


class ComplejoLineaOut(BaseModel):
    fecha: date | None
    simbolo: str
    isin: str | None
    nombre: str
    asset_category: str
    cantidad: Decimal = Field(decimal_places=4)
    importe_eur: Decimal = Field(decimal_places=2)
    broker: str


class ComplejosResumen(BaseModel):
    ejercicio: int
    fecha_calculo: date
    n: int
    lineas: list[ComplejoLineaOut]


def _q(x: Decimal, places: str) -> Decimal:
    return Decimal(str(x)).quantize(Decimal(places), ROUND_HALF_UP)


def _serializar(r) -> ComplejosResumen:  # type: ignore[no-untyped-def]
    return ComplejosResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        n=r.n,
        lineas=[
            ComplejoLineaOut(
                fecha=l.fecha, simbolo=l.simbolo, isin=l.isin, nombre=l.nombre,
                asset_category=l.asset_category, cantidad=_q(l.cantidad, "0.0001"),
                importe_eur=_q(l.importe_eur, "0.01"), broker=l.broker,
            )
            for l in r.lineas
        ],
    )


def _cartera(db: Session) -> models.Cartera:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return cartera


@router.get("/acumulado", response_model=ComplejosResumen,
            summary="Productos complejos detectados (todos los años)")
def get_complejos_acumulado(db: Session = Depends(get_db)) -> ComplejosResumen:
    return _serializar(calcular_complejos(db, _cartera(db).id, None))


@router.get("/{ejercicio}", response_model=ComplejosResumen,
            summary="Productos complejos detectados en el ejercicio")
def get_complejos(ejercicio: int, db: Session = Depends(get_db)) -> ComplejosResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar(calcular_complejos(db, _cartera(db).id, ejercicio))
