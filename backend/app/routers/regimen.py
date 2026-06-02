"""Régimen macro: el usuario fija 4 indicadores → régimen + calibración de tramo.
Auto-clasificación híbrida (números + IA con web) detrás de un job firmable."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import jobs
from app.services import regimen as svc
from app.services import regimen_auto


router = APIRouter(prefix="/regimen", tags=["regimen"])

_TIPO_AUTO = "regimen_auto"


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


# ── Auto-clasificación (híbrido números + IA, propuesta firmable) ──────────

class IndicadorPropuestaOut(BaseModel):
    senal: str                                  # VERDE | AMARILLA | ROJA
    razon: str
    fuentes: list[str] = []
    datos: dict = {}


class PropuestaOut(BaseModel):
    indicadores: dict[str, IndicadorPropuestaOut]
    regimen: str
    datos_objetivos: dict
    proveedor: str
    modelo: str
    created_at: str


class AutoEstadoOut(BaseModel):
    estado: str                                 # ninguno | en_curso | ok | error
    error: str | None = None
    propuesta: PropuestaOut | None = None


def _propuesta_out(p: regimen_auto.Propuesta) -> PropuestaOut:
    return PropuestaOut(
        indicadores={k: IndicadorPropuestaOut(
            senal=v.senal, razon=v.razon, fuentes=list(v.fuentes), datos=dict(v.datos),
        ) for k, v in p.indicadores.items()},
        regimen=p.regimen, datos_objetivos=dict(p.datos_objetivos),
        proveedor=p.proveedor, modelo=p.modelo, created_at=p.created_at,
    )


@router.get("/auto", response_model=AutoEstadoOut,
            summary="Estado del job de auto-clasificación + última propuesta")
def get_auto(db: Session = Depends(get_db)) -> AutoEstadoOut:
    cid = _cartera(db).id
    job = jobs.estado(db, cid, "", _TIPO_AUTO)
    p = regimen_auto.cargar_propuesta(db, cid)
    out = _propuesta_out(p) if p else None
    if job and job.estado == jobs.EN_CURSO:
        return AutoEstadoOut(estado="en_curso", propuesta=out)
    if out:
        return AutoEstadoOut(estado="ok", propuesta=out)
    if job and job.estado == jobs.ERROR:
        return AutoEstadoOut(estado="error", error=job.error)
    return AutoEstadoOut(estado="ninguno")


@router.post("/auto", response_model=AutoEstadoOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza la auto-clasificación en segundo plano (números + IA)")
def post_auto(db: Session = Depends(get_db)) -> AutoEstadoOut:
    jobs.lanzar(db, _cartera(db).id, "", _TIPO_AUTO, regimen_auto.proponer)
    return AutoEstadoOut(estado="en_curso")


@router.post("/firmar", response_model=RegimenOut,
             summary="Firma la propuesta vigente y la aplica al régimen")
def firmar(db: Session = Depends(get_db)) -> RegimenOut:
    cid = _cartera(db).id
    try:
        estado = regimen_auto.firmar(db, cid)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return _out(estado)


@router.delete("/auto", status_code=status.HTTP_204_NO_CONTENT,
               summary="Descarta la propuesta sin firmarla")
def delete_auto(db: Session = Depends(get_db)) -> None:
    regimen_auto.descartar_propuesta(db, _cartera(db).id)
