"""Cálculo de Letras del Tesoro / T-Bills — RCM (rendimiento capital mobiliario).

Lee los resultados de periodo IBKR (tabla `resultados_ibkr`, categoría TBILL).
La diferencia entre compra y amortización de una letra tributa en España como
RCM (no como ganancia patrimonial). IBKR ya reporta el `realized` en EUR.

Filtro por `ejercicio` (None → acumulado, sumando por símbolo entre años).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class BillLinea:
    simbolo: str
    realized_eur: Decimal


@dataclass
class BillsResultado:
    ejercicio: int                   # 0 = acumulado
    lineas: list[BillLinea]
    realized_total: Decimal          # RCM (rendimiento capital mobiliario)
    periodo_inicio: date | None
    periodo_fin: date | None
    fecha_calculo: date


def calcular_bills(
    db: Session, cartera_id: str, ejercicio: int | None
) -> BillsResultado:
    q = (
        select(models.ResultadoIbkr)
        .where(models.ResultadoIbkr.cartera_id == cartera_id)
        .where(models.ResultadoIbkr.categoria == "TBILL")
        .where(models.ResultadoIbkr.estado == "confirmada")
    )
    if ejercicio is not None:
        q = q.where(models.ResultadoIbkr.ejercicio == ejercicio)
    rows = list(db.execute(q).scalars())

    por_sym: dict[str, BillLinea] = {}
    inicio: date | None = None
    fin: date | None = None
    for r in rows:
        l = por_sym.get(r.clave)
        if l is None:
            l = BillLinea(simbolo=r.clave, realized_eur=Decimal("0"))
            por_sym[r.clave] = l
        l.realized_eur += Decimal(str(r.realized_eur))
        if r.periodo_inicio and (inicio is None or r.periodo_inicio < inicio):
            inicio = r.periodo_inicio
        if r.periodo_fin and (fin is None or r.periodo_fin > fin):
            fin = r.periodo_fin

    lineas = sorted(por_sym.values(), key=lambda x: x.simbolo)
    realized_total = sum((l.realized_eur for l in lineas), Decimal("0"))

    return BillsResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        lineas=lineas,
        realized_total=realized_total,
        periodo_inicio=inicio,
        periodo_fin=fin,
        fecha_calculo=date.today(),
    )
