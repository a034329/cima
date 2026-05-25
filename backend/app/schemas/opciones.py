"""Schemas Pydantic para el endpoint de opciones."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ContratoOpcion(BaseModel):
    """Un contrato/serie de opción agrupado por subyacente+tipo+strike+venc."""
    subyacente: str
    tipo_op: str
    strike: str
    vencimiento: str
    brokers: str
    clasificacion: str          # normal / ejercida / mixta / long_abierta / short_abierta / roll_abierta
    primas_cobradas: Decimal = Field(decimal_places=2)
    primas_pagadas: Decimal = Field(decimal_places=2)
    gastos: Decimal = Field(decimal_places=2)
    pl_bruto: Decimal = Field(decimal_places=2)
    pl_neto: Decimal = Field(decimal_places=2)
    contratos_vendidos: Decimal = Field(decimal_places=2)
    contratos_comprados: Decimal = Field(decimal_places=2)
    expiradas: int
    n_ejercidas: int
    n_net_abiertos: int


class OpcionesResumen(BaseModel):
    """Respuesta del endpoint /api/opciones/{ejercicio}."""
    ejercicio: int
    fecha_calculo: date
    n_opciones: int
    n_contratos: int

    # Casilla 1626 (otros elementos patrimoniales — opciones cerradas/expiradas)
    pl_neto: Decimal = Field(decimal_places=2)
    pl_bruto: Decimal = Field(decimal_places=2)
    primas_cobradas: Decimal = Field(decimal_places=2)
    primas_pagadas: Decimal = Field(decimal_places=2)
    gastos: Decimal = Field(decimal_places=2)
    n_expiradas: int

    # Ejercidas: la prima integra en el coste/precio de las acciones (no 1626)
    ejercidas_prima_integrar: Decimal = Field(decimal_places=2)

    # Diferidas (no van a este ejercicio)
    long_abiertas_coste: Decimal = Field(decimal_places=2)
    short_abiertas_prima: Decimal = Field(decimal_places=2)

    contratos: list[ContratoOpcion]
