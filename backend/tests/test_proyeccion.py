"""Tests de la proyección compartida (años a IF ↔ retorno requerido)."""
from __future__ import annotations

from decimal import Decimal

from app.services import proyeccion


def test_valor_proyectado_capitaliza_mensual() -> None:
    # Sin aportación: 100k al 10% durante 1 año = 110k (capitalización mensual compuesta).
    assert abs(proyeccion.valor_proyectado(100000, 0, 0.10, 1) - 110000) < 1


def test_retorno_requerido_cero_si_aportaciones_ya_llegan() -> None:
    # 10k + 4.800/año × 10 = 58k ≥ 40k → no hace falta crecer.
    assert proyeccion.retorno_requerido(Decimal("10000"), Decimal("4800"),
                                        Decimal("40000"), 10) == 0.0


def test_retorno_requerido_none_si_imposible() -> None:
    # 1M en 1 año desde 1k con 100/mes → ni al 500% anual.
    assert proyeccion.retorno_requerido(Decimal("1000"), Decimal("1200"),
                                        Decimal("1000000"), 1) is None


def test_retorno_requerido_es_inverso_de_anios_hasta() -> None:
    """La consistencia que faltaba: si a un retorno r llegas en ~N años, el
    retorno requerido para ese horizonte N debe rondar r."""
    cap, ap, obj = Decimal("100000"), Decimal("24000"), Decimal("300000")
    anios = proyeccion.anios_hasta(cap, ap, obj, 0.10)
    assert anios is not None
    n = round(float(anios))
    r = proyeccion.retorno_requerido(cap, ap, obj, n)
    assert r is not None and abs(r - 0.10) < 0.04
