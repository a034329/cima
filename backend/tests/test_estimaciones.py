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
    # CAGR4+Div ≈ 0.2192
    assert abs(c.cagr4_div_pct - Decimal("0.2192")) < Decimal("0.001")
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
    assert per.multiplo_objetivo == Decimal("40.0000")     # 200/5 (sin histórico → consenso)
    assert per.metrica_base_4y == Decimal("10.0000")       # EPS consenso 4A
    assert per.consenso_json is not None                   # referencia guardada

    fcf = db.execute(
        select(models.Estimacion).where(models.Estimacion.isin == "US_FCF")
    ).scalars().first()
    assert fcf.multiplo_objetivo is None                   # guardarraíl: no PER
    assert fcf.metrica_base_4y is None
    assert fcf.consenso_json is not None                   # pero sí guarda referencia


def test_multiplo_normalizado_min_y_alerta(db: Session, cartera, monkeypatch) -> None:
    """El múltiplo por defecto = min(consenso forward, histórico mediano≥3 años),
    acotado [5,45]; si divergen >30% se marca alerta (posible re-rating)."""
    _pos(db, cartera, "US_RR", "ReRater", 10, 1000, 300)
    db.commit()
    import app.services.precios as precios
    # consenso forward = 360/10 = 36×; histórico mediano = 24× (n=5) → min = 24×
    cons = {"US_RR": {"precio_obj_consenso": 360.0, "eps_forward": 10.0,
                      "eps_consenso_4y": 15.0, "per_hist_mediano": 24.0, "per_hist_n": 5}}
    monkeypatch.setattr(precios, "consenso_por_isin", lambda db, cid: cons)
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid, *a, **k: {})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_RR": (Decimal("300"), "USD")})

    svc.prefill_estimaciones(db, cartera.id)
    e = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_RR")).scalars().first()
    assert e.multiplo_objetivo == Decimal("24.0000")       # min(36, 24)

    c = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_RR"][0]
    assert c.mult_alerta is not None and "re-rating" in c.mult_alerta   # 36 vs 24 → diverge 50%


def test_heuristica_defensiva_metrica_contable(db: Session, cartera, monkeypatch) -> None:
    """Industria de métrica contable (gestora/BDC/REIT) → el prefill NO siembra
    múltiplo/métrica como PER y marca para revisión. Un PER normal sí se siembra.
    Al confirmar el tipo_val, la marca se levanta y el prefill vuelve a sembrar."""
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
    assert am.multiplo_objetivo is None      # NO sembrado como PER
    assert am.metrica_base_4y is None
    assert am.dividendo_share == Decimal("1.500000")   # el dividendo sí (yield válido)
    cam = [x for x in svc.calcular_estimaciones(db, cartera.id) if x.isin == "US_AM"][0]
    assert cam.mult_alerta is not None and "tipo_val" in cam.mult_alerta

    tech = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_TECH")).scalars().first()
    assert tech.multiplo_objetivo is not None    # PER normal sí se siembra
    assert tech.metrica_base_4y == Decimal("8.0000")

    # Confirmar el tipo_val (incl. PER) levanta la marca y autoriza el sembrado.
    import json
    meta = json.loads(am.consenso_json)
    meta["tipo_confirmado"] = True
    meta.pop("revisar_tipo_val", None)
    am.consenso_json = json.dumps(meta)
    db.commit()
    svc.prefill_estimaciones(db, cartera.id)
    am2 = db.execute(select(models.Estimacion).where(models.Estimacion.isin == "US_AM")).scalars().first()
    assert am2.multiplo_objetivo is not None      # ya confirmado → siembra
    assert am2.metrica_base_4y == Decimal("3.0000")


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
    """Un ETF (sin BPA) toma su CAGR del histórico de precio + el yield actual."""
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
    assert c.cagr4_div_pct == Decimal("0.10")      # total retorno
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
