"""Proyección de capital hacia la IF: capitalización + aportación MENSUAL.

Fuente ÚNICA para (1) los años hasta el objetivo (dashboard) y (2) el retorno
anual requerido para un horizonte dado (onboarding). Que ambos usen la MISMA
simulación garantiza que sean consistentes: el retorno requerido a N años es el
inverso de "en cuántos años llegas con ese retorno". Antes divergían (el
onboarding usaba una fórmula de suma a interés compuesto distinta).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def valor_proyectado(capital: float, aportacion_anual: float,
                     retorno: float, anios: float) -> float:
    """Valor tras `anios` capitalizando a `retorno` anual con aportación MENSUAL
    (aportacion_anual / 12 cada mes, sumada tras capitalizar)."""
    r_m = (1 + retorno) ** (1 / 12) - 1
    ap_m = aportacion_anual / 12
    cap = capital
    for _ in range(int(round(anios * 12))):
        cap = cap * (1 + r_m) + ap_m
    return cap


def anios_hasta(capital: Decimal, aportacion_anual: Decimal, objetivo: Decimal,
                retorno: float, max_anios: int = 50) -> Decimal | None:
    """Años (fraccionados a 0,1) hasta `objetivo`. None si no se alcanza en
    `max_anios`. Capitalización e ingreso MENSUAL."""
    if capital >= objetivo:
        return Decimal("0")
    r_m = (1 + retorno) ** (1 / 12) - 1
    ap_m = float(aportacion_anual) / 12
    cap = float(capital)
    obj = float(objetivo)
    for m in range(1, max_anios * 12 + 1):
        cap = cap * (1 + r_m) + ap_m
        if cap >= obj:
            return Decimal(str(m / 12)).quantize(Decimal("0.1"), ROUND_HALF_UP)
    return None


def retorno_requerido(capital: Decimal, aportacion_anual: Decimal,
                      objetivo: Decimal, anios: float) -> float | None:
    """Retorno anual (fracción) necesario para alcanzar `objetivo` en EXACTAMENTE
    `anios` años con aportación mensual. Inverso de `anios_hasta`.
    - 0.0 si el capital + aportaciones ya llegan sin necesidad de crecer.
    - None si ni con un retorno extraordinario (500% anual) se alcanza."""
    if anios <= 0:
        return None
    cap, obj, ap = float(capital), float(objetivo), float(aportacion_anual)
    if valor_proyectado(cap, ap, 0.0, anios) >= obj:
        return 0.0
    hi = 5.0
    if valor_proyectado(cap, ap, hi, anios) < obj:
        return None                         # ni al 500% anual
    lo = 0.0
    for _ in range(60):                     # bisección monótona
        mid = (lo + hi) / 2
        if valor_proyectado(cap, ap, mid, anios) >= obj:
            hi = mid
        else:
            lo = mid
    return hi
