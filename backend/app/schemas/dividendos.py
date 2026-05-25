"""Schemas Pydantic para el endpoint de dividendos (espeja Excel Cuádrate)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class EventoDividendo(BaseModel):
    fecha: str
    broker: str
    bruto: Decimal = Field(decimal_places=2)
    retencion: Decimal = Field(decimal_places=2)


class DividendoPorIsin(BaseModel):
    isin: str
    nombre: str
    pais: str
    bruto: Decimal = Field(decimal_places=2)
    ret_origen: Decimal = Field(decimal_places=2)          # extranjera → 0588
    retencion_es: Decimal = Field(default=Decimal("0"), decimal_places=2)  # 19% ES → 0591
    tasa_cdi: Decimal | None = None       # % tope CDI (None si nacional / sin CDI)
    limite_cdi: Decimal = Field(decimal_places=2)
    recuperable: Decimal = Field(decimal_places=2)   # casilla 0588 (extranjero)
    exceso: Decimal = Field(decimal_places=2)        # no recuperable (coste)
    es_nacional: bool
    sin_cdi: bool
    sin_retencion_es: bool
    brokers: str
    eventos: list[EventoDividendo]


class DividendosResumen(BaseModel):
    ejercicio: int
    fecha_calculo: date
    n_pagadores: int

    bruto_total: Decimal = Field(decimal_places=2)            # → casilla 0029
    ret_origen_total: Decimal = Field(decimal_places=2)
    ret_es_total: Decimal = Field(decimal_places=2)           # popup 0029 (retenciones)
    cdi_recuperable_total: Decimal = Field(decimal_places=2)  # → casilla 0588
    exceso_total: Decimal = Field(decimal_places=2)           # perdido (no recuperable)
    bruto_ext_con_ret: Decimal = Field(decimal_places=2)      # base 0588

    pagadores: list[DividendoPorIsin]
