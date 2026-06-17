"""Endpoint del dashboard (pantalla Resumen)."""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services.dashboard import calcular_dashboard


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _q2(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _q4(x) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP)


class CompBloqueOut(BaseModel):
    nombre: str
    categoria_base: str
    valor_eur: Decimal = Field(decimal_places=2)
    peso: Decimal = Field(decimal_places=4)
    cagr4_div_pct: Decimal | None = None
    cobertura: Decimal | None = None


class PosicionPesoOut(BaseModel):
    nombre: str
    isin: str
    categoria_base: str | None = None
    valor_eur: Decimal = Field(decimal_places=2)
    peso: Decimal = Field(decimal_places=4)


class PasoResumenOut(BaseModel):
    isin: str
    nombre: str
    decision: str
    prioridad: str


class OpcionRiesgoOut(BaseModel):
    simbolo: str
    tipo_op: str
    strike: str
    vencimiento: str
    dias_a_vencer: int | None
    moneyness: str | None
    es_corta: bool
    riesgo_ejercicio: bool


class DashboardOut(BaseModel):
    anio: int
    fecha_calculo: date
    capital_mercado_eur: Decimal = Field(decimal_places=2)
    gp_no_realizada_eur: Decimal = Field(decimal_places=2)
    gp_no_realizada_pct: Decimal = Field(decimal_places=4)
    liquidez_eur: Decimal = Field(decimal_places=2)
    # Desglose: total real en cuentas y cuánto está apartado en bloques fuera
    # de estrategia (colchón etc.). `liquidez_eur` = total − fuera_estrategia.
    liquidez_total_eur: Decimal = Field(decimal_places=2)
    liquidez_fuera_estrategia_eur: Decimal = Field(decimal_places=2)
    progreso_if_pct: Decimal = Field(decimal_places=4)
    anios_if: Decimal | None
    retorno_if_pct: Decimal = Field(decimal_places=4)
    composicion: list[CompBloqueOut]
    posiciones_peso: list[PosicionPesoOut]
    yield_actual_pct: Decimal = Field(decimal_places=4)
    dividendos_brutos_anio: Decimal = Field(decimal_places=2)
    yield_estimado_pct: Decimal | None
    cagr_anual_pct: Decimal | None
    retorno_5y_pct: Decimal | None
    proximos_pasos: list[PasoResumenOut]
    gp_realizada_anio: Decimal = Field(decimal_places=2)
    perdidas_por_aflorar: Decimal = Field(decimal_places=2)
    compensable_ahora: Decimal = Field(decimal_places=2)
    perdida_a_arrastrar: Decimal = Field(decimal_places=2)
    opciones_riesgo: list[OpcionRiesgoOut]
    opciones_proximas_vencer: int
    opciones_itm: int


@router.get("", response_model=DashboardOut,
            summary="Dashboard agregado (pantalla Resumen)")
def get_dashboard(db: Session = Depends(get_db),
                  cartera: models.Cartera = Depends(get_current_cartera)) -> DashboardOut:
    r = calcular_dashboard(db, cartera.id)
    return DashboardOut(
        anio=r.anio, fecha_calculo=r.fecha_calculo,
        capital_mercado_eur=_q2(r.capital_mercado_eur),
        gp_no_realizada_eur=_q2(r.gp_no_realizada_eur),
        gp_no_realizada_pct=_q4(r.gp_no_realizada_pct),
        liquidez_eur=_q2(r.liquidez_eur),
        liquidez_total_eur=_q2(r.liquidez_total_eur),
        liquidez_fuera_estrategia_eur=_q2(r.liquidez_fuera_estrategia_eur),
        progreso_if_pct=_q4(r.progreso_if_pct),
        anios_if=(_q2(r.anios_if) if r.anios_if is not None else None),
        retorno_if_pct=_q4(r.retorno_if_pct),
        composicion=[
            CompBloqueOut(nombre=c.nombre, categoria_base=c.categoria_base,
                          valor_eur=_q2(c.valor_eur), peso=_q4(c.peso),
                          cagr4_div_pct=(_q4(c.cagr4_div_pct) if c.cagr4_div_pct is not None else None),
                          cobertura=(_q4(c.cobertura) if c.cobertura is not None else None))
            for c in r.composicion
        ],
        posiciones_peso=[
            PosicionPesoOut(nombre=p.nombre, isin=p.isin, categoria_base=p.categoria_base,
                            valor_eur=_q2(p.valor_eur), peso=_q4(p.peso))
            for p in r.posiciones_peso
        ],
        yield_actual_pct=_q4(r.yield_actual_pct),
        dividendos_brutos_anio=_q2(r.dividendos_brutos_anio),
        yield_estimado_pct=(_q4(r.yield_estimado_pct) if r.yield_estimado_pct is not None else None),
        cagr_anual_pct=(_q4(r.cagr_anual_pct) if r.cagr_anual_pct is not None else None),
        retorno_5y_pct=(_q4(r.retorno_5y_pct) if r.retorno_5y_pct is not None else None),
        proximos_pasos=[
            PasoResumenOut(isin=p.isin, nombre=p.nombre, decision=p.decision,
                           prioridad=p.prioridad)
            for p in r.proximos_pasos
        ],
        gp_realizada_anio=_q2(r.gp_realizada_anio),
        perdidas_por_aflorar=_q2(r.perdidas_por_aflorar),
        compensable_ahora=_q2(r.compensable_ahora),
        perdida_a_arrastrar=_q2(r.perdida_a_arrastrar),
        opciones_riesgo=[
            OpcionRiesgoOut(
                simbolo=o.simbolo, tipo_op=o.tipo_op, strike=o.strike,
                vencimiento=o.vencimiento, dias_a_vencer=o.dias_a_vencer,
                moneyness=o.moneyness, es_corta=o.es_corta,
                riesgo_ejercicio=o.riesgo_ejercicio,
            )
            for o in r.opciones_riesgo
        ],
        opciones_proximas_vencer=r.opciones_proximas_vencer,
        opciones_itm=r.opciones_itm,
    )
