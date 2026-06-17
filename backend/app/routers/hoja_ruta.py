"""Hoja de ruta (1.7): genera (en segundo plano) los pasos para cerrar el déficit
de la estrategia firmada; el usuario los aprueba (→ crear_paso). Nivel cartera."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import hoja_ruta as svc
from app.services import jobs

router = APIRouter(prefix="/hoja-ruta", tags=["hoja-ruta"])

_TIPO = "hoja_ruta"


class GapBloqueOut(BaseModel):
    categoria_base: str
    nombre: str
    peso_actual: float
    peso_objetivo: float
    deficit_eur: float
    n_posiciones: int


class PasoPropuestoOut(BaseModel):
    isin: str
    nombre: str
    decision: str
    prioridad: str
    capital_objetivo_eur: float | None = None
    razon: str
    en_cartera: bool


class HojaRutaOut(BaseModel):
    capital_eur: float
    liquidez_eur: float
    deficit: list[GapBloqueOut]
    pasos: list[PasoPropuestoOut]
    huecos: list[str]
    resumen: str
    fecha: str
    proveedor: str
    disclaimer: str | None = None


class HojaRutaEstadoOut(BaseModel):
    estado: str                              # ninguno | en_curso | ok | error
    error: str | None = None
    resultado: HojaRutaOut | None = None


def _out(hr: svc.HojaRuta) -> HojaRutaOut:
    return HojaRutaOut(
        capital_eur=hr.capital_eur, liquidez_eur=hr.liquidez_eur,
        deficit=[GapBloqueOut(**g.__dict__) for g in hr.deficit],
        pasos=[PasoPropuestoOut(**p.__dict__) for p in hr.pasos],
        huecos=hr.huecos, resumen=hr.resumen, fecha=hr.fecha,
        proveedor=hr.proveedor, disclaimer=hr.disclaimer,
    )


@router.get("", response_model=HojaRutaEstadoOut,
            summary="Estado de la hoja de ruta + resultado guardado (para polling)")
def estado(db: Session = Depends(get_db),
           cartera: models.Cartera = Depends(get_current_cartera)) -> HojaRutaEstadoOut:
    cid = cartera.id
    job = jobs.estado(db, cid, "", _TIPO)
    hr = svc.guardado(db, cid)
    out = _out(hr) if hr else None
    if job and job.estado == jobs.EN_CURSO:
        return HojaRutaEstadoOut(estado="en_curso", resultado=out)
    if out:
        return HojaRutaEstadoOut(estado="ok", resultado=out)
    if job and job.estado == jobs.ERROR:
        return HojaRutaEstadoOut(estado="error", error=job.error)
    return HojaRutaEstadoOut(estado="ninguno")


@router.post("/generar", response_model=HojaRutaEstadoOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza la generación de la hoja de ruta en segundo plano")
def generar(db: Session = Depends(get_db),
            cartera: models.Cartera = Depends(get_current_cartera)) -> HojaRutaEstadoOut:
    jobs.lanzar(db, cartera.id, "", _TIPO, svc.proponer)
    return HojaRutaEstadoOut(estado="en_curso")
