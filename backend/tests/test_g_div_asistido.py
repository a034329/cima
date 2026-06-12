"""Tests del g_div asistido (V5): siembra de crecimiento_div_pct desde DPS real."""
from __future__ import annotations

from decimal import Decimal

from app.db import models
from app.services.estimaciones import _crecimiento_dps, _seed_estimacion


def test_crecimiento_dps_cagr_basico() -> None:
    # 1.00 → 1.21 en 2 años ⇒ 10% anual
    assert abs(_crecimiento_dps([1.0, 1.1, 1.21]) - 0.10) < 1e-9


def test_crecimiento_dps_acota_banda() -> None:
    assert _crecimiento_dps([1.0, 2.0, 4.0]) == 0.20      # 100% anual → tope
    assert _crecimiento_dps([4.0, 2.0, 1.0]) == -0.05     # −50% anual → suelo


def test_crecimiento_dps_exige_tres_anios() -> None:
    assert _crecimiento_dps([1.0, 1.5]) is None
    assert _crecimiento_dps(None) is None
    assert _crecimiento_dps([0.0, 0.0, 1.0]) is None      # solo 1 punto > 0


def test_crecimiento_dps_conserva_indice_temporal() -> None:
    # Año sin pago en medio: [1, 0, 1.21] ⇒ 2 puntos>0... <3 → None (no extrapolar)
    assert _crecimiento_dps([1.0, 0.0, 1.21]) is None
    # [1, 0.5, 1.21, 1.331]: 4 puntos>0, CAGR sobre 3 años = 10%
    assert abs(_crecimiento_dps([1.0, 0.5, 1.21, 1.331]) - (1.331 ** (1 / 3) - 1)) < 1e-9


def test_seed_siembra_g_div_desde_dps() -> None:
    e = models.Estimacion(cartera_id="c", isin="US0000000001", tipo_val="PER")
    _seed_estimacion(e, {"dps_hist": [1.0, 1.1, 1.21]}, None)
    assert e.crecimiento_div_pct == Decimal("0.1000")


def test_seed_no_pisa_g_div_del_usuario() -> None:
    e = models.Estimacion(cartera_id="c", isin="US0000000001", tipo_val="PER",
                          crecimiento_div_pct=Decimal("0.05"))
    _seed_estimacion(e, {"dps_hist": [1.0, 1.1, 1.21]}, None)
    assert e.crecimiento_div_pct == Decimal("0.05")


def test_seed_fondo_no_siembra_g_div() -> None:
    e = models.Estimacion(cartera_id="c", isin="IE00B4L5Y983", tipo_val="PER")
    _seed_estimacion(e, {"dps_hist": [1.0, 1.1, 1.21], "nombre": "iShares Core MSCI World ETF"}, None)
    assert e.crecimiento_div_pct is None
