"""Régimen macro: el usuario fija 4 indicadores → régimen + calibración de tramo."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import regimen as svc


router = APIRouter(prefix="/regimen", tags=["regimen"])


class CorreccionOut(BaseModel):
    sp_drawdown: float | None = None     # fracción negativa
    vix: float | None = None
    activa: bool = False
    escalado_min: int | None = None
    escalado_max: int | None = None
    nota: str = ""


class RegimenOut(BaseModel):
    indicadores: dict[str, str]      # {ciclo, inflacion, geopolitica, mercado}
    regimen: str                     # VERDE | AMARILLO | ROJO
    tramo_min: int
    tramo_max: int
    espaciado: str
    actualizado: str | None = None
    correccion: CorreccionOut | None = None     # regla del −14%


class RegimenIn(BaseModel):
    ciclo: str
    inflacion: str
    geopolitica: str
    mercado: str


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


def _out(e: svc.RegimenEstado) -> RegimenOut:
    from app.services.precios import mercado_correccion

    corr = svc.evaluar_correccion(e, mercado_correccion())
    return RegimenOut(
        indicadores=e.indicadores, regimen=e.regimen, tramo_min=e.tramo_min,
        tramo_max=e.tramo_max, espaciado=e.espaciado, actualizado=e.actualizado,
        correccion=CorreccionOut(
            sp_drawdown=corr.sp_drawdown, vix=corr.vix, activa=corr.activa,
            escalado_min=corr.escalado_min, escalado_max=corr.escalado_max, nota=corr.nota,
        ),
    )


@router.get("", response_model=RegimenOut, summary="Régimen macro vigente + calibración + regla −14%")
def get_regimen(db: Session = Depends(get_db)) -> RegimenOut:
    return _out(svc.estado_regimen(db, _cartera(db).id))


@router.put("", response_model=RegimenOut, summary="Fijar los 4 indicadores macro")
def put_regimen(payload: RegimenIn, db: Session = Depends(get_db)) -> RegimenOut:
    indicadores = payload.model_dump()
    invalidas = {k: v for k, v in indicadores.items() if v not in svc.SENALES}
    if invalidas:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Señal inválida {invalidas}; usa una de {svc.SENALES}",
        )
    return _out(svc.guardar_regimen(db, _cartera(db).id, indicadores))
