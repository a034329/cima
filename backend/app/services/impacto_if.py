"""Impacto de una decisión en los años hasta la Independencia Financiera (V2).

Traduce un efecto en euros (p. ej. el coste fiscal de una rotación) a la
unidad que de verdad le importa al usuario: cuánto ACERCA o RETRASA la IF.
Usa los mismos parámetros que la proyección del dashboard (capital en
estrategia, objetivo configurable, aportación prevista/real, retorno
proyectado de Estimaciones acotado) — si cambia la doctrina allí, cambiarla
aquí (`app/services/dashboard.py`, sección de proyección).
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

_TARGET_IF = Decimal("300000")
_RETORNO_IF = Decimal("0.07")


def parametros_proyeccion_if(
    db: Session, cartera_id: str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(capital_if, aportacion_anual, objetivo, retorno) — espejo del dashboard."""
    from app.services.aportaciones import aportaciones_por_anio
    from app.services.dashboard import capital_en_estrategia_eur
    from app.services.estimaciones import agregado_cartera

    capital = capital_en_estrategia_eur(db, cartera_id)
    c = db.get(models.Cartera, cartera_id)
    objetivo = (Decimal(str(c.objetivo_if_eur))
                if c and c.objetivo_if_eur else _TARGET_IF)
    prevista_mensual = (Decimal(str(c.aportacion_mensual_eur))
                        if c and c.aportacion_mensual_eur else Decimal("0"))
    if prevista_mensual > 0:
        aportacion = prevista_mensual * 12
    else:
        real = aportaciones_por_anio(db, cartera_id).get(
            date.today().year, Decimal("0"))
        aportacion = real if real > 0 else Decimal("0")
    retorno = _RETORNO_IF
    agg = agregado_cartera(db, cartera_id, solo_estrategia=True)
    if agg.cagr4_div_ponderado_pct is not None and agg.cagr4_div_ponderado_pct > 0:
        retorno = min(Decimal(str(agg.cagr4_div_ponderado_pct)), Decimal("0.25"))
    return capital, aportacion, objetivo, retorno


def delta_anios_if(
    db: Session, cartera_id: str, delta_capital_eur: Decimal,
    params: tuple[Decimal, Decimal, Decimal, Decimal] | None = None,
) -> Decimal | None:
    """Años de retraso (>0) o adelanto (<0) en la IF si el capital en
    estrategia cambia HOY en `delta_capital_eur` (un coste fiscal entra en
    negativo). None si alguna de las dos proyecciones no converge (capital
    o aportación insuficientes para alcanzar el objetivo en 50 años).

    Acepta `params` precalculados para llamadas en bucle (rotación)."""
    from app.services import proyeccion

    if delta_capital_eur == 0:
        return Decimal("0.0")
    capital, aportacion, objetivo, retorno = (
        params if params is not None
        else parametros_proyeccion_if(db, cartera_id)
    )
    base = proyeccion.anios_hasta(capital, aportacion, objetivo, float(retorno))
    con = proyeccion.anios_hasta(
        capital + delta_capital_eur, aportacion, objetivo, float(retorno))
    if base is None or con is None:
        return None
    return (con - base).quantize(Decimal("0.1"), ROUND_HALF_UP)
