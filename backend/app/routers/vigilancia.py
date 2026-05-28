"""Vigilancia de cartera: alertas de movimiento de precio."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import vigilancia as svc


router = APIRouter(prefix="/vigilancia", tags=["vigilancia"])


def _q2(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


class AlertaOut(BaseModel):
    isin: str
    nombre: str
    precio_anterior: Decimal = Field(decimal_places=2)
    precio_actual: Decimal = Field(decimal_places=2)
    cambio_pct: Decimal = Field(decimal_places=4)
    nivel: str


class VigilanciaOut(BaseModel):
    alertas: list[AlertaOut]
    desde: str | None = None


def _cartera(db: Session) -> models.Cartera:
    from fastapi import HTTPException
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay cartera. POST /api/bootstrap")
    return c


@router.get("", response_model=VigilanciaOut, summary="Alertas de movimiento de precio desde el último 'visto'")
def get_vigilancia(db: Session = Depends(get_db)) -> VigilanciaOut:
    alertas, desde = svc.evaluar(db, _cartera(db).id)
    return VigilanciaOut(
        alertas=[AlertaOut(isin=a.isin, nombre=a.nombre, precio_anterior=_q2(a.precio_anterior),
                           precio_actual=_q2(a.precio_actual),
                           cambio_pct=Decimal(str(a.cambio_pct)).quantize(Decimal("0.0001"), ROUND_HALF_UP),
                           nivel=a.nivel) for a in alertas],
        desde=desde,
    )


@router.post("/visto", status_code=status.HTTP_204_NO_CONTENT, summary="Marcar como visto (resetea el baseline)")
def visto(db: Session = Depends(get_db)) -> None:
    svc.marcar_visto(db, _cartera(db).id)
