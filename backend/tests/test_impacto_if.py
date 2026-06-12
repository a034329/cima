"""Tests del impacto en años-IF (V2)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.impacto_if import delta_anios_if


def test_delta_cero_sin_cambio(db, cartera) -> None:
    params = (Decimal("100000"), Decimal("12000"), Decimal("300000"), Decimal("0.07"))
    assert delta_anios_if(db, cartera.id, Decimal("0"), params=params) == Decimal("0.0")


def test_coste_fiscal_retrasa(db, cartera) -> None:
    params = (Decimal("100000"), Decimal("12000"), Decimal("300000"), Decimal("0.07"))
    d = delta_anios_if(db, cartera.id, Decimal("-20000"), params=params)
    assert d is not None and d > 0          # pagar 20k hoy retrasa la IF


def test_credito_adelanta(db, cartera) -> None:
    params = (Decimal("100000"), Decimal("12000"), Decimal("300000"), Decimal("0.07"))
    d = delta_anios_if(db, cartera.id, Decimal("20000"), params=params)
    assert d is not None and d < 0          # capital extra adelanta la IF


def test_none_si_no_converge(db, cartera) -> None:
    # Sin aportación y retorno 0 nunca se llega → None (no inventar números)
    params = (Decimal("1000"), Decimal("0"), Decimal("300000"), Decimal("0.0"))
    assert delta_anios_if(db, cartera.id, Decimal("-500"), params=params) is None
