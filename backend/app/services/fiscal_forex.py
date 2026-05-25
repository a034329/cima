"""Cálculo de G/P de divisa (forex) — Art. 33.5.e LIRPF.

Lee los resultados de periodo IBKR (tabla `resultados_ibkr`, categoría FOREX).
Sólo el `realized` es declarable (ganancia/pérdida patrimonial de la base del
ahorro); el `unrealized` es latente e informativo.

Filtro por `ejercicio` (None → acumulado, sumando por divisa entre años).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class ForexLinea:
    divisa: str
    realized_eur: Decimal
    unrealized_eur: Decimal


@dataclass
class ForexResultado:
    ejercicio: int                   # 0 = acumulado
    lineas: list[ForexLinea]
    realized_total: Decimal          # declarable (Art. 33.5.e)
    unrealized_total: Decimal        # latente, informativo
    periodo_inicio: date | None
    periodo_fin: date | None
    fecha_calculo: date


def calcular_forex(
    db: Session, cartera_id: str, ejercicio: int | None
) -> ForexResultado:
    q = (
        select(models.ResultadoIbkr)
        .where(models.ResultadoIbkr.cartera_id == cartera_id)
        .where(models.ResultadoIbkr.categoria == "FOREX")
        .where(models.ResultadoIbkr.estado == "confirmada")
    )
    if ejercicio is not None:
        q = q.where(models.ResultadoIbkr.ejercicio == ejercicio)
    rows = list(db.execute(q).scalars())

    por_divisa: dict[str, ForexLinea] = {}
    inicio: date | None = None
    fin: date | None = None
    for r in rows:
        l = por_divisa.get(r.clave)
        if l is None:
            l = ForexLinea(divisa=r.clave, realized_eur=Decimal("0"),
                           unrealized_eur=Decimal("0"))
            por_divisa[r.clave] = l
        l.realized_eur += Decimal(str(r.realized_eur))
        l.unrealized_eur += Decimal(str(r.unrealized_eur))
        if r.periodo_inicio and (inicio is None or r.periodo_inicio < inicio):
            inicio = r.periodo_inicio
        if r.periodo_fin and (fin is None or r.periodo_fin > fin):
            fin = r.periodo_fin

    lineas = sorted(por_divisa.values(), key=lambda x: x.divisa)
    realized_total = sum((l.realized_eur for l in lineas), Decimal("0"))
    unrealized_total = sum((l.unrealized_eur for l in lineas), Decimal("0"))

    return ForexResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        lineas=lineas,
        realized_total=realized_total,
        unrealized_total=unrealized_total,
        periodo_inicio=inicio,
        periodo_fin=fin,
        fecha_calculo=date.today(),
    )
