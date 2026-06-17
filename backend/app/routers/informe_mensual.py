"""Informe mensual de la cartera (V3): flujos del mes + foto IF a hoy."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from decimal import Decimal

from app.auth.deps import get_current_cartera
from app.db import get_db, models

router = APIRouter(prefix="/informe-mensual", tags=["informe-mensual"])


class MovimientoOut(BaseModel):
    fecha: date
    tipo: str
    nombre: str
    importe_eur: Decimal


class VentaOut(BaseModel):
    nombre: str
    isin: str
    gp_eur: Decimal


class InformeMensualOut(BaseModel):
    anio: int
    mes: int
    compras_eur: Decimal
    n_compras: int
    ventas_eur: Decimal
    n_ventas: int
    gastos_eur: Decimal
    aportaciones_eur: Decimal
    dividendos_bruto_eur: Decimal
    dividendos_retencion_eur: Decimal
    dividendos_neto_eur: Decimal
    intereses_eur: Decimal
    gp_realizada_eur: Decimal
    valor_mercado_eur: Decimal | None
    valor_mercado_var_pct: Decimal | None
    valor_mercado_completo: bool
    capital_estrategia_eur: Decimal | None
    objetivo_if_eur: Decimal | None
    progreso_if_pct: Decimal | None
    anios_if: Decimal | None
    destacados: list[MovimientoOut]
    ventas_detalle: list[VentaOut]


@router.get("/{anio}/{mes}", response_model=InformeMensualOut,
            summary="Cierre de mes: flujos, dividendos, G/P realizada y foto IF")
def get_informe_mensual(anio: int, mes: int,
                        db: Session = Depends(get_db),
                        cartera: models.Cartera = Depends(get_current_cartera)) -> InformeMensualOut:
    if not (1 <= mes <= 12) or not (2000 <= anio <= 2100):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mes o año fuera de rango")
    from app.services.informe_mensual import calcular_informe
    r = calcular_informe(db, cartera.id, anio, mes)
    return InformeMensualOut(
        anio=r.anio, mes=r.mes,
        compras_eur=r.compras_eur, n_compras=r.n_compras,
        ventas_eur=r.ventas_eur, n_ventas=r.n_ventas,
        gastos_eur=r.gastos_eur, aportaciones_eur=r.aportaciones_eur,
        dividendos_bruto_eur=r.dividendos_bruto_eur,
        dividendos_retencion_eur=r.dividendos_retencion_eur,
        dividendos_neto_eur=r.dividendos_neto_eur,
        intereses_eur=r.intereses_eur,
        gp_realizada_eur=r.gp_realizada_eur,
        valor_mercado_eur=r.valor_mercado_eur,
        valor_mercado_var_pct=r.valor_mercado_var_pct,
        valor_mercado_completo=r.valor_mercado_completo,
        capital_estrategia_eur=r.capital_estrategia_eur,
        objetivo_if_eur=r.objetivo_if_eur,
        progreso_if_pct=r.progreso_if_pct,
        anios_if=r.anios_if,
        destacados=[MovimientoOut(fecha=m.fecha, tipo=m.tipo, nombre=m.nombre,
                                  importe_eur=m.importe_eur) for m in r.destacados],
        ventas_detalle=[VentaOut(nombre=v.nombre, isin=v.isin, gp_eur=v.gp_eur)
                        for v in r.ventas_detalle],
    )
