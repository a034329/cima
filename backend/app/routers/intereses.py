"""Endpoint de Intereses — RCM (casilla 0027) e informativo no deducible."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services.fiscal_intereses import calcular_intereses


router = APIRouter(prefix="/intereses", tags=["intereses"])

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


class InteresLineaOut(BaseModel):
    fecha: date
    tipo: str
    casilla: str | None
    descripcion: str
    divisa: str
    importe_eur: Decimal = Field(decimal_places=2)
    broker: str


class InteresesResumen(BaseModel):
    ejercicio: int
    fecha_calculo: date
    rcm_total: Decimal = Field(decimal_places=2)       # casilla 0027
    debit_total: Decimal = Field(decimal_places=2)     # informativo no deducible
    neto_total: Decimal = Field(decimal_places=2)
    n_lineas: int
    lineas: list[InteresLineaOut]


def _q2(x: Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _serializar(r) -> InteresesResumen:  # type: ignore[no-untyped-def]
    return InteresesResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        rcm_total=_q2(r.rcm_total),
        debit_total=_q2(r.debit_total),
        neto_total=_q2(r.neto_total),
        n_lineas=len(r.lineas),
        lineas=[
            InteresLineaOut(
                fecha=l.fecha, tipo=l.tipo, casilla=l.casilla,
                descripcion=l.descripcion, divisa=l.divisa,
                importe_eur=_q2(l.importe_eur), broker=l.broker,
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


@router.get("/acumulado", response_model=InteresesResumen,
            summary="Intereses acumulados (todos los años)")
def get_intereses_acumulado(db: Session = Depends(get_db)) -> InteresesResumen:
    return _serializar(calcular_intereses(db, _cartera(db).id, None))


@router.get("/{ejercicio}", response_model=InteresesResumen,
            summary="Intereses del ejercicio (RCM casilla 0027)")
def get_intereses(ejercicio: int, db: Session = Depends(get_db)) -> InteresesResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar(calcular_intereses(db, _cartera(db).id, ejercicio))
