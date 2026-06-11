"""Tipo efectivo del dividendo para la métrica CAGR4+Div NETA.

Decisión de doctrina (Angel, 2026-06-11): la componente Div de la métrica
maestra usa el dividendo neto de lo que se paga DE VERDAD cada año:

    tipo_efectivo = tipo_ahorro (suelo, 19% configurable)
                  + exceso de la retención de ORIGEN sobre el tope del CDI

La retención española (0591) y la retención de origen hasta el tope del
convenio (0588) son CRÉDITOS en la propia declaración — no se descuentan
otra vez. El exceso (DE 26,375%→15%, CH 35%→15%, BE 30%→15%…) solo se
recupera reclamando al fisco extranjero (funcionalidad futura): mientras
tanto es coste real y resta retorno.

Ejemplos (suelo 19%): US/UK/ES → 19% · DE → 30,4% · CH → 39% · BE → 34%.

Tablas: topes CDI (`DTA_SOURCE_MAX`) y retenciones estatutarias reales
(`TR_SOURCE_WHT_RATE`) del vendor de Cuádrate — verificadas contra BOE/
fuentes primarias en la auditoría 2026-06-11 (incl. CDI Japón 5%). Para
países sin retención estatutaria conocida se asume retención == tope CDI
(exceso 0): sin dato no se inventa coste.

La componente de CRECIMIENTO del dividendo (g_div) refleja el check "yield
a medio plazo" del protocolo de rotaciones: un 4% creciendo al 15% supera
en el año 4 a un 12% plano.

    factor_horizonte(g) = media[(1+g)^t, t=1..4]
    Div_horizonte = yield_actual × (1 − tipo_efectivo) × factor_horizonte
"""
from __future__ import annotations

from decimal import Decimal

from app.config import settings

_CERO = Decimal("0")


def _tablas_vendor() -> tuple[dict, dict]:
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    return g.DTA_SOURCE_MAX, g.TR_SOURCE_WHT_RATE


def exceso_no_recuperable_pct(pais: str | None) -> Decimal:
    """Puntos de retención de origen por ENCIMA del tope CDI (fracción).
    0 si el país no retiene, si la estatutaria ≤ tope, o si no hay dato."""
    if not pais:
        return _CERO
    topes, estatutarias = _tablas_vendor()
    est = estatutarias.get(pais.upper())
    if est is None:
        return _CERO   # sin dato de estatutaria → no inventar coste
    tope = topes.get(pais.upper(), est)
    exceso = Decimal(str(est)) - Decimal(str(tope))
    return exceso if exceso > 0 else _CERO


def tipo_efectivo_dividendo(pais: str | None) -> Decimal:
    """Fracción del dividendo bruto que se pierde en impuestos no
    acreditables: suelo del ahorro + exceso de origen sobre el tope CDI."""
    suelo = Decimal(str(settings.tipo_ahorro_dividendo))
    return suelo + exceso_no_recuperable_pct(pais)


def factor_horizonte_div(g_div: Decimal | None, anios: int = 4) -> Decimal:
    """Media de (1+g)^t para t=1..anios — el multiplicador del yield actual
    que representa el yield medio cobrado durante el horizonte."""
    if g_div is None or g_div == 0:
        return Decimal("1")
    total = _CERO
    base = Decimal("1") + g_div
    acum = Decimal("1")
    for _ in range(anios):
        acum *= base
        total += acum
    return total / Decimal(anios)


def pais_de_isin(isin: str | None, nombre: str | None = None) -> str | None:
    """País del emisor según el vendor (resuelve ADRs vía ADR_PAIS_REAL)."""
    if not isin:
        return None
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    return g._pais_de_isin(isin, nombre or "")
