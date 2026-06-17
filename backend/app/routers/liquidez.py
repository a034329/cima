"""Endpoint de liquidez (cash flows) + validación contra saldo reportado."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services.liquidez import calcular_liquidez


router = APIRouter(prefix="/liquidez", tags=["liquidez"])


class LiquidezBrokerOut(BaseModel):
    alias: str
    calculada: Decimal = Field(decimal_places=2)
    reportada: Decimal | None = None
    diferencia: Decimal | None = None


class LiquidezOut(BaseModel):
    total_calculada: Decimal = Field(decimal_places=2)
    total_reportada: Decimal | None = None
    total_disponible: Decimal = Field(decimal_places=2)
    por_broker: list[LiquidezBrokerOut]


@router.get("", response_model=LiquidezOut,
            summary="Liquidez calculada de cash flows + validación vs saldo broker")
def get_liquidez(db: Session = Depends(get_db),
                 cartera: models.Cartera = Depends(get_current_cartera)) -> LiquidezOut:
    r = calcular_liquidez(db, cartera.id)
    return LiquidezOut(
        total_calculada=r.total_calculada,
        total_reportada=r.total_reportada,
        total_disponible=r.total_disponible,
        por_broker=[
            LiquidezBrokerOut(
                alias=b.alias, calculada=b.calculada,
                reportada=b.reportada, diferencia=b.diferencia,
            )
            for b in r.por_broker
        ],
    )
