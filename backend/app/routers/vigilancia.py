"""Vigilancia de cartera: alertas de movimiento de precio."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
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
    modo: str = "baseline"          # baseline | intradia


class AlertaPlanOut(BaseModel):
    isin: str
    nombre: str
    decision: str
    precio_alerta_eur: Decimal = Field(decimal_places=2)
    precio_actual_eur: Decimal = Field(decimal_places=2)
    paso_id: str
    razon: str | None = None


class VigilanciaOut(BaseModel):
    alertas: list[AlertaOut]                # baseline (compatibilidad)
    alertas_intradia: list[AlertaOut] = []  # vs cierre de ayer
    alertas_plan: list[AlertaPlanOut] = []  # pasos del plan habilitados por precio (V4)
    desde: str | None = None


def _alerta_out(a: svc.Alerta) -> AlertaOut:
    return AlertaOut(
        isin=a.isin, nombre=a.nombre,
        precio_anterior=_q2(a.precio_anterior), precio_actual=_q2(a.precio_actual),
        cambio_pct=Decimal(str(a.cambio_pct)).quantize(Decimal("0.0001"), ROUND_HALF_UP),
        nivel=a.nivel, modo=a.modo,
    )


@router.get("", response_model=VigilanciaOut,
            summary="Alertas de movimiento: vs último 'visto' (baseline) y vs cierre de ayer (intradia)")
def get_vigilancia(db: Session = Depends(get_db),
                   cartera: models.Cartera = Depends(get_current_cartera)) -> VigilanciaOut:
    cid = cartera.id
    alertas, desde = svc.evaluar(db, cid)
    intradia = svc.evaluar_intradia(db, cid)
    plan = svc.evaluar_plan_precio(db, cid)
    return VigilanciaOut(
        alertas=[_alerta_out(a) for a in alertas],
        alertas_intradia=[_alerta_out(a) for a in intradia],
        alertas_plan=[AlertaPlanOut(
            isin=a.isin, nombre=a.nombre, decision=a.decision,
            precio_alerta_eur=_q2(a.precio_alerta_eur),
            precio_actual_eur=_q2(a.precio_actual_eur),
            paso_id=a.paso_id, razon=a.razon,
        ) for a in plan],
        desde=desde,
    )


@router.post("/visto", status_code=status.HTTP_204_NO_CONTENT, summary="Marcar como visto (resetea el baseline)")
def visto(db: Session = Depends(get_db),
          cartera: models.Cartera = Depends(get_current_cartera)) -> None:
    svc.marcar_visto(db, cartera.id)
