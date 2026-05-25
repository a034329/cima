"""Endpoint de Letras del Tesoro / T-Bills — RCM. Sólo IBKR."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services.fiscal_bills import calcular_bills


router = APIRouter(prefix="/bills", tags=["bills"])

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


class BillLineaOut(BaseModel):
    simbolo: str
    realized_eur: Decimal = Field(decimal_places=2)


class BillsResumen(BaseModel):
    ejercicio: int
    fecha_calculo: date
    realized_total: Decimal = Field(decimal_places=2)   # RCM
    periodo_inicio: date | None
    periodo_fin: date | None
    lineas: list[BillLineaOut]


def _q2(x: Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _serializar(r) -> BillsResumen:  # type: ignore[no-untyped-def]
    return BillsResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        realized_total=_q2(r.realized_total),
        periodo_inicio=r.periodo_inicio,
        periodo_fin=r.periodo_fin,
        lineas=[
            BillLineaOut(simbolo=l.simbolo, realized_eur=_q2(l.realized_eur))
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


@router.get("/acumulado", response_model=BillsResumen,
            summary="T-Bills acumulado (todos los años)")
def get_bills_acumulado(db: Session = Depends(get_db)) -> BillsResumen:
    return _serializar(calcular_bills(db, _cartera(db).id, None))


@router.get("/{ejercicio}", response_model=BillsResumen,
            summary="Letras del Tesoro del ejercicio (RCM)")
def get_bills(ejercicio: int, db: Session = Depends(get_db)) -> BillsResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar(calcular_bills(db, _cartera(db).id, ejercicio))
