"""Test del cálculo de estimaciones (multi-método WG) — offline."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services import estimaciones as svc


def _pos(db, cartera, isin, nombre, qty, coste, precio_manual) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR",
                        precio_manual_eur=Decimal(str(precio_manual)))
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def test_cagr4_y_yield(db: Session, cartera, monkeypatch) -> None:
    p = _pos(db, cartera, "US_A", "Alpha", 10, 1000, 100)   # precio manual 100
    db.add(models.Estimacion(
        cartera_id=cartera.id, isin="US_A", tipo_val="PER",
        eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
        metrica_base_4y=Decimal("10"), dividendo_share=Decimal("3"),
    ))
    db.commit()
    # Precio nativo = precio manual (EUR) inyectado vía monkeypatch del helper.
    monkeypatch.setattr(svc, "calcular_estimaciones", svc.calcular_estimaciones)
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_A": (Decimal("100"), "EUR")})

    calcs = svc.calcular_estimaciones(db, cartera.id)
    c = [x for x in calcs if x.isin == "US_A"][0]
    # precio objetivo = 20 × 10 = 200
    assert c.precio_objetivo == Decimal("200")
    # CAGR4 = (200/100)^(1/4) - 1 ≈ 0.1892
    assert abs(c.cagr4_pct - Decimal("0.1892")) < Decimal("0.001")
    # yield = 3/100 = 0.03
    assert abs(c.div_yield_pct - Decimal("0.03")) < Decimal("0.0001")
    # BRUTO plano (reconciliación Excel): CAGR4 + yield ≈ 0.2192
    assert abs(c.cagr4_div_bruto_pct - Decimal("0.2192")) < Decimal("0.001")
    # MAESTRA = neto + crecimiento (decisión Angel 2026-06-11):
    #   US → tipo efectivo 19% (estatutaria 15% == tope CDI, sin exceso)
    #   g_div derivado del crecimiento implícito 5→10 en 4 años (18.92%, en
    #   banda) → factor medio de (1+g)^t t=1..4 ≈ 1.5713
    #   Div_horizonte ≈ 0.03 × 0.81 × 1.5713 ≈ 0.0382 → total ≈ 0.2274
    assert abs(c.tipo_efectivo_div_pct - Decimal("0.19")) < Decimal("0.0001")
    assert abs(c.cagr4_div_pct - Decimal("0.2274")) < Decimal("0.001")
    assert c.cagr4_div_pct != c.cagr4_div_bruto_pct
    # crecimiento = (10/5)^(1/4)-1 ≈ 0.1892
    assert abs(c.crecimiento_pct - Decimal("0.1892")) < Decimal("0.001")


def test_prefill_multiplo_consenso_y_guardarrail_no_per(db: Session, cartera, monkeypatch) -> None:
    """El prefill siembra multiplo = target_menor / EPS_forward y metrica_4A = EPS
    consenso, pero SOLO para tipo PER. P_FCF/P_BV/P_FRE quedan manuales."""
    _pos(db, cartera, "US_PER", "PerCo", 10, 1000, 100)
    cfcf = _pos(db, cartera, "US_FCF", "FcfCo", 10, 1000, 50)
    # FcfCo se valora por P_FCF → no debe autorrellenarse desde EPS.
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_FCF", tipo_val="P_FCF"))
    db.commit()

    import app.services.precios as precios
    cons = {
        "US_PER": {"precio_obj_consenso": 200.0, "eps_forward": 5.0, "eps_consenso_4y": 10.0,
                   "num_analistas_eps": 12, "anio_consenso_4y": 2030, "per_hist_medio": 18.0},
        "US_FCF": {"precio_obj_consenso": 300.0, "eps_forward": 2.0, "eps_consenso_4y": 3.0},
    }
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid, *a, **k: {})

    svc.prefill_estimaciones(db, cartera.id)

    per = db.execute(
        select(models.Estimacion).where(models.Estimacion.isin == "US_PER")
    ).scalars().first()
    assert per.multiplo_objetivo == Decimal("38.0000")     # implícito consenso 200/5=40 − 2 (conservador)
    assert per.metrica_base_4y == Decimal("10.0000")       # EPS consenso 4A
    assert per.consenso_json is not None                   # referencia guardada

    fcf = db.execute(
        select(models.Estimacion).where(models.Estimacion.isin == "US_FCF")
    ).scalars().first()
    assert fcf.multiplo_objetivo is None                   # guardarraíl: no PER
    assert fcf.metrica_base_4y is None
    assert fcf.consenso_json is not None                   # pero sí guarda referencia


def test_multiplo_implicito_consenso_menos_2_y_alerta(db: Session, cartera, monkeypatch) -> None:
    """El múltiplo objetivo = implícito del consenso (target÷EPS) − 2 puntos
    (conservador), NO el min con el histórico (que infravaloraba la calidad). Si
    consenso e histórico divergen >30% → alerta de re-rating."""
    _pos(db, cartera, "US_RR", "ReRater", 10, 1000, 300)
    db.commit()
    import app.services.precios as precios
    # implícito consenso = 360/10 = 36× → objetivo 36 − 2 = 34× (NO el histórico 24×)
    cons = {"US_RR": {"precio_obj_consenso": 360.0, "eps_forward": 10.0,
                      "eps_consenso_4y": 15.0, "per_hist_mediano": 24.0, "per_hist_n": 5}}
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid, *a, **k: {})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_RR": (Decimal("300"), "USD")})

    svc.prefill_estimaciones(db, cartera.id)
    e = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_RR")).scalars().first()
    assert e.multiplo_objetivo == Decimal("34.0000")       # 36 − 2, no el histórico 24

    c = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_RR"][0]
    assert c.mult_alerta is not None and "re-rating" in c.mult_alerta   # 36 vs 24 → diverge 50%


def test_heuristica_defensiva_metrica_contable(db: Session, cartera, monkeypatch) -> None:
    """ADR-005 2a: una gestora de activos se CLASIFICA como P_FRE (no PER), con
    múltiplo/métrica manuales y aviso; un software → PER sembrado normal."""
    _pos(db, cartera, "US_AM", "AltMgr", 10, 1000, 50)    # gestora alternativa (P/FRE real)
    _pos(db, cartera, "US_TECH", "TechCo", 10, 1000, 100)  # PER normal
    db.commit()

    import app.services.precios as precios
    funds = {
        "US_AM": {"eps": 2.0, "forward_eps": 2.2, "dividend": 1.5, "pe": 12.0,
                  "industry": "Asset Management"},
        "US_TECH": {"eps": 5.0, "forward_eps": 5.5, "dividend": None, "pe": 25.0,
                    "industry": "Software - Infrastructure"},
    }
    cons = {
        "US_AM": {"precio_obj_consenso": 60.0, "eps_forward": 2.2, "eps_consenso_4y": 3.0},
        "US_TECH": {"precio_obj_consenso": 150.0, "eps_forward": 5.5, "eps_consenso_4y": 8.0},
    }
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid, *a, **k: funds)
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_AM": (Decimal("50"), "USD"),
                                         "US_TECH": (Decimal("100"), "USD")})

    svc.prefill_estimaciones(db, cartera.id)

    am = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_AM")).scalars().first()
    assert am.tipo_val == "P_FRE"            # gestora → clasificada P_FRE
    assert am.multiplo_objetivo is None      # múltiplo/métrica manuales (2b)
    assert am.metrica_base_4y is None
    assert am.dividendo_share == Decimal("1.500000")   # el dividendo sí (yield válido)
    cam = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_AM"][0]
    assert cam.mult_alerta is not None and "clasificado" in cam.mult_alerta

    tech = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_TECH")).scalars().first()
    assert tech.tipo_val == "PER"
    assert tech.multiplo_objetivo is not None    # PER normal sí se siembra
    assert tech.metrica_base_4y == Decimal("8.0000")


def test_sin_inputs_no_calcula(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_B", "Beta", 10, 1000, 70)
    db.commit()
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_B": (Decimal("70"), "EUR")})
    calcs = svc.calcular_estimaciones(db, cartera.id)
    c = [x for x in calcs if x.isin == "US_B"][0]
    assert c.precio_objetivo is None
    assert c.cagr4_div_pct is None


def test_crecimiento_eps_serie_historico_mas_forward_acotado() -> None:
    """CAGR sobre [histórico + forward], acotado a banda; sin serie → 0%."""
    from app.services.estimaciones import _crecimiento_eps, _BANDA_CAGR_EPS
    lo, hi = _BANDA_CAGR_EPS
    # Otis-like: [2.96,3.39,4.07,3.50] + forward 4.72 → CAGR (4.72/2.96)^(1/4)-1 ≈ 12,4%
    g = _crecimiento_eps([2.96, 3.39, 4.07, 3.50], 4.72)
    assert abs(g - 0.124) < 0.01
    assert lo <= g <= hi
    # Crecimiento disparado → se capa al máximo de la banda.
    assert _crecimiento_eps([10.0], 30.0) == hi      # (30/10)^(1/1)-1 = 200% → cap
    # Sin serie utilizable → 0% (proyección plana).
    assert _crecimiento_eps([], None) == 0.0
    assert _crecimiento_eps([5.0], None) == 0.0
    # Caída fuerte → suelo de la banda.
    assert _crecimiento_eps([10.0], 2.0) == lo


def test_agregado_por_bloque_pondera_y_reporta_cobertura(db: Session, cartera, monkeypatch) -> None:
    """CAGR4+Div ponderado por valor, agrupado por bloque; la cobertura baja si
    alguna posición del bloque no tiene estimación."""
    from types import SimpleNamespace

    g = models.Bloque(cartera_id=cartera.id, nombre="Compounders", categoria_base="growth",
                       orden=0, es_base=True)
    i = models.Bloque(cartera_id=cartera.id, nombre="Income", categoria_base="income",
                      orden=1, es_base=True)
    db.add_all([g, i]); db.flush()
    a = _pos(db, cartera, "A", "Alpha", 10, 1000, 100); a.bloque_id = g.id   # con estimación
    b = _pos(db, cartera, "B", "Beta", 10, 1000, 100); b.bloque_id = g.id    # SIN estimación
    c = _pos(db, cartera, "C", "Gamma", 10, 1000, 100); c.bloque_id = i.id   # con estimación
    db.commit()

    import app.services.precios as precios
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid: ({"A": Decimal("100"), "B": Decimal("100"),
                                          "C": Decimal("100")}, []))
    monkeypatch.setattr(svc, "calcular_estimaciones", lambda db, cid: [
        SimpleNamespace(isin="A", cagr4_div_pct=Decimal("0.20"), div_yield_pct=None),
        SimpleNamespace(isin="C", cagr4_div_pct=Decimal("0.08"), div_yield_pct=None),
    ])  # B no aparece → sin estimación

    out = svc.agregado_por_bloque(db, cartera.id)
    assert out[g.id].cagr4_div_pct == Decimal("0.20")    # solo A pondera
    assert out[g.id].n_con_estimacion == 1
    assert out[g.id].cobertura == Decimal("0.5")         # A(1000) / [A+B = 2000]
    assert out[i.id].cagr4_div_pct == Decimal("0.08")
    assert out[i.id].cobertura == Decimal("1")


def test_etf_usa_cagr_historico_mas_yield(db: Session, cartera, monkeypatch) -> None:
    """Un ETF (sin BPA) toma su CAGR del histórico de precio + el yield NETO
    (la maestra es neta desde la decisión 2026-06-11; el bruto queda en
    cagr4_div_bruto_pct). ETF irlandés: sin retención en origen → tipo
    efectivo = suelo 19%."""
    _pos(db, cartera, "IE_ETF", "MSCI World ETF", 10, 1000, 100)   # sin Estimacion
    db.commit()
    import app.services.precios as precios
    import app.services.posiciones as posiciones
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"IE_ETF": (Decimal("100"), "EUR")})
    monkeypatch.setattr(posiciones, "_tipo_activo", lambda isin, nombre: "ETF")
    monkeypatch.setattr(precios, "cagr_historico_por_isin",
                        lambda db, cid, isines: {"IE_ETF": Decimal("0.08")})
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"IE_ETF": {"dividend": 2.0}})

    c = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "IE_ETF"][0]
    assert c.cagr4_pct == Decimal("0.08")          # CAGR de precio histórico
    assert c.div_yield_pct == Decimal("0.02")      # 2 € / 100 €
    assert c.cagr4_div_bruto_pct == Decimal("0.10")   # bruto plano (Excel)
    # Maestra neta: 0.08 + 0.02 × (1 − 0.19) = 0.0962 (g_div ETF = 0)
    assert c.cagr4_div_pct == Decimal("0.0962")
    assert "histórico" in (c.notas or "")


def test_stock_no_se_toca_por_el_enriquecido_etf(db: Session, cartera, monkeypatch) -> None:
    """Una acción normal con estimación válida NO se sobreescribe."""
    _pos(db, cartera, "US_A", "Alpha", 10, 1000, 100)
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_A", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
                            metrica_base_4y=Decimal("10")))
    db.commit()
    import app.services.precios as precios
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_A": (Decimal("100"), "EUR")})
    # Si se invocara el histórico para una acción, esto lo delataría:
    monkeypatch.setattr(precios, "cagr_historico_por_isin",
                        lambda db, cid, isines: {i: Decimal("9.99") for i in isines})

    c = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_A"][0]
    assert c.cagr4_div_pct is not None and c.cagr4_div_pct != Decimal("9.99")


def test_es_fondo_detecta_ucits_y_fallback_por_nombre() -> None:
    from app.services.estimaciones import _es_fondo
    assert _es_fondo("IE00B5BMR087", "ISHARES CORE S&P 500 UCITS ETF")        # classify_isin → ETF
    assert _es_fondo("IE00B5M1WJ87", "S&P Euro Dividend Aristocrats (Dist)")  # fallback ARISTOCRAT
    assert _es_fondo("IE000U9J8HX9", "Nasdaq Equity Premium Income Active")   # fallback EQUITY PREMIUM
    assert _es_fondo("IE00B86MWN23", "Edge MSCI Europe Min Volatility")       # fallback MIN VOLATILITY
    assert not _es_fondo("US5949181045", "MICROSOFT CORP")


def test_seed_no_siembra_per_en_fondos() -> None:
    import json
    from app.db import models
    from app.services.estimaciones import _seed_estimacion
    e = models.Estimacion(cartera_id="x", isin="IE00B5M1WJ87", tipo_val="PER",
                          consenso_json=json.dumps({"es_fondo": True}))
    _seed_estimacion(e, {"eps": 5, "pe": 20, "dividend": 1.2, "forward_eps": 6},
                     {"eps_consenso_4y": 7})
    assert e.multiplo_objetivo is None and e.metrica_base_4y is None   # no se siembra PER en un fondo
    assert e.dividendo_share is not None                               # el dividendo sí (ETF de reparto)
    assert json.loads(e.consenso_json).get("es_fondo") is True         # el flag persiste


def test_tipo_sotp_valora_por_nav() -> None:
    from decimal import Decimal
    from app.db import models
    from app.services.estimaciones import _calc_item
    assert "SOTP" in models.TIPOS_VAL
    assert models.etiquetas_tipo_val("SOTP")[0] == "P/NAV"
    e = models.Estimacion(cartera_id="x", isin="KYG217651051", tipo_val="SOTP",
                          multiplo_objetivo=Decimal("0.68"), metrica_base_4y=Decimal("115"),
                          dividendo_share=Decimal("2.31"))
    calc = _calc_item("KYG217651051", "CK Hutchison", e, Decimal("67.60"), "HKD")
    assert calc.tipo_val == "SOTP"
    assert calc.precio_objetivo == Decimal("78.20")          # P/NAV 0,68 × NAV/acc 115
    assert calc.cagr4_pct is not None and calc.cagr4_pct > 0  # 78,2 > 67,6


def test_precios_nativos_lee_cache_no_refresca_por_defecto(db: Session, cartera, monkeypatch) -> None:
    """Editar/recalcular NO debe disparar fetch de mercado: precios_nativos por
    defecto solo lee caché; `refrescar=True` (prefill) sí repuebla en vivo."""
    import app.services.precios as precios
    _pos(db, cartera, "US_Z", "Zeta", 10, 1000, 100)
    db.commit()
    llamado = {"n": 0}
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: (llamado.__setitem__("n", llamado["n"] + 1), ({}, []))[1])
    precios.precios_nativos(db, cartera.id)                  # lectura → solo caché
    assert llamado["n"] == 0
    precios.precios_nativos(db, cartera.id, refrescar=True)   # refresco explícito
    assert llamado["n"] == 1


def test_no_refetch_de_precio_cacheado_aunque_este_viejo(monkeypatch) -> None:
    """Política de caché de APIs financieras: si ya tenemos el dato cacheado NO se
    vuelve a pedir por antigüedad (solo `refrescar=True` o si falta)."""
    import app.services.precios as precios
    cache = {"px:AAA": {"precio": 50.0, "divisa": "EUR", "ts": 0}}   # ts=0 → muy viejo
    monkeypatch.setattr(precios, "_leer_cache", lambda: dict(cache))
    monkeypatch.setattr(precios, "_guardar_cache", lambda c: None)
    n = {"fetch": 0}
    def fake(sim):
        n["fetch"] += 1
        return (99.0, "EUR")
    monkeypatch.setattr(precios, "_precio_y_divisa", fake)

    r = precios.precio_nativo_simbolo("AAA")                 # lectura → caché, sin red
    assert r == (Decimal("50.0"), "EUR") and n["fetch"] == 0
    precios.precio_nativo_simbolo("AAA", refrescar=True)      # refresco explícito → sí
    assert n["fetch"] == 1


def test_precio_via_ia_fallback(monkeypatch) -> None:
    """Si Yahoo y FMP no dan precio, _precio_y_divisa cae a la IA (web)."""
    import app.services.precios as precios
    import app.adapters.ia as ia_mod
    # Yahoo "falla" (no precio) y FMP también (None)
    monkeypatch.setattr(precios, "_precio_fmp_us", lambda sim: None)
    class _IA:
        def investigar(self, system, user, timeout_s=None):
            return '{"precio": 67.6, "divisa": "HKD"}'
    monkeypatch.setattr(ia_mod, "get_clasificador", lambda *a, **k: _IA())
    # forzar el camino IA: yfinance puede no estar disponible o devolver vacío en test
    monkeypatch.setattr(precios, "_precio_via_ia",
                        lambda sim: (67.6, "HKD"))      # también probamos la firma directa
    assert precios._precio_via_ia("0001.HK") == (67.6, "HKD")
    # y, sin la patch directa, integra: el fallback se invoca desde _precio_y_divisa
    monkeypatch.undo()
    monkeypatch.setattr(precios, "_precio_fmp_us", lambda sim: None)
    monkeypatch.setattr(ia_mod, "get_clasificador", lambda *a, **k: _IA())
    # neutraliza yfinance para que el camino "try yfinance" no atrape
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "yfinance": raise ImportError
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Por defecto NO usa IA (rápido, lecturas): si Yahoo/FMP no dan, devuelve None.
    assert precios._precio_y_divisa("0001.HK") is None
    # `usar_ia=True` (refresco explícito): sí cae a la IA.
    assert precios._precio_y_divisa("0001.HK", usar_ia=True) == (67.6, "HKD")


def test_no_per_eps_no_contamina_dividendo():
    """Bug Blue Owl/OWL (P/FRE): `eps_actual` (EPS real) NO debe contaminar el
    crecimiento ni el dividendo del horizonte cuando la métrica base es FRE/NAV.
    Dos filas P_FRE idénticas salvo `eps_actual` deben dar el MISMO cagr4_div, y
    `crecimiento_pct` debe ser None (no se mezcla FRE con EPS)."""
    comun = dict(
        cartera_id="x", isin="US_OWL", tipo_val="P_FRE",
        multiplo_objetivo=Decimal("15"), metrica_base_4y=Decimal("1.10"),  # FRE/share 4Y
        dividendo_share=Decimal("0.72"),
        # crecimiento_div_pct NO fijado → antes caía al crec basura (FRE/EPS)
    )
    sin_eps = models.Estimacion(**comun)
    con_eps = models.Estimacion(**comun, eps_actual=Decimal("0.60"))  # EPS real, otra familia

    precio = Decimal("18")
    a = svc._calc_item("US_OWL", "Blue Owl", sin_eps, precio, "USD")
    b = svc._calc_item("US_OWL", "Blue Owl", con_eps, precio, "USD")

    assert a.crecimiento_pct is None and b.crecimiento_pct is None
    assert a.cagr4_div_pct == b.cagr4_div_pct      # eps_actual ya no afecta
    assert a.div_yield_pct == b.div_yield_pct       # yield = div/precio, intacto


def test_g_div_se_clampa_siempre():
    """6A: un g_div editado fuera de banda (15 = 1500%) NO debe disparar el
    factor del horizonte; se clampa a +20% como el derivado."""
    base = dict(cartera_id="x", isin="US_D", tipo_val="PER",
                multiplo_objetivo=Decimal("20"), metrica_base_4y=Decimal("12"),
                eps_actual=Decimal("8"), dividendo_share=Decimal("2"))
    loco = models.Estimacion(**base, crecimiento_div_pct=Decimal("15"))     # 1500%
    tope = models.Estimacion(**base, crecimiento_div_pct=Decimal("0.20"))   # +20%
    a = svc._calc_item("US_D", "D", loco, Decimal("100"), "USD")
    b = svc._calc_item("US_D", "D", tope, Decimal("100"), "USD")
    assert a.cagr4_div_pct == b.cagr4_div_pct      # ambos clampados a +20%


def test_clasificador_tipo_multiplo():
    """ADR-005 2a: el seed CLASIFICA el tipo de múltiplo (no solo marca) y no
    siembra PER sobre financieras. Banco/REIT → P_BV; gestora → P_FRE;
    corredor de seguros → PER; EPS<0 con FCF>0 → P_FCF."""
    import json as _json
    banco = models.Estimacion(cartera_id="x", isin="US_BANK", tipo_val="PER")
    svc._seed_estimacion(banco, {"industry": "Banks - Regional", "eps": 3.0,
                                 "forward_eps": 3.3, "pe": 11}, None)
    assert banco.tipo_val == "P_BV"            # clasificado, no PER
    assert banco.multiplo_objetivo is None     # no se siembra múltiplo PER
    assert _json.loads(banco.consenso_json).get("tipo_clasificado")

    # Corredor de seguros (Marsh/AON): SÍ se valora por PER pese a "insurance".
    brk = models.Estimacion(cartera_id="x", isin="US_BRK", tipo_val="PER")
    svc._seed_estimacion(brk, {"industry": "Insurance Brokers", "eps": 5.0,
                               "forward_eps": 5.5, "pe": 20}, None)
    assert brk.tipo_val == "PER" and brk.multiplo_objetivo is not None

    reit = models.Estimacion(cartera_id="x", isin="ES_REIT", tipo_val="PER")
    svc._seed_estimacion(reit, {"industry": "", "sector": "Real Estate",
                                "eps": 1.0, "pe": 18}, None)
    assert reit.tipo_val == "P_BV"

    owl = models.Estimacion(cartera_id="x", isin="US_OWL2", tipo_val="PER")
    svc._seed_estimacion(owl, {"industry": "Asset Management", "eps": 0.6, "pe": 25}, None)
    assert owl.tipo_val == "P_FRE" and owl.metrica_base_4y is None   # múltiplo/métrica manuales (2b)

    fcf = models.Estimacion(cartera_id="x", isin="US_FCF2", tipo_val="PER")
    svc._seed_estimacion(fcf, {"industry": "Software - Application", "eps": -1.0,
                               "fcf_ps": 3.5}, None)
    assert fcf.tipo_val == "P_FCF" and fcf.metrica_base_4y == Decimal("3.5000")

    # Un tipo no-PER ya elegido NO se degrada a PER (aunque no esté confirmado).
    manual = models.Estimacion(cartera_id="x", isin="US_M", tipo_val="P_FCF")
    svc._seed_estimacion(manual, {"industry": "Software - Application", "eps": 5.0, "pe": 20}, None)
    assert manual.tipo_val == "P_FCF"


def test_precios_nativos_respeta_precio_manual(db, cartera):
    from app.services.precios import precios_nativos
    _pos(db, cartera, "ES_MANUAL", "ManualCo", 10, 1000, 12.5)   # precio_manual_eur=12.5
    db.commit()
    out = precios_nativos(db, cartera.id)
    assert "ES_MANUAL" in out          # ya no desaparece sin px cacheado
    px, div = out["ES_MANUAL"]
    assert div == "EUR" and px == Decimal("12.5")


def test_metrica_divisa_reconcilia_misma_familia_gbp():
    """Métrica en GBP (libras) y precio en GBp (peniques) → se reescala ×100 y
    da el MISMO CAGR/yield que si todo estuviera en peniques (sin FX)."""
    a = svc._calc_item("UK_A", "A", models.Estimacion(
        cartera_id="x", isin="UK_A", tipo_val="PER", multiplo_objetivo=Decimal("15"),
        metrica_base_4y=Decimal("100"), metrica_divisa="GBp", dividendo_share=Decimal("40"),
    ), Decimal("1200"), "GBp")
    b = svc._calc_item("UK_B", "B", models.Estimacion(
        cartera_id="x", isin="UK_B", tipo_val="PER", multiplo_objetivo=Decimal("15"),
        metrica_base_4y=Decimal("1.00"), metrica_divisa="GBP", dividendo_share=Decimal("0.40"),
    ), Decimal("1200"), "GBp")
    assert a.cagr4_pct == b.cagr4_pct
    assert a.div_yield_pct == b.div_yield_pct


def test_metrica_divisa_cross_currency_no_inventa_cagr():
    """Métrica en DKK y precio en USD (ADR, caso NVO) → no reconciliable sin FX:
    no se calcula CAGR/yield y se avisa, en vez de dar un número falso."""
    c = svc._calc_item("DK_NVO", "Novo", models.Estimacion(
        cartera_id="x", isin="DK0060534915", tipo_val="PER", multiplo_objetivo=Decimal("20"),
        metrica_base_4y=Decimal("5"), metrica_divisa="DKK", dividendo_share=Decimal("1"),
    ), Decimal("80"), "USD")
    assert c.cagr4_pct is None and c.cagr4_div_pct is None and c.div_yield_pct is None
    assert c.mult_alerta and "divisa" in c.mult_alerta


def test_prefill_reseed_respeta_editado_y_refresca_auto():
    """3D: el prefill RE-SIEMBRA los campos auto con dato fresco pero NUNCA pisa
    los que el usuario editó (registrados en consenso_json['editado'])."""
    import json as _json
    e = models.Estimacion(cartera_id="x", isin="US_R", tipo_val="PER")
    svc._seed_estimacion(e, {"industry": "Software", "eps": 2.0, "forward_eps": 2.2,
                             "pe": 20, "dividend": 1.0, "currency": "USD"}, None)
    assert e.multiplo_objetivo is not None and e.dividendo_share == Decimal("1.0")

    # Usuario edita el múltiplo a mano → se marca como editado.
    e.multiplo_objetivo = Decimal("30")
    meta = _json.loads(e.consenso_json)
    meta["editado"] = ["multiplo_objetivo"]
    e.consenso_json = _json.dumps(meta)

    # Segundo prefill con datos frescos DISTINTOS.
    svc._seed_estimacion(e, {"industry": "Software", "eps": 3.0, "forward_eps": 3.3,
                             "pe": 15, "dividend": 2.0, "currency": "USD"}, None)
    assert e.multiplo_objetivo == Decimal("30")     # editado → preservado
    assert e.dividendo_share == Decimal("2.0")      # auto → refrescado
    assert e.eps_actual == Decimal("3.0")           # auto → refrescado


def test_horizonte_cagr_se_acorta_si_consenso_cerca():
    """3C: si el año objetivo del consenso está más cerca, el CAGR se anualiza
    sobre menos años (mayor), en vez de sobre 4 fijos."""
    import datetime as _dt, json as _json
    y = _dt.date.today().year
    def mk(years_out):
        return models.Estimacion(
            cartera_id="x", isin="US_H", tipo_val="PER",
            multiplo_objetivo=Decimal("6.5"), metrica_base_4y=Decimal("20"),  # precio_obj=130 (CAGR modesto)
            consenso_json=_json.dumps({"anio_consenso_4y": y + years_out}))
    cerca = svc._calc_item("US_H", "H", mk(2), Decimal("100"), "USD")
    lejos = svc._calc_item("US_H", "H", mk(4), Decimal("100"), "USD")
    assert cerca.cagr4_pct is not None and lejos.cagr4_pct is not None
    assert cerca.cagr4_pct > lejos.cagr4_pct      # 2 años anualiza más alto que 4


def test_consenso_caducado_avisa():
    import datetime as _dt, json as _json
    y = _dt.date.today().year
    e = models.Estimacion(cartera_id="x", isin="US_C", tipo_val="PER",
                          multiplo_objetivo=Decimal("6.5"), metrica_base_4y=Decimal("20"),  # precio_obj=130
                          consenso_json=_json.dumps({"anio_consenso_4y": y}))  # objetivo = este año
    c = svc._calc_item("US_C", "C", e, Decimal("100"), "USD")
    assert c.mult_alerta and "caducado" in c.mult_alerta


def test_2b_objetivo_no_per_metrica_proyectada_y_multiplo_calidad():
    """Fase 2b: P_BV proyecta el valor contable 4Y (ROE×retención) y fija el
    múltiplo objetivo = P/B actual × factor de calidad (acotado). Un negocio de
    alta calidad (ROE/márgenes/crecimiento altos) recibe prima sobre el actual."""
    e = models.Estimacion(cartera_id="x", isin="US_BK2", tipo_val="PER")
    svc._seed_estimacion(e, {
        "industry": "Banks - Diversified", "eps": 5.0,
        "book_value_ps": 40.0, "price_to_book": 1.5, "payout": 0.4,
        "roe": 0.22, "oper_margin": 0.40, "revenue_growth": 0.10,
    }, None)
    assert e.tipo_val == "P_BV"
    # crecimiento sostenible = 0.22 × (1−0.4) = 0.132 → cap 0.12 → BV 4Y = 40×1.12^4
    esperado_bv = Decimal(str(40.0 * (1.12) ** 4)).quantize(Decimal("0.0001"))
    assert e.metrica_base_4y == esperado_bv
    # factor calidad: ROE>0.20(+0.06) + oper>0.25(+0.05) + growth>0.06(+0.03) = 1.14
    assert e.multiplo_objetivo == (Decimal("1.5") * Decimal("1.14")).quantize(Decimal("0.0001"))


def test_2b_factor_calidad_neutro_sin_senales():
    e = models.Estimacion(cartera_id="x", isin="US_BK3", tipo_val="P_BV")
    svc._seed_estimacion(e, {"book_value_ps": 10.0, "price_to_book": 1.0}, None)
    # Sin ROE/márgenes/crecimiento → factor 1.0 → objetivo = P/B actual
    assert e.multiplo_objetivo == Decimal("1.0000")


def test_2b_refinar_multiplo_por_pares(db: Session, cartera):
    """2b-pares: el múltiplo objetivo se fija = mediana de pares × calidad
    relativa; respeta ediciones; marca el origen para que el prefill no lo pise."""
    import json as _json
    from app.services.comps import Comps, Peer
    from app.services import estimaciones as svc

    _pos(db, cartera, "US_BKP", "BankPeer", 10, 1000, 50)
    e = models.Estimacion(cartera_id=cartera.id, isin="US_BKP", tipo_val="P_BV",
                          metrica_base_4y=Decimal("60"),
                          consenso_json=_json.dumps({"calidad": {"roe": 0.25, "revenue_growth": 0.10}}))
    db.add(e); db.commit()

    comps = Comps(isin="US_BKP", nombre="BankPeer", sector="Banks", peers=[
        Peer(nombre="BankPeer", ticker="BKP", per=None, ev_ebitda=None, p_fcf=None,
             yield_pct=None, crecimiento_pct=0.10, roic_pct=0.25, es_objetivo=True, p_bv=1.5),
        Peer(nombre="PeerA", ticker="A", per=None, ev_ebitda=None, p_fcf=None,
             yield_pct=None, crecimiento_pct=0.05, roic_pct=0.12, es_objetivo=False, p_bv=1.0),
        Peer(nombre="PeerB", ticker="B", per=None, ev_ebitda=None, p_fcf=None,
             yield_pct=None, crecimiento_pct=0.06, roic_pct=0.14, es_objetivo=False, p_bv=1.2),
    ])
    ok = svc.refinar_multiplo_por_pares(db, cartera.id, "US_BKP", comps)
    assert ok
    db.refresh(e)
    # mediana pares (excl. objetivo) P/B = mediana(1.0, 1.2) = 1.1; ROE 0.25 > mediana
    # peer ROIC 0.13×1.1 → +0.08; growth 0.10 > 0.055×1.1 → +0.06 → factor 1.14
    assert e.multiplo_objetivo == (Decimal("1.1") * Decimal("1.14")).quantize(Decimal("0.0001"))
    assert _json.loads(e.consenso_json).get("multiplo_pares") is True

    # Una edición del usuario tiene prioridad: no se refina.
    e.consenso_json = _json.dumps({"editado": ["multiplo_objetivo"]})
    e.multiplo_objetivo = Decimal("2.0")
    db.commit()
    assert svc.refinar_multiplo_por_pares(db, cartera.id, "US_BKP", comps) is False
    db.refresh(e)
    assert e.multiplo_objetivo == Decimal("2.0")


def test_guarda_cordura_per_implicito_absurdo():
    """Guarda de cordura: EPS en otra divisa/unidad (caso Novo: EPS DKK ~18.76
    contra precio USD 43.52 → PER implícito ~2×) → NO se calcula CAGR y se avisa,
    en vez de soltar un 54% falso."""
    e = models.Estimacion(cartera_id="x", isin="DK_NOVO", tipo_val="PER",
                          eps_actual=Decimal("23.03"), multiplo_objetivo=Decimal("13.07"),
                          metrica_base_4y=Decimal("18.76"), metrica_divisa="USD")
    c = svc._calc_item("DK_NOVO", "Novo", e, Decimal("43.52"), "USD")
    assert c.cagr4_pct is None and c.cagr4_div_pct is None
    assert c.mult_alerta and "incoherente" in c.mult_alerta


def test_seed_metrica_divisa_es_la_del_precio():
    """metrica_divisa = la del PRECIO (los fundamentales ya vienen escalados a esa
    unidad). NO se usa financial_currency: para LSE rompía el dividendo (Diageo
    reporta en USD pero el dividendo va en peniques → ×78 → 324% yield)."""
    e = models.Estimacion(cartera_id="x", isin="GB_DGE", tipo_val="PER")
    svc._seed_estimacion(e, {"industry": "Beverages", "eps": 105.0,
                             "currency": "GBp", "financial_currency": "USD"}, None)
    assert e.metrica_divisa == "GBp"


def test_guarda_cordura_yield_absurdo():
    """Un dividendo en unidad/divisa equivocada (yield > 30%) → se suprime el yield
    y se avisa, en vez de mostrar un 324% (caso Diageo)."""
    e = models.Estimacion(cartera_id="x", isin="GB_X", tipo_val="PER",
                          eps_actual=Decimal("105"), multiplo_objetivo=Decimal("15"),
                          metrica_base_4y=Decimal("110"), dividendo_share=Decimal("4900"),
                          metrica_divisa="GBp")
    c = svc._calc_item("GB_X", "X", e, Decimal("1500"), "GBp")
    assert c.div_yield_pct is None
    assert c.mult_alerta and "yield" in c.mult_alerta


def test_eps_trailing_deprimido_se_normaliza():
    """EPS trailing atípicamente bajo (Carrefour: 0.47 vs histórico ~1.3-1.8) →
    se usa la mediana reciente, no el crudo; CAGR deja de ser disparatado."""
    e = models.Estimacion(cartera_id="x", isin="FR_CARR", tipo_val="PER")
    svc._seed_estimacion(e, {
        "industry": "Grocery Stores", "eps": 0.47, "forward_eps": 1.6,
        "eps_hist": [1.3, 1.5, 1.7, 0.47], "pe": 9.0,
    }, None)
    # base normalizada = mediana de [1.5, 1.7, 0.47(desc?), 1.6 fwd]… > 1.0, no 0.47
    assert float(e.eps_actual) > 1.0


def test_eps_negativo_puntual_no_va_a_p_fcf():
    """Kraft Heinz con EPS trailing negativo (impairment) pero histórico positivo
    → se normaliza a positivo y se clasifica PER, no P_FCF."""
    e = models.Estimacion(cartera_id="x", isin="US_KHC", tipo_val="PER")
    svc._seed_estimacion(e, {
        "industry": "Packaged Foods", "eps": -4.86, "forward_eps": 3.0,
        "eps_hist": [2.6, 2.8, 3.0, -4.86], "fcf_ps": 3.0, "pe": 8.0,
    }, None)
    assert e.tipo_val == "PER" and float(e.eps_actual) > 0


def test_3m_conglomerado_es_per_no_sotp():
    e = models.Estimacion(cartera_id="x", isin="US_MMM", tipo_val="PER")
    svc._seed_estimacion(e, {"industry": "Conglomerates", "eps": 6.0,
                             "forward_eps": 6.5, "pe": 18.0}, None)
    assert e.tipo_val == "PER"        # operativa → PER, no suma de partes


def test_tipo_ia_es_primario_y_corrige_el_determinista():
    """La clasificación IA manda: un BDC que el fallback determinista mandaría a
    P_FRE (industria 'Asset Management') se fija a P_BV si la IA lo dice — y el
    override actúa desde CUALQUIER tipo previo, no solo desde PER."""
    e1 = models.Estimacion(cartera_id="x", isin="US_OBDC", tipo_val="PER")
    svc._seed_estimacion(e1, {"industry": "Asset Management", "eps": 1.2}, None)
    assert e1.tipo_val == "P_FRE"            # fallback determinista (imperfecto)
    e2 = models.Estimacion(cartera_id="x", isin="US_OBDC", tipo_val="P_FRE")
    svc._seed_estimacion(e2, {"industry": "Asset Management", "eps": 1.2,
                              "book_value_ps": 15.0, "price_to_book": 1.0},
                         None, tipo_ia=("P_BV", "BDC cotiza sobre NAV"))
    assert e2.tipo_val == "P_BV"             # IA corrige, desde un no-PER


def test_clasificar_tipos_ia_parsea_lote(monkeypatch):
    """clasificar_tipos_ia: una llamada para varias empresas; parseo robusto;
    descarta tipos inválidos; {} si no hay candidatos."""
    from app.adapters.ia import get_clasificador
    fake = ('Aquí: {"US1": {"tipo": "P_BV", "razon": "banco"}, '
            '"US2": {"tipo": "PER", "razon": "operativa"}, '
            '"US3": {"tipo": "XXX", "razon": "inválido"}}')
    monkeypatch.setattr(type(get_clasificador()), "completar",
                        lambda self, s, u, **k: fake, raising=False)
    out = svc.clasificar_tipos_ia([{"isin": "US1"}, {"isin": "US2"}, {"isin": "US3"}])
    assert out["US1"][0] == "P_BV" and out["US2"][0] == "PER"
    assert "US3" not in out                  # tipo inválido descartado
    assert svc.clasificar_tipos_ia([]) == {}


def test_multiplo_desde_target_yfinance_sin_fmp():
    """Sin consenso FMP, el múltiplo se deriva del target medio de YFINANCE
    (targetMeanPrice ÷ forwardEps) − 2, en vez de caer al forwardPE crudo."""
    e = models.Estimacion(cartera_id="x", isin="US_QC", tipo_val="PER")
    svc._seed_estimacion(e, {
        "industry": "Internet Content & Information", "eps": 22.0,
        "forward_eps": 25.0, "target_mean": 700.0, "pe": 15.0,
        "eps_hist": [14, 18, 22],
    }, None)   # c=None → sin FMP
    # implícito = 700/25 = 28 → −2 = 26 (no el forwardPE 15)
    assert e.multiplo_objetivo == Decimal("26.0000")
