"""Feed de precios actuales por ISIN, con conversión a EUR y caché en disco.

Pipeline (no requiere que las posiciones tengan ticker — resuelve por ISIN):
  1. OpenFIGI (gratis, sin key a bajo volumen) ISIN → (ticker, exchCode).
  2. exchCode → sufijo de mercado yfinance (.L, .PA, .DE, .MC…).
  3. yfinance `history` → último cierre + divisa REAL (history_metadata),
     evitando asumir GBX en LSE (hay líneas en USD como JEPQ.L).
  4. Conversión a EUR (GBp/GBX → ×0.01; resto vía pares EUR{CCY}=X).

Caché en disco: FIGI (TTL largo, los ISIN no cambian) + precios/FX (TTL 6h).
Best-effort: ISIN que no resuelven (cripto, mercados raros) → `no_resueltos`.
"""
from __future__ import annotations

import json
import time
import warnings
from decimal import Decimal
from pathlib import Path

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models
from app.services.fifo import estado_posicion


_CACHE_PATH = Path(__file__).resolve().parents[2] / "precios_cache.json"
_TTL_PX = 6 * 3600          # precios y FX
_TTL_FIGI = 30 * 24 * 3600  # resolución ISIN→ticker (estable)
_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# OpenFIGI exchCode → sufijo de mercado yfinance. La DIVISA NO se infiere aquí
# (la da yfinance), solo el sufijo del símbolo.
_EXCH_SUFIJO = {
    "US": "", "UN": "", "UW": "", "UQ": "", "UA": "", "UR": "", "UP": "", "UV": "",
    "UC": "", "UD": "", "UF": "", "UI": "", "UL": "", "UM": "", "UO": "", "UU": "",
    "LN": ".L", "FP": ".PA", "GR": ".DE", "GY": ".DE", "GF": ".DE", "GS": ".DE",
    "SM": ".MC", "NA": ".AS", "IM": ".MI", "SW": ".SW", "VX": ".SW",
    "DC": ".CO", "SS": ".ST", "NO": ".OL", "FH": ".HE", "PW": ".WA",
    "CT": ".TO", "CN": ".TO", "HK": ".HK", "PL": ".LS",
}

# País del ISIN → exchCodes preferidos (cotización primaria). OpenFIGI devuelve
# cientos de matches y `data[0]` suele ser un mercado secundario (AMZN→Perú,
# Novo→Suiza); preferimos el mercado nativo del emisor.
_PAIS_EXCH = {
    "US": ["US"], "FR": ["FP"], "GB": ["LN"], "DE": ["GR", "GY", "GF", "GS"],
    "ES": ["SM"], "NL": ["NA"], "CH": ["SW", "VX"], "DK": ["DC"], "IT": ["IM"],
    "SE": ["SS"], "FI": ["FH"], "PL": ["PW"], "CA": ["CN", "CT"],
    "IE": ["LN", "US"], "KY": ["HK", "US"], "LU": ["LN", "GR"], "PT": ["PL"],
}

# Override directo ISIN → símbolo yfinance, con prioridad sobre OpenFIGI, para
# casos que no resuelve o resuelve a un mercado raro.
_ISIN_OVERRIDE: dict[str, str] = {
    "DK0062498333": "NVO",        # Novo Nordisk (ADR US, USD)
    "DE0005933972": "EXS2.DE",    # iShares TecDAX (Xetra)
    "KYG217651051": "0001.HK",    # CK Hutchison (HKD)
    "XF000BTC0017": "BTC-EUR",    # Bitcoin
    "XF000SOL0012": "SOL-EUR",    # Solana
}


def _leer_cache() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar_cache(cache: dict) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError:
        pass


def _fresco(entry: dict, ttl: int) -> bool:
    return bool(entry) and (time.time() - entry.get("ts", 0)) < ttl


def timestamp_precios() -> str | None:
    """ISO del precio cacheado más reciente (cuándo se obtuvieron los precios)."""
    cache = _leer_cache()
    ts = [e.get("ts", 0) for k, e in cache.items()
          if k.startswith("px:") and isinstance(e, dict)]
    if not ts:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(max(ts), tz=timezone.utc).isoformat()


# ── 1. OpenFIGI: ISIN → (ticker, exchCode) ─────────────────────────────────

def _mejor_match(isin: str, data: list[dict]) -> dict | None:
    """Elige el match de OpenFIGI más representativo: prioriza el mercado nativo
    del país del ISIN; si no, el primero con exchCode mapeable a yfinance."""
    if not data:
        return None
    equities = [m for m in data if m.get("marketSector") == "Equity"] or data
    for ex in _PAIS_EXCH.get(isin[:2].upper(), []):
        for m in equities:
            if m.get("exchCode") == ex:
                return m
    for m in equities:
        if m.get("exchCode") in _EXCH_SUFIJO:
            return m
    return equities[0]


def _resolver_figi(isines: list[str], cache: dict) -> None:
    """Rellena cache['figi:<isin>'] = {ticker, exch, ts} para los no frescos."""
    pendientes = [i for i in isines if not _fresco(cache.get(f"figi:{i}", {}), _TTL_FIGI)]
    for lote_inicio in range(0, len(pendientes), 10):   # sin key: 10 jobs/req
        lote = pendientes[lote_inicio:lote_inicio + 10]
        payload = [{"idType": "ID_ISIN", "idValue": i} for i in lote]
        try:
            r = requests.post(
                _OPENFIGI_URL, json=payload,
                headers={"Content-Type": "application/json"}, timeout=15,
            )
            if r.status_code != 200:
                continue
            for isin, res in zip(lote, r.json()):
                d = (res or {}).get("data") or []
                m = _mejor_match(isin, d)
                cache[f"figi:{isin}"] = {
                    "ticker": m.get("ticker") if m else None,
                    "exch": m.get("exchCode") if m else None,
                    "ts": time.time(),
                }
        except requests.RequestException:
            continue
        time.sleep(0.3)   # cortesía con el rate limit (25/min sin key)


def _yf_simbolo(ticker: str | None, exch: str | None) -> str | None:
    if not ticker or exch is None:
        return None
    suf = _EXCH_SUFIJO.get(exch)
    if suf is None:
        return None   # mercado no soportado
    return ticker.replace(" ", "-") + suf


# ── 2/3. yfinance: precio + divisa real ────────────────────────────────────

# Clave FMP vía entorno (CIMA_FMP_API_KEY). Solo resuelve símbolos US en el plan
# actual. Sin clave → el feed cae a yfinance (sin consenso de analistas).
_FMP_QUOTE = "https://financialmodelingprep.com/stable/quote"


def _precio_fmp_us(simbolo: str) -> tuple[float, str] | None:
    """Fallback US: FMP /stable/quote (solo símbolos sin sufijo de mercado)."""
    if "." in simbolo:
        return None   # FMP plan actual solo resuelve US
    try:
        r = requests.get(_FMP_QUOTE, params={"symbol": simbolo, "apikey": settings.fmp_api_key}, timeout=12)
        if r.status_code != 200:
            return None
        q = r.json()
        if q and q[0].get("price"):
            return float(q[0]["price"]), "USD"
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None
    return None


def _precio_y_divisa(simbolo: str) -> tuple[float, str] | None:
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = yf.Ticker(simbolo)
            serie = t.history(period="5d")["Close"].dropna()
            if len(serie):
                cur = (t.history_metadata or {}).get("currency") or "USD"
                return float(serie.iloc[-1]), cur
    except Exception:
        pass
    return _precio_fmp_us(simbolo)   # fallback determinista (US)


# ── 4. FX a EUR ─────────────────────────────────────────────────────────────

def _fx_eur(divisa: str, cache: dict) -> Decimal | None:
    """Factor para multiplicar un precio en `divisa` y obtener EUR.

    OJO con Reino Unido: yfinance distingue por CAJA — "GBp" = peniques (×0.01),
    "GBP" = libras (×1). NO hacer .upper() antes de decidir la escala, o las
    libras se tratan como peniques (÷100 → pérdida falsa del 99%)."""
    raw = (divisa or "EUR")
    div = raw.upper()
    if div == "EUR":
        return Decimal("1")
    es_pence = raw == "GBp" or div in ("GBX", "GBP_PENCE")
    if es_pence:
        base, escala = "GBP", Decimal("0.01")
    elif div == "GBP":
        base, escala = "GBP", Decimal("1")
    else:
        base, escala = div, Decimal("1")
    par = f"EUR{base}=X"
    entry = cache.get(f"fx:{par}", {})
    rate = entry.get("valor")
    if not rate:
        v = _precio_y_divisa(par)   # los pares FX también via history
        if v is None:
            return None
        rate = v[0]
        cache[f"fx:{par}"] = {"valor": rate, "ts": time.time()}
    return (Decimal("1") / Decimal(str(rate))) * escala


def obtener_precios_eur(
    db: Session, cartera_id: str, forzar: bool = False
) -> tuple[dict[str, Decimal], list[str]]:
    """Precio actual en EUR por ISIN de las posiciones abiertas.
    Devuelve (precios_por_isin, isines_sin_precio)."""
    posiciones = [
        pos for pos in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars()
        if estado_posicion(db, pos.id)["cantidad"] > 0
    ]
    # Override manual: tiene prioridad absoluta sobre el feed.
    manual = {
        p.isin: Decimal(str(p.precio_manual_eur))
        for p in posiciones if p.precio_manual_eur is not None
    }
    isines = [p.isin for p in posiciones if p.isin not in manual]
    cache = _leer_cache()

    _resolver_figi(isines, cache)

    precios: dict[str, Decimal] = dict(manual)
    no_resueltos: list[str] = []
    ahora = time.time()
    for pos in posiciones:
        if pos.isin in manual:
            continue
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if sim is None:
            no_resueltos.append(pos.isin)
            continue
        entry = cache.get(f"px:{sim}", {})
        if forzar or not _fresco(entry, _TTL_PX):
            pv = _precio_y_divisa(sim)
            if pv is None:
                no_resueltos.append(pos.isin)
                continue
            entry = {"precio": pv[0], "divisa": pv[1], "ts": ahora}
            cache[f"px:{sim}"] = entry
        fac = _fx_eur(entry.get("divisa", "EUR"), cache)
        if fac is None:
            no_resueltos.append(pos.isin)
            continue
        precios[pos.isin] = Decimal(str(entry["precio"])) * fac

    _guardar_cache(cache)
    return precios, no_resueltos


_TTL_FUND = 7 * 24 * 3600   # fundamentales cambian lento


def _fetch_fundamentales(sim: str) -> dict | None:
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tk = yf.Ticker(sim)
            info = tk.info or {}
        cur = info.get("currency")
        # Quirk yfinance en LSE: cotiza en PENIQUES (currency='GBp') pero da
        # BPA/dividendo en LIBRAS → ×100 para igualar la unidad del precio.
        # (financialCurrency NO es fiable: da USD/EUR para valores británicos.)
        esc = 100 if cur == "GBp" else 1
        def _x(v):  # type: ignore[no-untyped-def]
            return v * esc if isinstance(v, (int, float)) else v
        # BPA histórico real por año fiscal (oldest→newest) del income statement.
        # Sirve para proyectar el EPS a 4 años con un CAGR REAL (no extrapolar 1
        # año). `eps_fiscal` = último ejercicio cerrado (base de la proyección).
        eps_hist: list[float] = []
        eps_fiscal = None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fin = tk.income_stmt
            for fila in ("Diluted EPS", "Basic EPS"):
                if fin is not None and fila in fin.index:
                    serie = fin.loc[fila].dropna()
                    # columnas = fechas fin de ejercicio; ordenar ascendente.
                    pares = sorted(serie.items(), key=lambda kv: kv[0])
                    eps_hist = [float(_x(v)) for _, v in pares if v is not None]
                    if eps_hist:
                        eps_fiscal = eps_hist[-1]
                    break
        except Exception:
            pass
        return {
            "eps": _x(info.get("trailingEps")),
            "forward_eps": _x(info.get("forwardEps")),
            "dividend": _x(info.get("dividendRate")),
            "pe": info.get("forwardPE") or info.get("trailingPE"),  # ratio, sin unidad
            "sector": info.get("sector"),
            "industry": info.get("industry"),   # para detectar familias de métrica contable

            "currency": cur,
            "eps_hist": eps_hist,        # BPA real por año fiscal (oldest→newest)
            "eps_fiscal": eps_fiscal,    # BPA del último ejercicio cerrado
        }
    except Exception:
        return None


def sector_por_isin(db: Session, cartera_id: str) -> dict[str, str]:
    """{isin: sector} de las posiciones abiertas, vía yfinance .info (cacheado
    con los fundamentales). Best-effort: los que no resuelven se omiten (el
    llamador los agrupa en 'Sin clasificar')."""
    obtener_precios_eur(db, cartera_id)   # asegura resolución figi
    cache = _leer_cache()
    out: dict[str, str] = {}
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if not sim:
            continue
        key = f"fund:{sim}"
        entry = cache.get(key, {})
        # Re-fetch si caduca O si es una entrada antigua sin el campo 'sector'.
        if not _fresco(entry, _TTL_FUND) or "sector" not in entry:
            f = _fetch_fundamentales(sim)
            if f is not None:
                entry = {**f, "ts": time.time()}
                cache[key] = entry
        sec = entry.get("sector")
        if sec:
            out[pos.isin] = sec
    _guardar_cache(cache)
    return out


def fundamentales_por_isin(db: Session, cartera_id: str) -> dict[str, dict]:
    """{isin: {eps, forward_eps, dividend, pe}} vía yfinance .info, cacheado 7d.
    Best-effort: símbolos sin datos se omiten."""
    obtener_precios_eur(db, cartera_id)   # asegura resolución figi
    cache = _leer_cache()
    out: dict[str, dict] = {}
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if not sim:
            continue
        key = f"fund:{sim}"
        entry = cache.get(key, {})
        # Re-fetch si caduca, o es entrada antigua sin 'currency' (BPA en libras
        # sin normalizar) o sin 'eps_fiscal' (sin BPA histórico para el CAGR).
        if (not _fresco(entry, _TTL_FUND) or "currency" not in entry
                or "eps_fiscal" not in entry or "industry" not in entry):
            f = _fetch_fundamentales(sim)
            if f is not None:
                entry = {**f, "ts": time.time()}
                cache[key] = entry
        if entry and any(entry.get(k) is not None for k in ("eps", "dividend", "pe")):
            out[pos.isin] = entry
    _guardar_cache(cache)
    return out


_FMP_BASE = "https://financialmodelingprep.com/stable"
_TTL_CONS = 7 * 24 * 3600   # consenso de analistas cambia lento


def _fetch_consenso(sim: str) -> dict | None:
    """Consenso de analistas (FMP). Plan gratuito → solo símbolos US (sin sufijo).
    Devuelve: eps_forward (FY+1), eps_consenso_4y + rango + nº analistas + año,
    precio_obj_consenso (menor entre media y mediana del target) + rango,
    per_hist_medio/mediano (PER trailing 5y, referencia). Todo en divisa nativa."""
    import statistics
    from datetime import date

    if "." in sim:
        return None   # FMP plan actual no cubre mercados no-US
    out: dict = {}

    def _get(path: str, **params) -> list:
        try:
            r = requests.get(f"{_FMP_BASE}/{path}",
                             params={**params, "apikey": settings.fmp_api_key}, timeout=12)
            data = r.json() if r.status_code == 200 else []
            return data if isinstance(data, list) else []
        except (requests.RequestException, ValueError):
            return []

    est = [e for e in _get("analyst-estimates", symbol=sim, period="annual", limit=5)
           if e.get("epsAvg") is not None and e.get("date")]
    est.sort(key=lambda e: e["date"])
    if est:
        hoy = date.today().isoformat()
        futuros = [e for e in est if e["date"] >= hoy] or est
        fwd, lejano = futuros[0], futuros[-1]      # FY+1 y el año más lejano (~+4)
        out["eps_forward"] = fwd.get("epsAvg")
        out["eps_consenso_4y"] = lejano.get("epsAvg")
        out["eps_high"] = lejano.get("epsHigh")
        out["eps_low"] = lejano.get("epsLow")
        out["num_analistas_eps"] = lejano.get("numAnalystsEps")
        out["anio_consenso_4y"] = int(lejano["date"][:4])

    pt = _get("price-target-consensus", symbol=sim)
    if pt:
        cand = [x for x in (pt[0].get("targetConsensus"), pt[0].get("targetMedian")) if x]
        if cand:
            out["precio_obj_consenso"] = min(cand)   # sesgo prudente
        out["target_high"] = pt[0].get("targetHigh")
        out["target_low"] = pt[0].get("targetLow")

    pes = [x["priceToEarningsRatio"] for x in _get("ratios", symbol=sim, period="annual", limit=5)
           if x.get("priceToEarningsRatio") and 0 < x["priceToEarningsRatio"] <= 80]
    if pes:
        out["per_hist_medio"] = round(statistics.mean(pes), 4)
        out["per_hist_mediano"] = round(statistics.median(pes), 4)
        out["per_hist_n"] = len(pes)   # años válidos (cap solo fiable con ≥3)

    return out or None


def consenso_por_isin(db: Session, cartera_id: str) -> dict[str, dict]:
    """{isin: consenso} de las posiciones abiertas, cacheado 7d. Best-effort:
    símbolos sin datos (no-US en plan gratuito) se omiten → quedan manuales."""
    obtener_precios_eur(db, cartera_id)   # asegura resolución figi
    cache = _leer_cache()
    out: dict[str, dict] = {}
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if not sim:
            continue
        key = f"cons:{sim}"
        entry = cache.get(key, {})
        if not _fresco(entry, _TTL_CONS):
            c = _fetch_consenso(sim)
            if c is not None:
                entry = {"datos": c, "ts": time.time()}
                cache[key] = entry
        datos = entry.get("datos") if entry else None
        if datos:
            out[pos.isin] = datos
    _guardar_cache(cache)
    return out


# ── Helpers a nivel de SÍMBOLO (reutilizados por watchlist/seguimiento) ──────

def precio_nativo_simbolo(sim: str) -> tuple[Decimal, str] | None:
    """Precio actual + divisa nativa de un símbolo yfinance, cacheado (px:<sim>)."""
    cache = _leer_cache()
    entry = cache.get(f"px:{sim}", {})
    if not _fresco(entry, _TTL_PX):
        pv = _precio_y_divisa(sim)
        if pv is None:
            return None
        entry = {"precio": pv[0], "divisa": pv[1], "ts": time.time()}
        cache[f"px:{sim}"] = entry
        _guardar_cache(cache)
    if entry.get("precio") is None:
        return None
    return Decimal(str(entry["precio"])), entry.get("divisa", "EUR")


def fundamentales_simbolo(sim: str) -> dict | None:
    """Fundamentales yfinance de un símbolo, cacheado (fund:<sim>, 7d)."""
    cache = _leer_cache()
    key = f"fund:{sim}"
    entry = cache.get(key, {})
    if not _fresco(entry, _TTL_FUND):
        f = _fetch_fundamentales(sim)
        if f is not None:
            entry = {**f, "ts": time.time()}
            cache[key] = entry
            _guardar_cache(cache)
    return entry or None


def consenso_simbolo(sim: str) -> dict | None:
    """Consenso de analistas de un símbolo, cacheado (cons:<sim>, 7d)."""
    cache = _leer_cache()
    key = f"cons:{sim}"
    entry = cache.get(key, {})
    if not _fresco(entry, _TTL_CONS):
        c = _fetch_consenso(sim)
        if c is not None:
            entry = {"datos": c, "ts": time.time()}
            cache[key] = entry
            _guardar_cache(cache)
    return entry.get("datos") if entry else None


def resolver_ticker(ticker: str) -> dict | None:
    """ticker → {ticker, isin, nombre, divisa} vía FMP profile (respaldo yfinance).
    Cacheado (tick:<TICKER>). Si no hay ISIN, usa el ticker como clave."""
    sim = (ticker or "").strip().upper()
    if not sim:
        return None
    cache = _leer_cache()
    entry = cache.get(f"tick:{sim}", {})
    if _fresco(entry, _TTL_FIGI) and entry.get("isin"):
        return {k: entry.get(k) for k in ("ticker", "isin", "nombre", "divisa")}

    info: dict | None = None
    try:
        r = requests.get(f"{_FMP_BASE}/profile",
                         params={"symbol": sim, "apikey": settings.fmp_api_key}, timeout=12)
        data = r.json() if r.status_code == 200 else []
        if isinstance(data, list) and data:
            p = data[0]
            info = {"ticker": sim, "isin": p.get("isin"),
                    "nombre": p.get("companyName"), "divisa": p.get("currency")}
    except (requests.RequestException, ValueError):
        pass

    if not info or not info.get("isin") or not info.get("nombre"):
        try:
            import yfinance as yf  # type: ignore[import-not-found]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t = yf.Ticker(sim)
                yi = t.isin
                inf = t.info or {}
            base = info or {"ticker": sim}
            yi = yi if (yi and yi != "-") else None
            info = {
                "ticker": sim,
                "isin": base.get("isin") or yi,
                "nombre": base.get("nombre") or inf.get("longName") or inf.get("shortName"),
                "divisa": base.get("divisa") or inf.get("currency"),
            }
        except Exception:
            pass

    if not info:
        return None
    if not info.get("isin"):
        info["isin"] = sim[:12]   # sin ISIN: el ticker hace de clave
    cache[f"tick:{sim}"] = {**info, "ts": time.time()}
    _guardar_cache(cache)
    return info


def precios_nativos(db: Session, cartera_id: str) -> dict[str, tuple[Decimal, str]]:
    """Precio actual en DIVISA NATIVA + divisa, por ISIN de posiciones abiertas.
    Para valoración: los ratios CAGR/yield son agnósticos a divisa, así que se
    comparan precio nativo y métricas (EPS/dividendo) en su moneda de reporte."""
    obtener_precios_eur(db, cartera_id)   # asegura cachés pobladas
    cache = _leer_cache()
    out: dict[str, tuple[Decimal, str]] = {}
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if not sim:
            continue
        px = cache.get(f"px:{sim}", {})
        if px.get("precio") is not None:
            out[pos.isin] = (Decimal(str(px["precio"])), px.get("divisa", "EUR"))
    return out
