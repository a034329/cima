"""Endpoint de dividendos — espeja la pestaña Dividendos del Excel de Cuádrate."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.schemas.dividendos import (
    DividendoPorIsin,
    DividendosResumen,
    EventoDividendo,
)
from app.services.fiscal_dividendos import (
    calcular_dividendos,
    diversificacion_dividendos,
    serie_dividendos,
)
from decimal import Decimal
from pydantic import BaseModel, Field


router = APIRouter(prefix="/dividendos", tags=["dividendos"])

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


def _to_pagador(d: dict[str, Any]) -> DividendoPorIsin:
    return DividendoPorIsin(
        isin=d["isin"],
        nombre=d["nombre"],
        pais=d["pais"],
        bruto=d["bruto"],
        ret_origen=d["ret_origen"],
        retencion_es=d.get("retencion_es", 0),
        tasa_cdi=d.get("tasa_cdi"),
        limite_cdi=d["limite_cdi"],
        recuperable=d["recuperable"],
        exceso=d["exceso"],
        es_nacional=d["es_nacional"],
        sin_cdi=d["sin_cdi"],
        sin_retencion_es=d["sin_retencion_es"],
        brokers=d["brokers"],
        eventos=[
            EventoDividendo(
                fecha=e["fecha"], broker=e["broker"],
                bruto=e["bruto"], retencion=e["retencion"],
            )
            for e in d["eventos"]
        ],
    )


def _serializar(r) -> DividendosResumen:  # type: ignore[no-untyped-def]
    return DividendosResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        n_pagadores=len(r.resumen),
        bruto_total=r.bruto_total,
        ret_origen_total=r.ret_origen_total,
        ret_es_total=r.ret_es_total,
        cdi_recuperable_total=r.cdi_recuperable_total,
        exceso_total=r.exceso_total,
        bruto_ext_con_ret=r.bruto_ext_con_ret,
        pagadores=[_to_pagador(d) for d in r.resumen],
    )


def _cartera(db: Session) -> models.Cartera:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return cartera


class PuntoAnualOut(BaseModel):
    anio: int
    bruto: Decimal = Field(decimal_places=2)
    neto: Decimal = Field(decimal_places=2)


class PuntoMensualOut(BaseModel):
    anio: int
    mes: int
    bruto: Decimal = Field(decimal_places=2)


class SerieDividendosOut(BaseModel):
    anual: list[PuntoAnualOut]
    mensual: list[PuntoMensualOut]


@router.get("/serie", response_model=SerieDividendosOut,
            summary="Evolución de dividendos por año y por mes (gráficas)")
def get_serie_dividendos(db: Session = Depends(get_db)) -> SerieDividendosOut:
    s = serie_dividendos(db, _cartera(db).id)
    return SerieDividendosOut(
        anual=[PuntoAnualOut(anio=p.anio, bruto=p.bruto, neto=p.neto) for p in s.anual],
        mensual=[PuntoMensualOut(anio=p.anio, mes=p.mes, bruto=p.bruto) for p in s.mensual],
    )


class TrozoDivOut(BaseModel):
    clave: str
    bruto: Decimal = Field(decimal_places=2)


class DiversificacionOut(BaseModel):
    anio: int | None = None
    bruto_total: Decimal = Field(decimal_places=2)
    por_empresa: list[TrozoDivOut]
    por_pais: list[TrozoDivOut]
    por_sector: list[TrozoDivOut]


@router.get("/diversificacion", response_model=DiversificacionOut,
            summary="Reparto del dividendo por empresa, país y sector")
def get_diversificacion(anio: int | None = None,
                        db: Session = Depends(get_db)) -> DiversificacionOut:
    d = diversificacion_dividendos(db, _cartera(db).id, anio)
    conv = lambda lst: [TrozoDivOut(clave=t.clave, bruto=t.bruto) for t in lst]  # noqa: E731
    return DiversificacionOut(
        anio=d.anio, bruto_total=d.bruto_total,
        por_empresa=conv(d.por_empresa), por_pais=conv(d.por_pais),
        por_sector=conv(d.por_sector),
    )


@router.get("/acumulado", response_model=DividendosResumen,
            summary="Dividendos acumulados (todos los años)")
def get_dividendos_acumulado(db: Session = Depends(get_db)) -> DividendosResumen:
    cartera = _cartera(db)
    return _serializar(calcular_dividendos(db, cartera.id, None))


@router.get("/{ejercicio}", response_model=DividendosResumen,
            summary="Dividendos del ejercicio (bruto 0029, CDI 0588)")
def get_dividendos(ejercicio: int, db: Session = Depends(get_db)) -> DividendosResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    cartera = _cartera(db)
    return _serializar(calcular_dividendos(db, cartera.id, ejercicio))
