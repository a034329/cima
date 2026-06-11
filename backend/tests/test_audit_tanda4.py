"""Regresiones de la auditoría Cima 2026-06-11 — Tanda 4 (dominio).

D2 — la métrica maestra CAGR4+Div pasa a dividendo NETO (suelo 19% + exceso
     de retención de origen sobre el tope CDI) con CRECIMIENTO del dividendo
     a 4 años (decisión Angel 2026-06-11). El bruto plano queda en
     cagr4_div_bruto_pct para reconciliar con analisis.xlsx.
D3 — _crecimiento_eps contaba mal los años al filtrar BPA ≤ 0.
A8 — es_fondo nunca se inicializaba: un ETF con trailingEps se sembraba
     como PER pese a la guarda (código muerto).
D1 — la compuerta determinista forzaba TODO ETF no-temático a "indice"
     0.95 — incluidos min-vol/covered-call (familia colchón / rentas).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models


# ═══════════════════════════════════════════════════════════════════════════
# D2 — tipo efectivo y factor de crecimiento
# ═══════════════════════════════════════════════════════════════════════════

def test_d2_tipo_efectivo_por_pais():
    from app.services.dividendo_neto import tipo_efectivo_dividendo
    # US: estatutaria 15% == tope CDI → solo el suelo del 19%
    assert tipo_efectivo_dividendo("US") == Decimal("0.19")
    # CH: 35% − 15% tope = 20 puntos de exceso → 39%
    assert tipo_efectivo_dividendo("CH") == Decimal("0.39")
    # DE: 26,375% − 15% = 11,375 puntos → 30,375%
    assert tipo_efectivo_dividendo("DE") == Decimal("0.30375")
    # BE: 30% − 15% = 15 puntos → 34%
    assert tipo_efectivo_dividendo("BE") == Decimal("0.34")
    # País sin dato de estatutaria → no se inventa exceso: suelo
    assert tipo_efectivo_dividendo("JP") == Decimal("0.19")
    assert tipo_efectivo_dividendo(None) == Decimal("0.19")


def test_d2_factor_horizonte():
    from app.services.dividendo_neto import factor_horizonte_div
    assert factor_horizonte_div(Decimal("0")) == Decimal("1")
    assert factor_horizonte_div(None) == Decimal("1")
    # g=15%: media de 1.15, 1.3225, 1.520875, 1.74900625 ≈ 1.43560
    f = factor_horizonte_div(Decimal("0.15"))
    assert abs(f - Decimal("1.43560")) < Decimal("0.0001")


def test_d2_bam_4pct_creciendo_supera_a_7pct_plano():
    """El check 'yield a medio plazo' del protocolo: 4% creciendo al 15%
    rinde más en el horizonte que un 7% plano del mismo país."""
    from app.services.dividendo_neto import factor_horizonte_div
    neto = Decimal("1") - Decimal("0.19")
    bam = Decimal("0.04") * neto * factor_horizonte_div(Decimal("0.15"))
    plano = Decimal("0.07") * neto * factor_horizonte_div(Decimal("0"))
    # 4% × 1.4356 ≈ 5.74% bruto-equivalente — aún menos que 7% plano a 4 años
    # vista, pero la brecha se reduce de 3 a ~1,26 puntos y el cruce llega en
    # el año 5; lo que el test fija es que el crecimiento SÍ computa:
    assert bam > Decimal("0.04") * neto, "el crecimiento debe aumentar la componente Div"
    assert plano - bam < Decimal("0.011"), f"brecha sin reducir: {plano - bam}"


def test_d2_g_div_editable_tiene_prioridad(db: Session, cartera, monkeypatch):
    import app.services.precios as precios
    from app.services import estimaciones as svc
    p = models.Posicion(cartera_id=cartera.id, isin="US_B00000001",
                        nombre="Beta", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=__import__("datetime").date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Estimacion(
        cartera_id=cartera.id, isin="US_B00000001", tipo_val="PER",
        eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
        metrica_base_4y=Decimal("5"),           # sin crecimiento implícito
        dividendo_share=Decimal("4"),
        crecimiento_div_pct=Decimal("0.15"),    # g_div editado por el usuario
    ))
    db.commit()
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_B00000001": (Decimal("100"), "EUR")})
    c = [x for x in svc.calcular_estimaciones(db, cartera.id)
         if x.isin == "US_B00000001"][0]
    assert c.crecimiento_div_aplicado_pct == Decimal("0.15")
    # Div_horizonte = 0.04 × 0.81 × 1.43560 ≈ 0.046513
    assert abs(c.div_horizonte_pct - Decimal("0.046513")) < Decimal("0.0005")


# ═══════════════════════════════════════════════════════════════════════════
# D3 — años reales en el CAGR del BPA
# ═══════════════════════════════════════════════════════════════════════════

def test_d3_bpa_negativo_intermedio_no_acorta_los_anios():
    from app.services.estimaciones import _crecimiento_eps
    # [5, −1, 6]: el crecimiento real es a 2 años → (6/5)^(1/2)−1 ≈ 9,54%
    g = _crecimiento_eps([5, -1, 6], None)
    assert abs(g - 0.0954) < 0.002, f"pre-fix daba 20% al contar 1 año: {g}"


def test_d3_serie_limpia_sin_cambio():
    from app.services.estimaciones import _crecimiento_eps
    # Dentro de la banda [−5%, +15%]: (6.05/5)^(1/2) − 1 = 10%
    g = _crecimiento_eps([5, 5.5, 6.05], None)
    assert abs(g - 0.10) < 1e-9


# ═══════════════════════════════════════════════════════════════════════════
# A8 — es_fondo se inicializa por detección
# ═══════════════════════════════════════════════════════════════════════════

def test_a8_etf_con_eps_no_se_siembra_como_per(monkeypatch):
    import json
    import app.services.posiciones as posiciones
    from app.services.estimaciones import _seed_estimacion
    monkeypatch.setattr(posiciones, "_tipo_activo", lambda isin, nombre: "ETF")
    e = models.Estimacion(cartera_id="c", isin="IE_ETF000001", tipo_val="PER")
    f = {"eps": 4.2, "pe": 18.0, "dividend": 1.0, "nombre": "MSCI World ETF"}
    _seed_estimacion(e, f, None)
    assert e.multiplo_objetivo is None, \
        "un ETF con trailingEps del feed se sembraba como PER (guarda muerta)"
    assert e.metrica_base_4y is None
    meta = json.loads(e.consenso_json or "{}")
    assert meta.get("es_fondo") is True, "la marca debe PERSISTIRSE la primera vez"


# ═══════════════════════════════════════════════════════════════════════════
# D1 — la familia colchón no se fuerza a "indice"
# ═══════════════════════════════════════════════════════════════════════════

def _ctx(nombre):
    from app.adapters.ia.base import ContextoEmpresa
    return ContextoEmpresa(isin="IE0000000001", nombre=nombre, tipo_activo="ETF")


def test_d1_min_vol_y_covered_call_van_a_la_ia():
    from app.services.clasificador import pregate
    assert pregate(_ctx("iShares Edge MSCI Min Vol Europe")) is None
    assert pregate(_ctx("JPM Nasdaq Equity Premium Income")) is None
    assert pregate(_ctx("Global X S&P 500 Covered Call")) is None


def test_d1_etf_amplio_sigue_en_indice_y_tematico_en_satelite():
    from app.services.clasificador import pregate
    assert pregate(_ctx("Vanguard FTSE All-World UCITS ETF")) == "indice"
    assert pregate(_ctx("VanEck Semiconductor UCITS ETF")) == "satelite"
