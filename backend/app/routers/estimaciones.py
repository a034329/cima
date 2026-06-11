"""Endpoints de estimaciones de valoración (Fase 2, multi-método WG)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services import estimaciones as svc


router = APIRouter(prefix="/estimaciones", tags=["estimaciones"])


def _q(x, places: str):  # type: ignore[no-untyped-def]
    return None if x is None else Decimal(str(x)).quantize(Decimal(places), ROUND_HALF_UP)


class EstimacionOut(BaseModel):
    isin: str
    nombre: str
    tipo_val: str
    divisa: str | None
    precio_actual: Decimal | None
    eps_actual: Decimal | None
    multiplo_objetivo: Decimal | None
    metrica_base_4y: Decimal | None
    dividendo_share: Decimal | None
    precio_objetivo: Decimal | None
    crecimiento_pct: Decimal | None
    cagr4_pct: Decimal | None
    div_yield_pct: Decimal | None
    cagr4_div_pct: Decimal | None          # MAESTRA: neto + crecimiento 4Y
    cagr4_div_bruto_pct: Decimal | None = None   # plano/bruto (reconciliación Excel)
    div_yield_neto_pct: Decimal | None = None
    div_horizonte_pct: Decimal | None = None
    tipo_efectivo_div_pct: Decimal | None = None
    crecimiento_div_pct: Decimal | None = None   # g_div aplicado (campo o derivado)
    notas: str | None
    # Consenso de analistas (referencia, NO editable):
    eps_forward: Decimal | None
    eps_consenso_4y: Decimal | None
    eps_consenso_high: Decimal | None
    eps_consenso_low: Decimal | None
    num_analistas_eps: int | None
    anio_consenso_4y: int | None
    precio_obj_consenso: Decimal | None
    target_high: Decimal | None
    target_low: Decimal | None
    per_hist_medio: Decimal | None
    per_hist_mediano: Decimal | None
    mult_alerta: str | None


class EstimacionesResumen(BaseModel):
    estimaciones: list[EstimacionOut]
    yield_estimado_pct: Decimal | None
    cagr4_div_ponderado_pct: Decimal | None
    cobertura: Decimal = Field(decimal_places=4)


class EstimacionIn(BaseModel):
    tipo_val: str | None = None
    eps_actual: Decimal | None = None
    multiplo_objetivo: Decimal | None = None
    metrica_base_4y: Decimal | None = None
    dividendo_share: Decimal | None = None
    crecimiento_div_pct: Decimal | None = None   # g_div editable (fracción)
    notas: str | None = None


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay cartera. POST /api/bootstrap")
    return c


def _to_out(c) -> EstimacionOut:  # type: ignore[no-untyped-def]
    return EstimacionOut(
        isin=c.isin, nombre=c.nombre, tipo_val=c.tipo_val, divisa=c.divisa,
        precio_actual=_q(c.precio_actual, "0.0001"), eps_actual=_q(c.eps_actual, "0.0001"),
        multiplo_objetivo=_q(c.multiplo_objetivo, "0.0001"),
        metrica_base_4y=_q(c.metrica_base_4y, "0.0001"),
        dividendo_share=_q(c.dividendo_share, "0.000001"),
        precio_objetivo=_q(c.precio_objetivo, "0.0001"),
        crecimiento_pct=_q(c.crecimiento_pct, "0.0001"),
        cagr4_div_bruto_pct=_q(c.cagr4_div_bruto_pct, "0.0001"),
        div_yield_neto_pct=_q(c.div_yield_neto_pct, "0.0001"),
        div_horizonte_pct=_q(c.div_horizonte_pct, "0.0001"),
        tipo_efectivo_div_pct=_q(c.tipo_efectivo_div_pct, "0.0001"),
        crecimiento_div_pct=_q(c.crecimiento_div_aplicado_pct, "0.0001"),
        cagr4_pct=_q(c.cagr4_pct, "0.0001"), div_yield_pct=_q(c.div_yield_pct, "0.0001"),
        cagr4_div_pct=_q(c.cagr4_div_pct, "0.0001"), notas=c.notas,
        eps_forward=_q(c.eps_forward, "0.0001"),
        eps_consenso_4y=_q(c.eps_consenso_4y, "0.0001"),
        eps_consenso_high=_q(c.eps_consenso_high, "0.0001"),
        eps_consenso_low=_q(c.eps_consenso_low, "0.0001"),
        num_analistas_eps=c.num_analistas_eps, anio_consenso_4y=c.anio_consenso_4y,
        precio_obj_consenso=_q(c.precio_obj_consenso, "0.0001"),
        target_high=_q(c.target_high, "0.0001"), target_low=_q(c.target_low, "0.0001"),
        per_hist_medio=_q(c.per_hist_medio, "0.0001"),
        per_hist_mediano=_q(c.per_hist_mediano, "0.0001"),
        mult_alerta=c.mult_alerta,
    )


@router.get("", response_model=EstimacionesResumen,
            summary="Estimaciones por posición + agregado de cartera")
def get_estimaciones(db: Session = Depends(get_db)) -> EstimacionesResumen:
    cid = _cartera(db).id
    calcs = svc.calcular_estimaciones(db, cid)
    agg = svc.agregado_cartera(db, cid)
    return EstimacionesResumen(
        estimaciones=[_to_out(c) for c in calcs],
        yield_estimado_pct=_q(agg.yield_estimado_pct, "0.0001"),
        cagr4_div_ponderado_pct=_q(agg.cagr4_div_ponderado_pct, "0.0001"),
        cobertura=_q(agg.cobertura, "0.0001"),
    )


@router.post("/prefill", summary="Auto-rellenar estimaciones desde el feed (campos vacíos)")
def prefill(db: Session = Depends(get_db)) -> dict[str, int]:
    n = svc.prefill_estimaciones(db, _cartera(db).id)
    return {"actualizadas": n}


@router.put("/{isin}", status_code=status.HTTP_204_NO_CONTENT,
            summary="Editar la estimación de una posición")
def editar(isin: str, payload: EstimacionIn, db: Session = Depends(get_db)) -> None:
    cid = _cartera(db).id
    if payload.tipo_val is not None and payload.tipo_val not in models.TIPOS_VAL:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"tipo_val inválido: {payload.tipo_val}")
    e = db.execute(
        select(models.Estimacion)
        .where(models.Estimacion.cartera_id == cid)
        .where(models.Estimacion.isin == isin)
    ).scalars().first()
    if e is None:
        e = models.Estimacion(cartera_id=cid, isin=isin, tipo_val=payload.tipo_val or "PER")
        db.add(e)
    campos = payload.model_dump(exclude_unset=True)
    for k, v in campos.items():
        setattr(e, k, v)
    # Fijar el tipo_val cuenta como confirmación: levanta la marca defensiva de
    # "revisar tipo" y autoriza al prefill a volver a sembrar (ver _seed_estimacion).
    if "tipo_val" in campos:
        import json
        meta = {}
        if e.consenso_json:
            try:
                meta = json.loads(e.consenso_json) or {}
            except ValueError:
                meta = {}
        meta["tipo_confirmado"] = True
        meta.pop("revisar_tipo_val", None)
        e.consenso_json = json.dumps(meta)
    db.commit()
