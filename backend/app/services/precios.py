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

import datetime as _dt
import json
import re
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


def _precio_divisa_y_cierre_anterior(simbolo: str) -> tuple[float, str, float | None] | None:
    """Yahoo: precio actual + divisa + cierre del día ANTERIOR (sin coste extra,
    mismo `history(period='5d')`). El cierre anterior es la base del cambio
    intra-día para la vigilancia (alertas de "está subiendo X% HOY").
    """
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = yf.Ticker(simbolo)
            serie = t.history(period="5d")["Close"].dropna()
            if len(serie):
                cur = (t.history_metadata or {}).get("currency") or "USD"
                px = float(serie.iloc[-1])
                prev = float(serie.iloc[-2]) if len(serie) >= 2 else None
                return px, cur, prev
    except Exception:
        return None
    return None


def _precio_y_divisa(simbolo: str, usar_ia: bool = False) -> tuple[float, str] | None:
    """Yahoo → FMP (US). `usar_ia=True` añade el fallback IA (lento, minutos).
    Por defecto SIN IA — para no colgar las lecturas si Yahoo/FMP fallan. El
    refresco explícito (prefill / `forzar`) sí pasa `usar_ia=True`."""
    r = _precio_divisa_y_cierre_anterior(simbolo)
    if r is not None:
        return r[0], r[1]
    r = _precio_fmp_us(simbolo)      # fallback determinista (FMP plan gratuito: US)
    if r is not None:
        return r
    if usar_ia:
        return _precio_via_ia(simbolo)   # último recurso (lento): web search vía IA
    return None


def _precio_via_ia(simbolo: str) -> tuple[float, str] | None:
    """Fallback de precio vía IA + web cuando Yahoo y FMP no lo dan (p.ej. símbolos
    no-US fuera del plan gratuito de FMP). Lento; el caller cachea el resultado en
    `px:<sim>` → solo se llama una vez por símbolo. Best-effort: cualquier error → None."""
    try:
        from app.adapters.ia import get_clasificador
        system = (
            "Eres un asistente de cotizaciones. Busca en la web EL ÚLTIMO PRECIO DE CIERRE "
            "del símbolo dado y la DIVISA en la que cotiza (código ISO, o 'GBp'/'GBX' para "
            "peniques). Si no lo encuentras con seguridad, devuelve null en precio. NO inventes.\n"
            'Responde EXCLUSIVAMENTE con JSON: {"precio": <num o null>, "divisa": "<ISO o GBp>"}'
        )
        texto = get_clasificador().investigar(system, f"Símbolo: {simbolo}")
        m = re.search(r"\{.*\}", (texto or "").strip(), re.DOTALL)
        if not m:
            return None
        d = json.loads(m.group(0), strict=False)
        p = d.get("precio")
        c = d.get("divisa")
        if not isinstance(p, (int, float)) or p <= 0 or not c:
            return None
        return float(p), str(c).strip()
    except Exception:
        return None


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
    # TTL: el FX caduca como los precios (auditoría Cima 2026-06-11, A1 —
    # antes `if not rate` nunca refrescaba: un tipo de cambio de hace meses
    # se mezclaba con precios frescos en todas las valoraciones, pese a que
    # el docstring del módulo promete TTL 6h). Si el refetch falla, se
    # conserva el valor rancio (mejor FX viejo que posición sin valorar).
    if not rate or not _fresco(entry, _TTL_PX):
        v = _precio_y_divisa(par)   # los pares FX también via history
        if v is not None:
            rate = v[0]
            cache[f"fx:{par}"] = {"valor": rate, "ts": time.time()}
        elif not rate:
            return None
    return (Decimal("1") / Decimal(str(rate))) * escala


def obtener_precios_eur(
    db: Session, cartera_id: str, forzar: bool = False
) -> tuple[dict[str, Decimal], list[str]]:
    """Precio actual en EUR por ISIN de las posiciones abiertas.
    Devuelve (precios_por_isin, isines_sin_precio).

    RENDIMIENTO: por defecto NO refresca por antigüedad — usa la caché tal cual y
    solo baja de la red el precio que FALTE (posición nueva). Así las lecturas
    (dashboard, cartera, resumen) no bloquean en Yahoo en cada carga. El refresco
    de mercado es explícito: `forzar=True` (lo hace el prefill / "Actualizar")."""
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
        # Fetch si (a) refresco explícito (`forzar` → con IA fallback) o (b) FALTA
        # el precio cacheado (rápido: solo Yahoo+FMP, sin IA → no cuelga la lectura).
        # La antigüedad NO dispara fetch — para eso está "Actualizar desde el feed".
        if forzar or entry.get("precio") is None:
            # Intentamos Yahoo (que también nos da `prev_close` sin coste extra
            # para alertas intra-día); si falla, fallback a FMP (sin prev_close)
            # y, si se pidió, a IA. El `prev_close` puede quedar None — la
            # vigilancia intra-día simplemente se la salta para ese ISIN.
            r = _precio_divisa_y_cierre_anterior(sim)
            if r is None:
                pv = _precio_y_divisa(sim, usar_ia=forzar)
                if pv is None:
                    no_resueltos.append(pos.isin)
                    continue
                entry = {"precio": pv[0], "divisa": pv[1],
                         "prev_close": None, "ts": ahora}
            else:
                entry = {"precio": r[0], "divisa": r[1],
                         "prev_close": r[2], "ts": ahora}
            cache[f"px:{sim}"] = entry
        if entry.get("precio") is None:                # sin caché y sin forzar → no_resuelto, no romper
            no_resueltos.append(pos.isin)
            continue
        fac = _fx_eur(entry.get("divisa", "EUR"), cache)
        if fac is None:
            no_resueltos.append(pos.isin)
            continue
        precios[pos.isin] = Decimal(str(entry["precio"])) * fac

    _guardar_cache(cache)
    return precios, no_resueltos


def obtener_cierres_anteriores_eur(
    db: Session, cartera_id: str,
) -> dict[str, Decimal]:
    """Precio de cierre del DÍA ANTERIOR para cada ISIN de la cartera, en EUR.

    Lee SOLO del cache (no fuerza fetch). Asume que `obtener_precios_eur` se
    ha llamado antes (siempre se llama al dibujar dashboard / vigilancia) y ya
    pobló `prev_close`. Para ISINes sin prev_close cacheado, simplemente no
    aparecen en el dict — la vigilancia intra-día los omite sin error.

    Útil para alertas intra-día (cambio % desde el cierre de ayer)."""
    posiciones = [
        pos for pos in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars()
        if estado_posicion(db, pos.id)["cantidad"] > 0
    ]
    cache = _leer_cache()
    out: dict[str, Decimal] = {}
    for pos in posiciones:
        figi = cache.get(f"figi:{pos.isin}", {})
        sim = _ISIN_OVERRIDE.get(pos.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if sim is None:
            continue
        entry = cache.get(f"px:{sim}", {})
        prev = entry.get("prev_close")
        if prev is None:
            continue
        fac = _fx_eur(entry.get("divisa", "EUR"), cache)
        if fac is None:
            continue
        out[pos.isin] = Decimal(str(prev)) * fac
    return out


# ── Helpers públicos reutilizables (histórico mensual, ADR-004) ────────────

def resolver_simbolos(isines: list[str]) -> dict[str, str]:
    """ISIN → símbolo yfinance para los que resuelven (OpenFIGI + overrides).
    Reusa la misma resolución que el feed spot. Los no resueltos se omiten."""
    cache = _leer_cache()
    _resolver_figi(isines, cache)
    out: dict[str, str] = {}
    for isin in isines:
        figi = cache.get(f"figi:{isin}", {})
        sim = _ISIN_OVERRIDE.get(isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if sim is not None:
            out[isin] = sim
    _guardar_cache(cache)
    return out


def base_y_escala(divisa: str) -> tuple[str, Decimal]:
    """Divisa de cotización → (divisa BASE para el par EUR, escala). GBp/GBX →
    (GBP, 0.01); el resto → (divisa, 1). Misma convención de peniques que el spot."""
    raw = divisa or "EUR"
    div = raw.upper()
    if div == "EUR":
        return "EUR", Decimal("1")
    if raw == "GBp" or div in ("GBX", "GBP_PENCE"):
        return "GBP", Decimal("0.01")
    if div == "GBP":
        return "GBP", Decimal("1")
    return div, Decimal("1")


_TTL_FUND = 7 * 24 * 3600   # fundamentales cambian lento


def _cobertura_fcf(info: dict) -> float | None:
    """FCF / dividendo total pagado (free cash flow cubre N veces el
    dividendo). >1,5 holgado · 1,1-1,5 ajustado · <1,1 dividendo en riesgo
    (umbral del protocolo de rotaciones de WG)."""
    fcf = info.get("freeCashflow")
    dps = info.get("dividendRate")
    shares = info.get("sharesOutstanding")
    if not all(isinstance(x, (int, float)) for x in (fcf, dps, shares)):
        return None
    div_total = dps * shares
    if div_total <= 0:
        return None
    return fcf / div_total


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
        # Dividendo por acción ANUAL real (suma de pagos por año natural,
        # oldest→newest, solo años completos): base del g_div asistido. El
        # escalado GBp no afecta al CAGR (ratio), pero se aplica por coherencia.
        dps_hist: list[float] = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                divs = tk.dividends
            if divs is not None and len(divs) > 0:
                por_anio: dict[int, float] = {}
                for fecha, v in divs.items():
                    por_anio[fecha.year] = por_anio.get(fecha.year, 0.0) + float(v)
                anio_en_curso = _dt.date.today().year   # incompleto → fuera
                dps_hist = [float(_x(por_anio[a]))
                            for a in sorted(por_anio) if a < anio_en_curso][-6:]
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
            "beta": info.get("beta"),    # beta 5y (unitless) — señal de volatilidad
            # ROE como proxy de calidad: ROIC no está en .info; returnOnEquity sí
            # (fracción, p.ej. 0.37 = 37%). Apalancado, pero útil para el corte.
            "roe": info.get("returnOnEquity"),
            # Salud del dividendo (V6). Cobertura FCF calculada con valores
            # CRUDOS de yfinance (FCF, DPS y nº acciones en la misma divisa
            # financiera — sin el escalado GBp, que rompería las unidades).
            "payout": info.get("payoutRatio"),   # sobre beneficios (fracción)
            "fcf_cobertura_div": _cobertura_fcf(info),
            "dps_hist": dps_hist,    # DPS anual real (oldest→newest, años completos)
        }
    except Exception:
        return None


def sector_por_isin(db: Session, cartera_id: str, refrescar: bool = False) -> dict[str, str]:
    """{isin: sector} de las posiciones abiertas (cacheado con los fundamentales).
    Por defecto SOLO LEE la caché `fund:` (la calienta el prefill) → no bloquea las
    lecturas (dashboard→dividendos). `refrescar=True` baja del feed lo que falte."""
    if refrescar:
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
        if refrescar and (not _fresco(entry, _TTL_FUND) or "sector" not in entry):
            f = _fetch_fundamentales(sim)
            if f is not None:
                entry = {**f, "ts": time.time()}
                cache[key] = entry
        sec = entry.get("sector")
        if sec:
            out[pos.isin] = sec
    if refrescar:
        _guardar_cache(cache)
    return out


def fundamentales_por_isin(
    db: Session, cartera_id: str, refrescar: bool = False
) -> dict[str, dict]:
    """{isin: {eps, forward_eps, dividend, pe}} vía yfinance .info, cacheado 7d.
    Por defecto SOLO LEE la caché (no bloquea las lecturas); `refrescar=True`
    repuebla desde el feed (prefill). Best-effort: símbolos sin datos se omiten."""
    if refrescar:
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
        # Re-fetch (solo en refresco) si caduca o le faltan campos clave.
        if refrescar and (not _fresco(entry, _TTL_FUND) or "currency" not in entry
                          or "eps_fiscal" not in entry or "industry" not in entry
                          or "beta" not in entry):
            f = _fetch_fundamentales(sim)
            if f is not None:
                entry = {**f, "ts": time.time()}
                cache[key] = entry
        if entry and any(entry.get(k) is not None for k in ("eps", "dividend", "pe")):
            out[pos.isin] = entry
    if refrescar:
        _guardar_cache(cache)
    return out


_FMP_BASE = "https://financialmodelingprep.com/stable"
_TTL_CONS = 7 * 24 * 3600   # consenso de analistas cambia lento
_TTL_HIST = 7 * 24 * 3600   # CAGR histórico cambia lento


def _fetch_cagr_historico(sim: str, max_anios: int = 20) -> float | None:
    """CAGR de PRECIO anualizado sobre el histórico disponible (tope `max_anios`).
    Proxy de retorno de un ETF/índice que no tiene BPA. Best-effort vía yfinance;
    None si no hay al menos ~1 año de serie."""
    try:
        import pandas as pd  # type: ignore[import-not-found]
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            serie = yf.Ticker(sim).history(period="max")["Close"].dropna()
    except Exception:
        return None
    if len(serie) < 2:
        return None
    corte = serie.index[-1] - pd.Timedelta(days=int(365.25 * max_anios))
    serie = serie.loc[serie.index >= corte]
    if len(serie) < 2:
        return None
    p0, p1 = float(serie.iloc[0]), float(serie.iloc[-1])
    anios = (serie.index[-1] - serie.index[0]).days / 365.25
    if p0 <= 0 or anios < 1:           # menos de 1 año → no anualizar
        return None
    return (p1 / p0) ** (1 / anios) - 1


def _fetch_mercado() -> dict | None:
    """Datos macro objetivos vía yfinance — ancla numérica para el régimen auto.

    Devuelve: SP500 drawdown (52 semanas) + VIX (regla −14%), Brent y WTI (señal
    geopolítica/m. primas), spread 10y-3m de la curva (señal de recesión: <0 =
    inversión, antesala histórica), tendencia SP500 vs SMA200 (señal mercado).
    Best-effort: si yfinance falla, None. Indicadores individuales pueden venir
    null sin invalidar el resto."""
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sp = yf.Ticker("^GSPC").history(period="1y")["Close"].dropna()
            vix_s = yf.Ticker("^VIX").history(period="5d")["Close"].dropna()
            brent_s = yf.Ticker("BZ=F").history(period="5d")["Close"].dropna()
            wti_s = yf.Ticker("CL=F").history(period="5d")["Close"].dropna()
            tnx_s = yf.Ticker("^TNX").history(period="5d")["Close"].dropna()    # 10y note yield (x10)
            irx_s = yf.Ticker("^IRX").history(period="5d")["Close"].dropna()    # 13w bill yield
    except Exception:
        return None
    if len(sp) < 2:
        return None
    maxv, cur = float(sp.max()), float(sp.iloc[-1])
    drawdown = (cur - maxv) / maxv if maxv > 0 else 0.0
    sma200 = float(sp.tail(200).mean()) if len(sp) >= 50 else None
    spread = None
    if len(tnx_s) and len(irx_s):
        # ^TNX y ^IRX cotizan en %×10 (yfinance: 42.50 = 4,25%). Spread en pp.
        spread = (float(tnx_s.iloc[-1]) - float(irx_s.iloc[-1])) / 10.0
    return {
        "sp_drawdown": drawdown,
        "sp_precio": cur,
        "sp_sma200": sma200,
        "vix": float(vix_s.iloc[-1]) if len(vix_s) else None,
        "brent_usd": float(brent_s.iloc[-1]) if len(brent_s) else None,
        "wti_usd": float(wti_s.iloc[-1]) if len(wti_s) else None,
        "yield_curve_spread_pp": spread,
    }


def _macro_datos() -> dict | None:
    """Datos macro objetivos completos cacheados 6h (SP/VIX/Brent/WTI/curva).
    Es el cache base; `mercado_correccion()` extrae solo lo que la regla −14%
    necesita, y `datos_macro_objetivos()` lo expone entero al régimen auto."""
    cache = _leer_cache()
    entry = cache.get("mercado:sp_vix", {})
    if not _fresco(entry, _TTL_PX) or "sp_drawdown" not in entry:
        m = _fetch_mercado()
        if m is not None:
            entry = {**m, "ts": time.time()}
            cache["mercado:sp_vix"] = entry
            _guardar_cache(cache)
    return entry if "sp_drawdown" in entry else None


def mercado_correccion() -> dict | None:
    """{sp_drawdown: fracción negativa, vix: float|None} cacheado 6h. Datos de
    mercado globales (no por cartera). None si no hay datos."""
    entry = _macro_datos()
    if entry is None:
        return None
    return {"sp_drawdown": entry["sp_drawdown"], "vix": entry.get("vix")}


def datos_macro_objetivos() -> dict | None:
    """Snapshot completo de datos macro auto-fetcheados para el régimen auto.
    Mismo cache que `mercado_correccion`: una sola petición a yfinance/día.
    Claves: sp_drawdown, sp_precio, sp_sma200, vix, brent_usd, wti_usd,
    yield_curve_spread_pp. Cada una puede ser None si no se pudo recuperar."""
    return _macro_datos()


def cagr_historico_por_isin(
    db: Session, cartera_id: str, isines: list[str], refrescar: bool = False
) -> dict[str, Decimal]:
    """CAGR de precio histórico anualizado por ISIN, SOLO para los `isines`
    pedidos (p.ej. los ETF). Cacheado 7d. Por defecto SOLO LEE la caché (no
    bloquea las lecturas); `refrescar=True` baja la serie en vivo (prefill)."""
    pedidos = set(isines)
    if not pedidos:
        return {}
    if refrescar:
        obtener_precios_eur(db, cartera_id)   # asegura resolución figi
    cache = _leer_cache()
    out: dict[str, Decimal] = {}
    for isin in pedidos:
        figi = cache.get(f"figi:{isin}", {})
        sim = _ISIN_OVERRIDE.get(isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if not sim:
            continue
        key = f"histcagr:{sim}"
        entry = cache.get(key, {})
        if refrescar and (not _fresco(entry, _TTL_HIST) or "cagr" not in entry):
            entry = {"cagr": _fetch_cagr_historico(sim), "ts": time.time()}
            cache[key] = entry
        if entry.get("cagr") is not None:
            out[isin] = Decimal(str(entry["cagr"]))
    if refrescar:
        _guardar_cache(cache)
    return out


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

def precio_nativo_simbolo(sim: str, refrescar: bool = False) -> tuple[Decimal, str] | None:
    """Precio actual + divisa nativa de un símbolo yfinance, cacheado (px:<sim>).
    SOLO baja de la red si `refrescar=True` (refresco explícito). Las lecturas
    nunca tocan la red para evitar colgar el GET con la cascada YF→FMP→IA."""
    cache = _leer_cache()
    entry = cache.get(f"px:{sim}", {})
    if refrescar:
        pv = _precio_y_divisa(sim)
        if pv is not None:
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


def precios_nativos(
    db: Session, cartera_id: str, refrescar: bool = False
) -> dict[str, tuple[Decimal, str]]:
    """Precio actual en DIVISA NATIVA + divisa, por ISIN de posiciones abiertas.
    Para valoración: los ratios CAGR/yield son agnósticos a divisa, así que se
    comparan precio nativo y métricas (EPS/dividendo) en su moneda de reporte.

    Por defecto SOLO LEE la caché (instantáneo): no bloquea recálculos (editar una
    estimación) con fetch de mercado. `refrescar=True` repuebla precios en vivo —
    lo hace el prefill / refresco explícito, no cada lectura."""
    if refrescar:
        obtener_precios_eur(db, cartera_id, forzar=True)   # repuebla cachés
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
