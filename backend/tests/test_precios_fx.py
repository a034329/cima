"""Regresión de conversión de divisa a EUR (_fx_eur).

Caso crítico: Reino Unido. yfinance distingue por CAJA — "GBp" = peniques,
"GBP" = libras. Un .upper() previo los colapsa y trata las libras como peniques
(÷100 → pérdida falsa del 99%). Bug detectado en Zegona (2026-05-22).
"""
from __future__ import annotations

from decimal import Decimal

from app.services.precios import _fx_eur


# EURGBP = 0.865 (0,865 GBP por EUR) → 1 GBP = 1,1561 EUR
_CACHE = {"fx:EURGBP=X": {"valor": 0.865, "ts": 9e9}}


def _eur(divisa: str, precio: float) -> Decimal:
    f = _fx_eur(divisa, dict(_CACHE))
    assert f is not None
    return (Decimal(str(precio)) * f).quantize(Decimal("0.01"))


def test_gbp_libras_no_se_dividen_por_100() -> None:
    # 17,94 GBP (libras) → ~20,74 EUR. NO debe dar ~0,21 EUR.
    assert _eur("GBP", 17.94) == Decimal("20.74")


def test_gbp_peniques_si_se_dividen_por_100() -> None:
    # 1794 GBp (peniques) = 17,94 GBP → ~20,74 EUR.
    assert _eur("GBp", 1794.0) == Decimal("20.74")
    assert _eur("GBX", 1794.0) == Decimal("20.74")


def test_eur_es_identidad() -> None:
    assert _fx_eur("EUR", {}) == Decimal("1")


def test_misma_cifra_eur_para_gbp_y_gbp_peniques() -> None:
    # El mismo valor económico expresado en libras o peniques → mismo EUR.
    assert _eur("GBP", 17.94) == _eur("GBp", 1794.0)


def test_fundamentales_lse_peniques_a_libras(monkeypatch) -> None:
    """Quirk yfinance LSE: precio en peniques (currency='GBp') pero BPA/dividendo
    en libras → _fetch_fundamentales los normaliza ×100. USD no escala."""
    import yfinance
    from app.services.precios import _fetch_fundamentales

    class _T:
        def __init__(self, sim, info):
            self.info = info

    gbp = {"currency": "GBp", "trailingEps": 0.8, "forwardEps": 0.9,
           "dividendRate": 0.63, "forwardPE": 20, "sector": "Consumer Defensive"}
    monkeypatch.setattr(yfinance, "Ticker", lambda s: _T(s, gbp))
    f = _fetch_fundamentales("DGE.L")
    assert f["eps"] == 80          # 0,8 libras → 80 peniques
    assert f["dividend"] == 63
    assert f["pe"] == 20           # ratio, sin escalar
    assert f["currency"] == "GBp"

    usd = {"currency": "USD", "trailingEps": 5.0, "dividendRate": 2.0, "forwardPE": 25}
    monkeypatch.setattr(yfinance, "Ticker", lambda s: _T(s, usd))
    f2 = _fetch_fundamentales("MSFT")
    assert f2["eps"] == 5.0        # USD no escala
    assert f2["dividend"] == 2.0
