"""Histórico de cierres mensuales y evolución de la cartera (ADR-004).

GET lee del caché global; si faltan meses, el front llama a POST /refrescar para
disparar el backfill en segundo plano (job) y hace polling del GET.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import historico, jobs

router = APIRouter(prefix="/historico", tags=["historico"])

_TIPO = "historico_precios"


class PuntoOut(BaseModel):
    anio_mes: str
    valor_eur: str
    aportado_eur: str
    completo: bool


class SerieOut(BaseModel):
    puntos: list[PuntoOut]
    meses_pendientes: int
    job: str                     # ninguno | en_curso | ok | error


def _job_estado(db: Session, cartera_id: str) -> str:
    j = jobs.estado(db, cartera_id, "", _TIPO)
    if j is None:
        return "ninguno"
    return j.estado


def _serie_out(serie: historico.SerieEvolucion, job: str) -> SerieOut:
    return SerieOut(
        puntos=[
            PuntoOut(
                anio_mes=p.anio_mes,
                valor_eur=f"{p.valor_eur:.2f}",
                aportado_eur=f"{p.aportado_eur:.2f}",
                completo=p.completo,
            )
            for p in serie.puntos
        ],
        meses_pendientes=serie.meses_pendientes,
        job=job,
    )


@router.get("/cartera", response_model=SerieOut,
            summary="Evolución mensual de la cartera (valor de mercado + aportado)")
def get_serie_cartera(
    db: Session = Depends(get_db),
    cartera: models.Cartera = Depends(get_current_cartera),
) -> SerieOut:
    serie = historico.serie_cartera(db, cartera.id)
    return _serie_out(serie, _job_estado(db, cartera.id))


@router.get("/posicion/{isin}", response_model=SerieOut,
            summary="Evolución mensual del valor de una posición")
def get_serie_posicion(
    isin: str,
    db: Session = Depends(get_db),
    cartera: models.Cartera = Depends(get_current_cartera),
) -> SerieOut:
    serie = historico.serie_posicion(db, cartera.id, isin)
    return _serie_out(serie, _job_estado(db, cartera.id))


@router.post("/refrescar", status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza el backfill del histórico en segundo plano")
def refrescar(
    db: Session = Depends(get_db),
    cartera: models.Cartera = Depends(get_current_cartera),
) -> dict:
    lanzado = jobs.lanzar(db, cartera.id, "", _TIPO, historico.poblar_historico)
    return {"lanzado": lanzado, "job": _job_estado(db, cartera.id)}
