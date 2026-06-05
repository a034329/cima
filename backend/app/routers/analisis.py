"""Análisis profundo por empresa: /one-pager (estudio inicial con IA + web)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import comps as comps_svc
from app.services import jobs
from app.services import one_pager as svc
from app.services import valoracion as val_svc


router = APIRouter(prefix="/analisis", tags=["analisis"])


class OnePagerOut(BaseModel):
    isin: str
    nombre: str
    que_hace: str
    tesis: str
    riesgos: str
    valoracion: str
    encaje: str
    veredicto: str
    clasificacion: str
    fuentes: list[str]
    fecha: str
    proveedor: str
    disclaimer: str | None = None


class EscenarioOut(BaseModel):
    nombre: str
    multiplo: float
    metrica_base_4y: float
    precio_objetivo: float
    cagr4_pct: float | None = None
    razon: str
    # Guardias post-cálculo (bug BAM 5-jun-2026). El frontend usa `bloqueado`
    # para deshabilitar el botón "Aplicar a Estimaciones" cuando el escenario
    # es sospechoso de error dimensional (métrica agregada vs per-share) o
    # CAGR irreal.
    alertas: list[str] = []
    bloqueado: bool = False
    desglose: list[dict] = []


class ValoracionOut(BaseModel):
    isin: str
    nombre: str
    tipo_val: str
    precio_actual: float | None = None
    anclas: dict
    escenarios: list[EscenarioOut]
    fecha: str
    proveedor: str
    disclaimer: str | None = None


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


def _out(op: svc.OnePager) -> OnePagerOut:
    return OnePagerOut(
        isin=op.isin, nombre=op.nombre, que_hace=op.que_hace, tesis=op.tesis,
        riesgos=op.riesgos, valoracion=op.valoracion, encaje=op.encaje, veredicto=op.veredicto,
        clasificacion=op.clasificacion, fuentes=op.fuentes, fecha=op.fecha,
        proveedor=op.proveedor, disclaimer=op.disclaimer,
    )


class OnePagerEstadoOut(BaseModel):
    estado: str                              # ninguno | en_curso | ok | error
    error: str | None = None
    resultado: OnePagerOut | None = None


@router.get("/{isin}/one-pager", response_model=OnePagerEstadoOut,
            summary="Estado del one-pager + resultado guardado (para polling)")
def one_pager_estado(isin: str, db: Session = Depends(get_db)) -> OnePagerEstadoOut:
    cid = _cartera(db).id
    job = jobs.estado(db, cid, isin, "one_pager")
    res = svc.guardado(db, cid, isin)
    out = _out(res) if res else None
    if job and job.estado == jobs.EN_CURSO:
        return OnePagerEstadoOut(estado="en_curso", resultado=out)
    if out:
        return OnePagerEstadoOut(estado="ok", resultado=out)
    if job and job.estado == jobs.ERROR:
        return OnePagerEstadoOut(estado="error", error=job.error)
    return OnePagerEstadoOut(estado="ninguno")


@router.post("/{isin}/one-pager", response_model=OnePagerEstadoOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza la generación del one-pager en segundo plano (IA + web)")
def one_pager_lanzar(isin: str, db: Session = Depends(get_db)) -> OnePagerEstadoOut:
    jobs.lanzar(db, _cartera(db).id, isin, "one_pager", svc.generar)
    return OnePagerEstadoOut(estado="en_curso")


def _val_out(v: val_svc.Valoracion) -> ValoracionOut:
    return ValoracionOut(
        isin=v.isin, nombre=v.nombre, tipo_val=v.tipo_val, precio_actual=v.precio_actual,
        anclas=v.anclas,
        escenarios=[EscenarioOut(nombre=e.nombre, multiplo=e.multiplo,
                                 metrica_base_4y=e.metrica_base_4y, precio_objetivo=e.precio_objetivo,
                                 cagr4_pct=e.cagr4_pct, razon=e.razon,
                                 alertas=list(getattr(e, "alertas", []) or []),
                                 bloqueado=bool(getattr(e, "bloqueado", False)),
                                 desglose=list(getattr(e, "desglose", []) or []),
                                 ) for e in v.escenarios],
        fecha=v.fecha, proveedor=v.proveedor, disclaimer=v.disclaimer,
    )


class ValoracionEstadoOut(BaseModel):
    estado: str                              # ninguno | en_curso | ok | error
    error: str | None = None
    resultado: ValoracionOut | None = None


@router.get("/{isin}/valoracion", response_model=ValoracionEstadoOut,
            summary="Estado de la valoración + resultado guardado (para polling)")
def valoracion_estado(isin: str, db: Session = Depends(get_db)) -> ValoracionEstadoOut:
    cid = _cartera(db).id
    job = jobs.estado(db, cid, isin, "valoracion")
    v = val_svc.guardado(db, cid, isin)
    out = _val_out(v) if v else None
    if job and job.estado == jobs.EN_CURSO:
        return ValoracionEstadoOut(estado="en_curso", resultado=out)
    if out:
        return ValoracionEstadoOut(estado="ok", resultado=out)
    if job and job.estado == jobs.ERROR:
        return ValoracionEstadoOut(estado="error", error=job.error)
    return ValoracionEstadoOut(estado="ninguno")


@router.post("/{isin}/valoracion", response_model=ValoracionEstadoOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza los escenarios de valoración en segundo plano (solo PER)")
def valoracion_lanzar(isin: str, db: Session = Depends(get_db)) -> ValoracionEstadoOut:
    jobs.lanzar(db, _cartera(db).id, isin, "valoracion", val_svc.proponer)
    return ValoracionEstadoOut(estado="en_curso")


class PeerOut(BaseModel):
    nombre: str
    ticker: str
    per: float | None = None
    ev_ebitda: float | None = None
    p_fcf: float | None = None
    yield_pct: float | None = None
    crecimiento_pct: float | None = None
    roic_pct: float | None = None
    es_objetivo: bool = False


class CompsOut(BaseModel):
    isin: str
    nombre: str
    sector: str
    peers: list[PeerOut]
    lectura: str
    fuentes: list[str]
    fecha: str
    proveedor: str
    disclaimer: str | None = None


class CompsEstadoOut(BaseModel):
    estado: str                              # ninguno | en_curso | ok | error
    error: str | None = None
    resultado: CompsOut | None = None


def _comps_out(c: comps_svc.Comps) -> CompsOut:
    return CompsOut(
        isin=c.isin, nombre=c.nombre, sector=c.sector,
        peers=[PeerOut(**p.__dict__) for p in c.peers],
        lectura=c.lectura, fuentes=c.fuentes, fecha=c.fecha,
        proveedor=c.proveedor, disclaimer=c.disclaimer,
    )


@router.get("/{isin}/comps", response_model=CompsEstadoOut,
            summary="Estado de los comparables + resultado guardado (para polling)")
def comps_estado(isin: str, db: Session = Depends(get_db)) -> CompsEstadoOut:
    cid = _cartera(db).id
    job = jobs.estado(db, cid, isin, "comps")
    c = comps_svc.guardado(db, cid, isin)
    out = _comps_out(c) if c else None
    if job and job.estado == jobs.EN_CURSO:
        return CompsEstadoOut(estado="en_curso", resultado=out)
    if out:
        return CompsEstadoOut(estado="ok", resultado=out)
    if job and job.estado == jobs.ERROR:
        return CompsEstadoOut(estado="error", error=job.error)
    return CompsEstadoOut(estado="ninguno")


@router.post("/{isin}/comps", response_model=CompsEstadoOut,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Lanza la generación de comparables en segundo plano (IA + web)")
def comps_lanzar(isin: str, db: Session = Depends(get_db)) -> CompsEstadoOut:
    jobs.lanzar(db, _cartera(db).id, isin, "comps", comps_svc.generar)
    return CompsEstadoOut(estado="en_curso")
