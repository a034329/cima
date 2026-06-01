"""Endpoints de posiciones enriquecidas + preferencias de columnas."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.services.posiciones import calcular_metricas_posiciones


router = APIRouter(tags=["posiciones"])


# Catálogo de columnas disponibles (id → label). El frontend lo usa para el
# selector. `default` indica las que se muestran por defecto. `pm_real` es fija.
COLUMNAS = [
    {"id": "pm_real", "label": "Precio medio (ponderado)", "default": True, "fija": True},
    {"id": "precio_actual_eur", "label": "Precio actual", "default": True, "fija": False},
    {"id": "precio_actual_local", "label": "Precio (divisa local)", "default": False, "fija": False},
    {"id": "gp_no_realizada_eur", "label": "G/P no realizada", "default": True, "fija": False},
    {"id": "gp_no_realizada_pct", "label": "G/P no realizada %", "default": True, "fija": False},
    {"id": "rentab_total_pct", "label": "Rentab. total % (+div+opc)", "default": True, "fija": False},
    {"id": "rentab_total_hist_pct", "label": "Rentab. histórica del valor (+cierres)", "default": False, "fija": False},
    {"id": "cagr4_div_pct", "label": "CAGR4+Div proyectado (4A)", "default": False, "fija": False},
    {"id": "pm_fiscal_es", "label": "PM fiscal ES", "default": False, "fija": False},
    {"id": "opciones_ejercidas_anio", "label": "Opciones ejercidas (año)", "default": True, "fija": False},
    {"id": "opciones_ejercidas_hist", "label": "Opciones ejercidas (histórico)", "default": False, "fija": False},
    {"id": "primas_opc_anio", "label": "Primas opciones netas (año)", "default": False, "fija": False},
    {"id": "primas_opc_hist", "label": "Primas opciones netas (histórico)", "default": False, "fija": False},
    {"id": "dividendos_anio", "label": "Dividendos (año)", "default": True, "fija": False},
    {"id": "dividendos_hist", "label": "Dividendos (histórico)", "default": False, "fija": False},
    {"id": "pm_desc", "label": "PM desc. div+primas", "default": False, "fija": False},
    {"id": "gp_realizada_anio", "label": "G/P realizada (año)", "default": True, "fija": False},
    {"id": "importe_diferido_2m", "label": "Diferido 2M", "default": True, "fija": False},
    {"id": "umbral_rotacion_1y_pct", "label": "Umbral rotación 1A", "default": False, "fija": False},
    {"id": "umbral_rotacion_2y_pct", "label": "Umbral rotación 2A", "default": False, "fija": False},
    {"id": "umbral_rotacion_3y_pct", "label": "Umbral rotación 3A", "default": False, "fija": False},
    {"id": "umbral_rotacion_4y_pct", "label": "Umbral rotación 4A", "default": False, "fija": False},
]
_IDS_VALIDOS = {c["id"] for c in COLUMNAS}
_DEFAULTS = [c["id"] for c in COLUMNAS if c["default"]]


class PosicionMetricasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    isin: str
    nombre: str
    cantidad: Decimal = Field(decimal_places=10)
    pm_real: Decimal = Field(decimal_places=4)
    precio_actual_eur: Decimal = Field(decimal_places=4)
    gp_no_realizada_eur: Decimal = Field(decimal_places=2)
    gp_no_realizada_pct: Decimal = Field(decimal_places=4)
    rentab_total_pct: Decimal = Field(decimal_places=4)
    rentab_total_hist_pct: Decimal = Field(decimal_places=4)
    primas_opc_anio: Decimal = Field(decimal_places=2)
    primas_opc_hist: Decimal = Field(decimal_places=2)
    pm_fiscal_es: Decimal = Field(decimal_places=4)
    opciones_ejercidas_anio: Decimal = Field(decimal_places=2)
    opciones_ejercidas_hist: Decimal = Field(decimal_places=2)
    dividendos_anio: Decimal = Field(decimal_places=2)
    dividendos_hist: Decimal = Field(decimal_places=2)
    pm_desc: Decimal = Field(decimal_places=4)
    importe_diferido_2m: Decimal = Field(decimal_places=2)
    gp_realizada_anio: Decimal = Field(decimal_places=2)
    decision: str                  # decisión vigente del plan (columna fija)
    tipo_activo: str = "STOCK"     # STOCK / ETF / CRYPTO
    precio_actual_local: Decimal | None = Field(default=None, decimal_places=4)
    divisa_cotizacion: str | None = None
    umbral_rotacion_1y_pct: Decimal | None = Field(default=None, decimal_places=4)
    umbral_rotacion_2y_pct: Decimal | None = Field(default=None, decimal_places=4)
    umbral_rotacion_3y_pct: Decimal | None = Field(default=None, decimal_places=4)
    umbral_rotacion_4y_pct: Decimal | None = Field(default=None, decimal_places=4)
    cagr4_div_pct: Decimal | None = Field(default=None, decimal_places=4)


class PosicionesResumen(BaseModel):
    anio: int
    columnas_catalogo: list[dict]
    columnas_seleccionadas: list[str]
    posiciones: list[PosicionMetricasOut]
    precios_actualizados: str | None = None   # ISO; cuándo se obtuvieron los precios


class ColumnasIn(BaseModel):
    columnas: list[str]


def _timestamp_precios() -> str | None:
    from app.services.precios import timestamp_precios
    return timestamp_precios()


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return c


def _seleccion_columnas(db: Session, cartera_id: str) -> list[str]:
    pref = db.execute(
        select(models.Preferencias).where(models.Preferencias.cartera_id == cartera_id)
    ).scalar_one_or_none()
    if pref is None or not pref.columnas_posiciones:
        return list(_DEFAULTS)
    try:
        sel = json.loads(pref.columnas_posiciones)
        sel = [c for c in sel if c in _IDS_VALIDOS]
    except (json.JSONDecodeError, TypeError):
        sel = list(_DEFAULTS)
    # pm_real siempre presente
    if "pm_real" not in sel:
        sel = ["pm_real", *sel]
    return sel


@router.get("/posiciones", response_model=PosicionesResumen,
            summary="Posiciones con métricas + columnas seleccionadas")
def get_posiciones(db: Session = Depends(get_db)) -> PosicionesResumen:
    cartera = _cartera(db)
    metricas = calcular_metricas_posiciones(db, cartera.id)
    return PosicionesResumen(
        anio=date.today().year,
        columnas_catalogo=COLUMNAS,
        columnas_seleccionadas=_seleccion_columnas(db, cartera.id),
        posiciones=metricas,  # type: ignore[arg-type]
        precios_actualizados=_timestamp_precios(),
    )


@router.put("/posiciones/columnas", response_model=PosicionesResumen,
            summary="Guardar selección de columnas (por cartera)")
def set_columnas(payload: ColumnasIn, db: Session = Depends(get_db)) -> PosicionesResumen:
    cartera = _cartera(db)
    sel = [c for c in payload.columnas if c in _IDS_VALIDOS]
    if "pm_real" not in sel:
        sel = ["pm_real", *sel]

    pref = db.execute(
        select(models.Preferencias).where(models.Preferencias.cartera_id == cartera.id)
    ).scalar_one_or_none()
    if pref is None:
        pref = models.Preferencias(cartera_id=cartera.id)
        db.add(pref)
    pref.columnas_posiciones = json.dumps(sel)
    db.commit()

    metricas = calcular_metricas_posiciones(db, cartera.id)
    return PosicionesResumen(
        anio=date.today().year,
        columnas_catalogo=COLUMNAS,
        columnas_seleccionadas=sel,
        posiciones=metricas,  # type: ignore[arg-type]
        precios_actualizados=_timestamp_precios(),
    )
