"""Endpoint del cálculo fiscal de opciones (DGT V2172-21).

GET /api/opciones/{ejercicio} y /api/opciones/acumulado.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.schemas.opciones import ContratoOpcion, OpcionesResumen
from app.services.fiscal_opciones import calcular_opciones, opciones_abiertas
from app.services.opciones import OpcionCandidata, reconciliar_opciones


router = APIRouter(prefix="/opciones", tags=["opciones"])


class OpcionAbiertaOut(BaseModel):
    subyacente: str
    tipo_op: str
    strike: str
    vencimiento: str
    contratos: int
    es_corta: bool
    prima_neta_eur: Decimal = Field(decimal_places=2)
    dias_a_vencer: int | None = None
    moneyness: str | None = None
    precio_subyacente: Decimal | None = Field(default=None, decimal_places=4)
    divisa_subyacente: str | None = None
    gp_estimada_eur: Decimal | None = Field(default=None, decimal_places=2)
    gp_estimada_pct: Decimal | None = Field(default=None, decimal_places=4)

_EJERCICIO_MIN = 2015
_EJERCICIO_MAX = 2030


def _clasificacion(c: dict[str, Any]) -> str:
    if c.get("es_mixta"):
        return "mixta"
    if c.get("es_ejercida"):
        return "ejercida"
    if c.get("es_long_abierta"):
        return "long_abierta"
    if c.get("es_short_abierta"):
        return "short_abierta"
    if c.get("es_roll_abierta"):
        return "roll_abierta"
    return "normal"


def _to_contrato(c: dict[str, Any]) -> ContratoOpcion:
    return ContratoOpcion(
        subyacente=c.get("subyacente", ""),
        tipo_op=c.get("tipo_op", "?"),
        strike=str(c.get("strike", "")),
        vencimiento=c.get("vencimiento", ""),
        brokers=c.get("brokers", ""),
        clasificacion=_clasificacion(c),
        primas_cobradas=c.get("primas_cobradas", Decimal("0")),
        primas_pagadas=c.get("primas_pagadas", Decimal("0")),
        gastos=c.get("gastos", Decimal("0")),
        pl_bruto=c.get("pl_bruto", Decimal("0")),
        pl_neto=c.get("pl_neto", Decimal("0")),
        contratos_vendidos=c.get("contratos_vendidos", Decimal("0")),
        contratos_comprados=c.get("contratos_comprados", Decimal("0")),
        expiradas=int(c.get("expiradas", 0)),
        n_ejercidas=int(c.get("n_ejercidas", 0)),
        n_net_abiertos=int(c.get("n_net_abiertos", 0)),
    )


def _serializar(r) -> OpcionesResumen:  # type: ignore[no-untyped-def]
    t = r.totales
    return OpcionesResumen(
        ejercicio=r.ejercicio,
        fecha_calculo=r.fecha_calculo,
        n_opciones=r.n_opciones,
        n_contratos=len(r.por_contrato),
        pl_neto=t.get("pl_neto", Decimal("0")),
        pl_bruto=t.get("pl_bruto", Decimal("0")),
        primas_cobradas=t.get("primas_cobradas", Decimal("0")),
        primas_pagadas=t.get("primas_pagadas", Decimal("0")),
        gastos=t.get("gastos", Decimal("0")),
        n_expiradas=int(t.get("n_expiradas", 0)),
        ejercidas_prima_integrar=t.get("ejercidas_prima_integrar", Decimal("0")),
        long_abiertas_coste=t.get("long_abiertas_coste", Decimal("0")),
        short_abiertas_prima=t.get("short_abiertas_prima", Decimal("0")),
        contratos=[_to_contrato(c) for c in r.por_contrato],
    )


class OpcionIn(BaseModel):
    """Payload para registrar una opción manualmente."""
    fecha: date
    subyacente: str = Field(min_length=1)
    tipo_op: str = Field(pattern="^[CP]$")          # C / P
    strike: str
    vencimiento: str                                # "19JUN26"
    accion: str = Field(pattern="^(compra|venta)$")
    cantidad: Decimal = Field(gt=0)
    prima_unitaria: Decimal = Field(ge=0)
    importe_eur: Decimal = Field(ge=0)
    gastos_eur: Decimal = Field(default=Decimal("0"), ge=0)
    expirada: bool = False
    ejercida: bool = False
    subyacente_isin: str | None = None
    broker_id: str | None = None
    isin: str | None = None


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Registrar opción manual")
def crear_opcion(payload: OpcionIn, db: Session = Depends(get_db),
                 cartera: models.Cartera = Depends(get_current_cartera)) -> dict[str, Any]:
    simbolo = f"{payload.subyacente} {payload.tipo_op}{payload.strike} {payload.vencimiento}"
    # external_id determinista para evitar duplicados de la misma operación.
    ext_id = (
        f"manual-{payload.subyacente}-{payload.fecha.isoformat()}-{payload.accion}-"
        f"{payload.tipo_op}{payload.strike}-{int(payload.importe_eur * 100)}"
    )
    cand = OpcionCandidata(
        fecha=payload.fecha, simbolo=simbolo, isin=payload.isin,
        tipo_op=payload.tipo_op, subyacente=payload.subyacente, strike=payload.strike,
        vencimiento=payload.vencimiento, accion=payload.accion,
        cantidad=payload.cantidad, prima_unitaria=payload.prima_unitaria,
        importe_eur=payload.importe_eur, gastos_eur=payload.gastos_eur,
        expirada=payload.expirada, ejercida=payload.ejercida,
        external_id=ext_id, broker_id=payload.broker_id,
        subyacente_isin=payload.subyacente_isin,
    )
    r = reconciliar_opciones(db, cartera.id, [cand])
    return {"insertadas": r.insertadas, "deduplicadas": r.deduplicadas, "simbolo": simbolo}


@router.get("/acumulado", response_model=OpcionesResumen,
            summary="Resumen de opciones acumulado (todos los años)")
def get_opciones_acumulado(db: Session = Depends(get_db),
                           cartera: models.Cartera = Depends(get_current_cartera)) -> OpcionesResumen:
    return _serializar(calcular_opciones(db, cartera.id, None))


@router.get("/abiertas", response_model=list[OpcionAbiertaOut],
            summary="Opciones abiertas vivas (para la vista de Cartera)")
def get_opciones_abiertas(db: Session = Depends(get_db),
                          cartera: models.Cartera = Depends(get_current_cartera)) -> list[OpcionAbiertaOut]:
    return [
        OpcionAbiertaOut(
            subyacente=o.subyacente, tipo_op=o.tipo_op, strike=o.strike,
            vencimiento=o.vencimiento, contratos=o.contratos, es_corta=o.es_corta,
            prima_neta_eur=o.prima_neta_eur, dias_a_vencer=o.dias_a_vencer,
            moneyness=o.moneyness, precio_subyacente=o.precio_subyacente,
            divisa_subyacente=o.divisa_subyacente, gp_estimada_eur=o.gp_estimada_eur,
            gp_estimada_pct=o.gp_estimada_pct,
        )
        for o in opciones_abiertas(db, cartera.id)
    ]


@router.get("/{ejercicio}", response_model=OpcionesResumen,
            summary="Resumen de opciones del ejercicio (DGT V2172-21)")
def get_opciones(ejercicio: int, db: Session = Depends(get_db),
                 cartera: models.Cartera = Depends(get_current_cartera)) -> OpcionesResumen:
    if not (_EJERCICIO_MIN <= ejercicio <= _EJERCICIO_MAX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ejercicio fuera de rango ({_EJERCICIO_MIN}-{_EJERCICIO_MAX})",
        )
    return _serializar(calcular_opciones(db, cartera.id, ejercicio))
