"""Tests del score de salud del dividendo (V6)."""
from __future__ import annotations

from app.services.salud_dividendo import _score
from app.services.precios import _cobertura_fcf


def test_score_alta() -> None:
    nivel, motivo = _score(payout=0.40, cobertura=2.0)
    assert nivel == "ALTA"
    assert "2.0×" in motivo and "40%" in motivo


def test_score_media_por_cobertura_ajustada() -> None:
    assert _score(payout=0.40, cobertura=1.2)[0] == "MEDIA"


def test_score_media_por_payout() -> None:
    assert _score(payout=0.80, cobertura=2.0)[0] == "MEDIA"


def test_score_riesgo_cobertura_baja() -> None:
    nivel, motivo = _score(payout=0.50, cobertura=0.9)
    assert nivel == "RIESGO" and "0.90×" in motivo


def test_score_riesgo_payout_negativo() -> None:
    # Pierde dinero y paga dividendo → riesgo aunque el FCF aún cubra
    nivel, motivo = _score(payout=-0.5, cobertura=2.0)
    assert nivel == "RIESGO" and "perdiendo dinero" in motivo


def test_score_sin_datos() -> None:
    assert _score(payout=None, cobertura=None)[0] == "SIN_DATOS"


def test_cobertura_fcf_unidades_crudas() -> None:
    # 1.000 M de FCF / (2 €/acc × 250 M acc) = 2,0×
    info = {"freeCashflow": 1_000_000_000, "dividendRate": 2.0,
            "sharesOutstanding": 250_000_000}
    assert _cobertura_fcf(info) == 2.0


def test_cobertura_fcf_sin_datos() -> None:
    assert _cobertura_fcf({"freeCashflow": 1.0}) is None
    assert _cobertura_fcf({"freeCashflow": 1.0, "dividendRate": 0,
                           "sharesOutstanding": 100}) is None
