"""Estimaciones de valoración (Fase 2) — modelo multi-método de WG.

Por cada posición: precio objetivo = `multiplo_objetivo` (N) × `metrica_base_4y`
(O), según `tipo_val`. De ahí:
  - CAGR4   = (precio_objetivo / precio_actual)^(1/4) − 1   (revalorización anual)
  - yield   = dividendo_share / precio_actual
  - CAGR4+Div = CAGR4 + yield                               (retorno total anual)

Todo en divisa nativa (ratios agnósticos a divisa). El agregado de cartera
pondera por valor de mercado (EUR) de cada posición y alimenta el dashboard.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import models
from app.services.fifo import estado_posicion


def _d(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None


@dataclass
class EstimacionCalc:
    isin: str
    nombre: str
    tipo_val: str
    divisa: str | None
    precio_actual: Decimal | None
    eps_actual: Decimal | None
    multiplo_objetivo: Decimal | None
    metrica_base_4y: Decimal | None
    dividendo_share: Decimal | None
    precio_objetivo: Decimal | None
    crecimiento_pct: Decimal | None      # CAGR implícito eps_actual → metrica_base_4y
    cagr4_pct: Decimal | None
    div_yield_pct: Decimal | None
    # MÉTRICA MAESTRA (decisión Angel 2026-06-11): CAGR4 + Div NETO del
    # horizonte = cagr4 + yield × (1 − tipo_efectivo) × factor_crecimiento_4Y.
    cagr4_div_pct: Decimal | None
    notas: str | None
    # Variante bruta y plana (la del Excel analisis.xlsx) — solo reconciliación.
    cagr4_div_bruto_pct: Decimal | None = None
    div_yield_neto_pct: Decimal | None = None        # yield × (1 − tipo_efectivo)
    div_horizonte_pct: Decimal | None = None         # neto × factor crecimiento 4Y
    metrica_divisa: str | None = None                # divisa de la métrica (para reconciliar con el precio)
    tipo_efectivo_div_pct: Decimal | None = None     # 0.19 + exceso CDI del país
    crecimiento_div_aplicado_pct: Decimal | None = None  # g_div usado (campo o derivado)
    # Consenso de analistas (referencia, NO editable):
    eps_forward: Decimal | None = None
    eps_consenso_4y: Decimal | None = None
    eps_consenso_high: Decimal | None = None
    eps_consenso_low: Decimal | None = None
    num_analistas_eps: int | None = None
    anio_consenso_4y: int | None = None
    precio_obj_consenso: Decimal | None = None
    target_high: Decimal | None = None
    target_low: Decimal | None = None
    per_hist_medio: Decimal | None = None
    per_hist_mediano: Decimal | None = None
    mult_alerta: str | None = None    # aviso de divergencia/normalización del múltiplo


def _cagr(final: Decimal, inicial: Decimal, anios: int) -> Decimal | None:
    if inicial is None or inicial <= 0 or final is None or final <= 0:
        return None
    try:
        return Decimal(str((float(final) / float(inicial)) ** (1 / anios) - 1))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def _calc_item(
    isin: str, nombre: str, e: models.Estimacion | None,
    precio: Decimal | None, divisa: str | None,
    observadas: dict[str, Decimal] | None = None,
) -> EstimacionCalc:
    """Calcula una estimación (precio objetivo, CAGR4, yield…) a partir de la fila
    Estimacion + precio nativo. Compartido por cartera y seguimiento/watchlist."""
    tipo = e.tipo_val if e else "PER"
    eps = Decimal(str(e.eps_actual)) if e and e.eps_actual is not None else None
    mult = Decimal(str(e.multiplo_objetivo)) if e and e.multiplo_objetivo is not None else None
    base = Decimal(str(e.metrica_base_4y)) if e and e.metrica_base_4y is not None else None
    div = Decimal(str(e.dividendo_share)) if e and e.dividendo_share is not None else None

    c: dict = {}
    if e and e.consenso_json:
        try:
            c = json.loads(e.consenso_json) or {}
        except ValueError:
            c = {}
    # Horizonte REAL del CAGR: si la métrica viene del consenso, su año objetivo
    # (anio_consenso_4y) puede estar a menos de 4 años vista cuando el consenso
    # envejece entre prefills. Annualizar sobre 4 años fijos subestima el retorno
    # (auditoría 2026-06-18, 3C). Sin año de consenso → 4 (proyección derivada a 4A).
    import datetime as _dt
    anio_obj = c.get("anio_consenso_4y")
    horizonte = 4
    try:
        if anio_obj:
            horizonte = max(1, int(anio_obj) - _dt.date.today().year)
    except (TypeError, ValueError):
        horizonte = 4
    consenso_caduco = bool(anio_obj) and horizonte <= 1

    precio_obj = (mult * base) if (mult is not None and base is not None) else None

    # Reconciliar divisa de la métrica (precio_obj, dividendo) con la del PRECIO.
    # Si están en monedas distintas, comparar precio_obj con precio (o div/precio)
    # da un CAGR/yield disparatado (auditoría 2026-06-18, Grupo 1):
    #   - misma familia distinta escala (GBp↔GBP): se reescala por la escala (exacto, sin FX).
    #   - divisas realmente distintas: no inventamos número (precio_obj/div → None) y se avisa.
    from app.services.precios import base_y_escala
    div_yield = div
    alerta_divisa = False
    md = getattr(e, "metrica_divisa", None) if e else None
    if md and divisa and md != divisa:
        mb, em = base_y_escala(md)
        pb, ep = base_y_escala(divisa)
        if mb == pb and ep:
            f = em / ep
            if precio_obj is not None:
                precio_obj = precio_obj * f
            if div_yield is not None:
                div_yield = div_yield * f
        else:
            precio_obj = None
            div_yield = None
            alerta_divisa = True

    cagr4 = _cagr(precio_obj, precio, horizonte) if (precio_obj is not None and precio) else None
    yld = (div_yield / precio) if (div_yield is not None and precio and precio > 0) else None
    # `crec` = crecimiento de la MÉTRICA (col O vs col L) — SOLO tiene sentido en
    # PER, donde ambas son EPS. En P/FCF·P/BV·P/FRE, `base` es la métrica (FRE,
    # NAV, FCF/share…) y `eps_actual` sigue siendo el EPS real: compararlos mezcla
    # familias y da un crecimiento falso que, además, contaminaba el g_div del
    # dividendo cuando no había histórico de DPS (caso Blue Owl / OWL, P/FRE).
    crec = (_cagr(base, eps, horizonte)
            if (tipo == "PER" and base is not None and eps is not None) else None)

    # Componente Div NETA del horizonte (decisión Angel 2026-06-11):
    #   neto = yield × (1 − tipo_efectivo); tipo_efectivo = suelo del ahorro
    #   (19% config.) + exceso de retención de origen sobre el tope CDI.
    #   horizonte = neto × media[(1+g_div)^t, t=1..4] — el yield medio que se
    #   cobrará de verdad en los 4 años, no el plano de hoy.
    # g_div: campo editable; si falta, se deriva del crecimiento implícito de
    # la métrica capado a [−5%, +20%] (sin métrica → 0, plano conservador).
    from app.services.dividendo_neto import (
        exceso_observado_pct, factor_horizonte_div, pais_de_isin,
    )
    g_div = (Decimal(str(e.crecimiento_div_pct))
             if e is not None and e.crecimiento_div_pct is not None else None)
    if g_div is None:
        g_div = crec if crec is not None else Decimal("0")
    # Clamp SIEMPRE a [−5%, +20%] (no solo la rama derivada): un g_div editado a
    # mano fuera de banda (p.ej. 15 = 1500%) disparaba factor_horizonte_div a
    # ×17 y reventaba el cagr4_div (auditoría 2026-06-18, hallazgo 6A).
    g_div = max(Decimal("-0.05"), min(Decimal("0.20"), g_div))
    # Tipo efectivo CALIBRADO: suelo del ahorro + exceso sobre el tope CDI con
    # la retención OBSERVADA en los propios dividendos del usuario (el broker
    # manda: DeGiro retiene 25% en FR aunque la estatutaria de TR sea 12,8%).
    # Sin historial → estatutaria del vendor, como antes.
    pais = pais_de_isin(isin, nombre)
    tipo_efectivo = (Decimal(str(settings.tipo_ahorro_dividendo))
                     + exceso_observado_pct(pais, isin, observadas))
    yld_neto = (yld * (Decimal("1") - tipo_efectivo)) if yld is not None else None
    div_horizonte = (yld_neto * factor_horizonte_div(g_div)) if yld_neto is not None else None

    cagr4_div_bruto = (cagr4 + yld) if (cagr4 is not None and yld is not None) else cagr4
    cagr4_div = (cagr4 + div_horizonte) if (cagr4 is not None and div_horizonte is not None) else cagr4

    # Alerta del múltiplo. La marca defensiva (familia de métrica contable sin
    # confirmar) tiene prioridad: avisa de que NO se autocompletó y hay que fijar
    # el tipo_val. Si no, la alerta de divergencia consenso/histórico (solo PER).
    revisar = c.get("revisar_tipo_val")
    if alerta_divisa:
        # Divisa de la métrica ≠ divisa del precio y no reconciliable → no se
        # calcula CAGR (mejor sin número que con uno falso).
        alerta = f"métrica en {md} pero precio en {divisa} — revisa la divisa (CAGR no calculado)"
    elif revisar:
        alerta = f"posible métrica contable ({revisar}?) — fija el tipo_val, no autocompletado"
    elif consenso_caduco:
        # El año objetivo del consenso ya llegó/pasó → el CAGR se anualiza sobre
        # ~1 año y queda inflado. Refresca las estimaciones (3C).
        alerta = "consenso caducado (año objetivo alcanzado) — refresca las estimaciones"
    elif tipo == "PER":
        alerta = _multiplo_consenso_hist(c)[1]
    elif mult is None or base is None:
        # No-PER clasificado pero sin múltiplo/métrica objetivo (los no-PER se
        # fijan a mano hasta la Fase 2b) → avisar en vez de quedar mudo.
        from app.db.models import etiquetas_tipo_val
        alerta = f"{etiquetas_tipo_val(tipo)[0]} clasificado — fija múltiplo y métrica objetivo"
    else:
        alerta = None

    return EstimacionCalc(
        isin=isin, nombre=nombre, tipo_val=tipo, divisa=divisa,
        precio_actual=precio, eps_actual=eps, multiplo_objetivo=mult,
        metrica_base_4y=base, dividendo_share=div, precio_objetivo=precio_obj,
        crecimiento_pct=crec, cagr4_pct=cagr4, div_yield_pct=yld,
        cagr4_div_pct=cagr4_div, cagr4_div_bruto_pct=cagr4_div_bruto,
        div_yield_neto_pct=yld_neto, div_horizonte_pct=div_horizonte,
        metrica_divisa=md,
        tipo_efectivo_div_pct=tipo_efectivo, crecimiento_div_aplicado_pct=g_div,
        notas=(e.notas if e else None),
        eps_forward=_d(c.get("eps_forward")),
        eps_consenso_4y=_d(c.get("eps_consenso_4y")),
        eps_consenso_high=_d(c.get("eps_high")),
        eps_consenso_low=_d(c.get("eps_low")),
        num_analistas_eps=c.get("num_analistas_eps"),
        anio_consenso_4y=c.get("anio_consenso_4y"),
        precio_obj_consenso=_d(c.get("precio_obj_consenso")),
        target_high=_d(c.get("target_high")),
        target_low=_d(c.get("target_low")),
        per_hist_medio=_d(c.get("per_hist_medio")),
        per_hist_mediano=_d(c.get("per_hist_mediano")),
        mult_alerta=alerta,
    )


def _filas_estimacion(db: Session, cartera_id: str) -> dict[str, models.Estimacion]:
    return {
        e.isin: e for e in db.execute(
            select(models.Estimacion).where(models.Estimacion.cartera_id == cartera_id)
        ).scalars()
    }


def _multiplo_consenso_hist(c: dict | None) -> tuple[float | None, str | None]:
    """Múltiplo normalizado + alerta. `min(múltiplo forward de consenso, PER
    histórico mediano con ≥3 años válidos)`, acotado a [5, 45]. Alerta si consenso
    e histórico divergen >30% (posible re-rating que el min capa en silencio) o si
    el normalizado queda fuera de rango. El min solo baja → la bandera es el
    antídoto: avisa cuando la prudencia podría costar un compounder."""
    if not c:
        return None, None
    target = c.get("precio_obj_consenso")
    eps_fwd = c.get("eps_forward")
    per_med = c.get("per_hist_mediano")
    per_n = c.get("per_hist_n") or 0
    cm = (target / eps_fwd) if (target and eps_fwd and eps_fwd > 0) else None
    hm = per_med if (per_med and per_n >= 3) else None
    cand = [m for m in (cm, hm) if m and m > 0]
    if not cand:
        return None, None
    norm = min(cand)
    alerta = None
    if cm and hm and abs(cm - hm) / min(cm, hm) > 0.30:
        alerta = f"consenso {cm:.0f}× vs histórico {hm:.0f}× — posible re-rating, revisa"
    elif norm > 45 or norm < 5:
        alerta = f"múltiplo normalizado {norm:.0f}× fuera de rango — revisa"
    return max(5.0, min(45.0, norm)), alerta


def calcular_estimaciones(db: Session, cartera_id: str) -> list[EstimacionCalc]:
    from app.services.dividendo_neto import tasa_origen_observada
    from app.services.precios import precios_nativos

    natives = precios_nativos(db, cartera_id)
    filas = _filas_estimacion(db, cartera_id)
    observadas = tasa_origen_observada(db, cartera_id)
    out: list[EstimacionCalc] = []
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        precio, divisa = natives.get(pos.isin, (None, None))
        out.append(_calc_item(pos.isin, pos.nombre or pos.isin,
                              filas.get(pos.isin), precio, divisa,
                              observadas=observadas))
    _enriquecer_etfs_historico(db, cartera_id, out, observadas=observadas)
    out.sort(key=lambda x: x.nombre.lower())
    return out


# Fondos/ETF que `classify_isin` no reconoce por ISIN (algunos UCITS irlandeses:
# Aristocrats, Min Volatility, Equity Premium Income) → fallback por nombre. NO se
# valoran por PER: su retorno = CAGR de precio histórico (ventana máxima) + yield.
_FONDO_KW = ("UCITS", "ETF", "ARISTOCRAT", "MIN VOLATILITY", "MINIMUM VOLATILITY",
             "EQUITY PREMIUM INCOME")


def _es_fondo(isin: str | None, nombre: str | None) -> bool:
    from app.services.posiciones import _tipo_activo
    if _tipo_activo(isin, nombre) == "ETF":
        return True
    n = (nombre or "").upper()
    return any(k in n for k in _FONDO_KW)


def _enriquecer_etfs_historico(db: Session, cartera_id: str,  # noqa: PLR0912
                               calcs: list[EstimacionCalc],
                               observadas: dict[str, Decimal] | None = None) -> None:
    """ETF/índice no tienen BPA → el modelo no les da CAGR. Como proxy de retorno
    total usamos el CAGR de PRECIO histórico (máx. ~20a) + el yield actual. Muta
    los calcs en sitio. Best-effort: si falla la red, los deja como estaban."""
    from app.services import precios as precios_svc

    etf = [c for c in calcs
           if c.cagr4_div_pct is None and _es_fondo(c.isin, c.nombre)]
    if not etf:
        return
    try:
        hist = precios_svc.cagr_historico_por_isin(db, cartera_id, [c.isin for c in etf])
    except Exception:
        return
    if not hist:
        return
    try:
        funds = precios_svc.fundamentales_por_isin(db, cartera_id)
    except Exception:
        funds = {}
    for c in etf:
        cagr_precio = hist.get(c.isin)
        if cagr_precio is None:
            continue
        yld = c.div_yield_pct
        if yld is None:                                   # ETF de reparto: yield = div/precio
            div = funds.get(c.isin, {}).get("dividend")
            if div is not None and c.precio_actual and c.precio_actual > 0:
                yld = Decimal(str(div)) / c.precio_actual
        c.cagr4_pct = cagr_precio
        c.div_yield_pct = yld
        from app.services.dividendo_neto import exceso_observado_pct, pais_de_isin
        tipo_ef = (Decimal(str(settings.tipo_ahorro_dividendo))
                   + exceso_observado_pct(pais_de_isin(c.isin, c.nombre),
                                          c.isin, observadas))
        yld_b = yld or Decimal("0")
        c.cagr4_div_bruto_pct = cagr_precio + yld_b
        c.div_yield_neto_pct = yld_b * (Decimal("1") - tipo_ef)
        c.div_horizonte_pct = c.div_yield_neto_pct      # ETFs: g_div = 0 (plano)
        c.tipo_efectivo_div_pct = tipo_ef
        c.crecimiento_div_aplicado_pct = Decimal("0")
        c.cagr4_div_pct = cagr_precio + c.div_horizonte_pct
        c.notas = ((c.notas + " · ") if c.notas else "") + "CAGR histórico (precio) + yield"


def calcular_estimaciones_seguimiento(db: Session, cartera_id: str) -> list[EstimacionCalc]:
    """Estimaciones de las empresas en seguimiento (watchlist). Sin precio
    actual no hay CAGR4+Div, así que merece la pena fetchar en vivo cuando el
    cache no tiene el ticker (los seguimientos no entran en `obtener_precios_eur`,
    que solo procesa posiciones). Estrategia:
      1. ticker → cache (`precio_nativo_simbolo`, instantáneo si está).
      2. ticker → red (yfinance) si no está cacheado.
      3. ISIN → resolución FIGI → símbolo yahoo → red (igual que posiciones)
         como fallback cuando el ticker no es un símbolo Yahoo válido.
    """
    from app.services.precios import (
        _ISIN_OVERRIDE, _leer_cache, _resolver_figi, _yf_simbolo,
        precio_nativo_simbolo,
    )

    filas = _filas_estimacion(db, cartera_id)
    from app.services.dividendo_neto import tasa_origen_observada
    observadas = tasa_origen_observada(db, cartera_id)
    seguimientos = list(db.execute(
        select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
    ).scalars())
    # Resolución FIGI de TODOS los ISINes de seguimiento en una sola pasada
    # (cachea ticker+exchCode 30d). Solo dispara la red si falta alguno.
    cache = _leer_cache()
    _resolver_figi([s.isin for s in seguimientos], cache)

    out: list[EstimacionCalc] = []
    actualizar_ticker = False
    for s in seguimientos:
        pv: tuple[Decimal, str] | None = None
        # 1) PRIMERO ISIN → FIGI → símbolo Yahoo. El ISIN es identificador
        # global fiable; los tickers que el usuario teclea pueden ser textos
        # libres ("MCDONALD", "COSTCO") que Yahoo no reconoce.
        figi = cache.get(f"figi:{s.isin}", {})
        sim_yf = _ISIN_OVERRIDE.get(s.isin) or _yf_simbolo(figi.get("ticker"), figi.get("exch"))
        if sim_yf:
            pv = precio_nativo_simbolo(sim_yf) or precio_nativo_simbolo(sim_yf, refrescar=True)
        # 2) Si FIGI no resuelve (cripto, ISIN raro), intento con el ticker manual.
        if pv is None:
            pv = precio_nativo_simbolo(s.ticker) or precio_nativo_simbolo(s.ticker, refrescar=True)
        # 3) Último recurso: buscar en yfinance por nombre. Usamos `nombre` si
        # existe; si no, el propio `ticker` (el usuario suele teclear ahí el
        # nombre comercial — caso real: "MCDONALD" en ticker, nombre vacío).
        if pv is None:
            termino = (s.nombre or s.ticker or "").strip()
            if termino:
                sim_busqueda = _buscar_simbolo_yfinance(termino)
                if sim_busqueda:
                    pv = precio_nativo_simbolo(sim_busqueda, refrescar=True)
                    # Self-healing: guardamos el símbolo bueno en el watchlist
                    # para que la próxima lectura sea directa (paso 1 acertará).
                    if pv is not None and s.ticker != sim_busqueda:
                        s.ticker = sim_busqueda
                        actualizar_ticker = True
        precio, divisa = (pv[0], pv[1]) if pv else (None, s.divisa)
        out.append(_calc_item(s.isin, s.nombre or s.ticker,
                              filas.get(s.isin), precio, divisa,
                              observadas=observadas))
    if actualizar_ticker:
        db.commit()
    out.sort(key=lambda x: x.nombre.lower())
    return out


def _buscar_simbolo_yfinance(nombre: str) -> str | None:
    """Busca un símbolo Yahoo por nombre de empresa. Útil cuando el ticker
    del watchlist no es un símbolo Yahoo válido (caso real: usuarios que
    teclean "MCDONALD" en vez de "MCD"). Best-effort; cualquier error → None."""
    import warnings
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = yf.Search(nombre, max_results=1)
            quotes = getattr(r, "quotes", None) or []
            if quotes:
                sym = quotes[0].get("symbol")
                return str(sym) if sym else None
    except Exception:
        return None
    return None


def prefill_estimaciones(db: Session, cartera_id: str) -> int:
    """Auto-rellena (solo campos VACÍOS, sin pisar ediciones) desde el CONSENSO
    de analistas, con fundamentales yfinance como respaldo. Por defecto:
      - multiplo_objetivo = precio_obj_consenso / EPS_forward   (PER forward de
        consenso; precio_obj = menor entre target medio y mediano → sesgo prudente)
      - metrica_base_4y   = EPS de consenso del año ~+4
      - eps_actual        = EPS trailing (yfinance)
      - dividendo_share   = dividendo/acción (yfinance)
    Si no hay consenso (no-US en plan gratuito) → respaldo: múltiplo = forwardPE,
    métrica 4A = EPS·(1+g)⁴. Guarda el consenso como referencia (consenso_json).
    Devuelve nº de posiciones tocadas. Híbrido: el usuario ajusta libremente."""
    from app.services.precios import (
        cagr_historico_por_isin, consenso_por_isin, fundamentales_por_isin, obtener_precios_eur,
    )

    # El prefill ES el refresco de mercado explícito: repuebla precios + fundamentales
    # en vivo (con refrescar=True). El resto de lecturas usan solo la caché → instantáneas.
    obtener_precios_eur(db, cartera_id, forzar=True)
    # También seguimientos (no entran en obtener_precios_eur que solo mira posiciones)
    # — sin esto, los watchlist se quedan sin precio y el CAGR4+Div queda en blanco.
    from app.services.precios import precio_nativo_simbolo
    for seg in db.execute(
        select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
    ).scalars():
        precio_nativo_simbolo(seg.ticker, refrescar=True)
    funds = fundamentales_por_isin(db, cartera_id, refrescar=True)
    cons = consenso_por_isin(db, cartera_id)
    existentes = _filas_estimacion(db, cartera_id)
    n = 0
    for isin in set(funds) | set(cons):
        e = existentes.get(isin)
        nueva = e is None
        if nueva:
            e = models.Estimacion(cartera_id=cartera_id, isin=isin, tipo_val="PER")
        _seed_estimacion(e, funds.get(isin, {}), cons.get(isin))
        if nueva:
            db.add(e)
        n += 1
    db.commit()

    # Calienta el CAGR histórico de los fondos/ETF para que las lecturas (cache-only)
    # lo tengan disponible (su valoración no es por PER).
    fondos = [p.isin for p in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)).scalars()
        if _es_fondo(p.isin, p.nombre)]
    if fondos:
        cagr_historico_por_isin(db, cartera_id, fondos, refrescar=True)
    return n


# Banda de saneo del CAGR del BPA para la proyección a 4 años (sin consenso):
# evita que un año atípico lo dispare o que un sector en crisis lo hunda.
_BANDA_CAGR_EPS = (-0.05, 0.15)

# Familias de industria (yfinance) que NO se valoran por BPA: gestoras de activos
# y BDCs (FRE/share o NAV/share) y REITs (FFO/NAV). El feed las agrupa de forma
# ambigua — "Asset Management" mezcla P/FRE (OWL, BAM), P/BV (OBDC) y hasta PER
# (Gladstone) — así que NO se puede clasificar el tipo automáticamente. La
# heurística es DEFENSIVA: detecta la familia y frena el sembrado-como-PER + marca
# para revisión; el usuario fija el tipo exacto. Se excluyen a propósito "credit
# services" (arrastraría Visa/Mastercard) y "stock exchanges" (arrastraría SPGI,
# MSCI…), todos PER legítimos. COIN (P/FCF) queda como excepción manual.
# Familias que NO se valoran por PER sobre EPS (EPS volátil por provisiones/
# mark-to-market) → P/BV·P/FRE·P/AFFO… La detección SOLO activa la marca
# `revisar_tipo_val` (no fija tipo), así que ampliarla es conservador.
_IND_CONTABLE = ("asset management", "reit", "banks", "bank", "insurance",
                 "capital markets", "mortgage", "credit services")
# Excepciones valoradas SÍ por PER pese a contener una palabra contable
# (corredores de seguros tipo Marsh/AON, bolsas/mercados tipo CME/ICE).
_IND_CONTABLE_EXCL = ("insurance brokers", "financial data", "stock exchanges")
# Respaldo cuando `industry` viene vacío (típico no-US): nombre/sector.
_NOMBRE_CONTABLE = ("REIT", "BDC", "MORTGAGE", "CAPITAL CORP", "BANCO", "BANCA")
_SECTOR_CONTABLE = ("financial services", "real estate")


def _clasificar_tipo_val(industria: str, sector: str, nombre_u: str,
                         eps: float | None, fcf_ps: float | None) -> tuple[str, str]:
    """Clasifica el TIPO de múltiplo por modelo de negocio (ADR-005, 2a), dentro
    del catálogo actual (PER/P_FCF/P_BV/P_FRE/SOTP). Devuelve (tipo, razón).
    Determinista: industria/sector + nombre + señal de EPS. El usuario puede
    override (queda fijado). P_S/P_AFFO/EV-EBITDA llegan en 2c."""
    def has(s: str, *ks: str) -> bool:
        return any(k in s for k in ks)

    # REIT / inmobiliario patrimonialista → P_BV (NAV; P_AFFO en 2c)
    if has(industria, "reit") or has(nombre_u, "REIT") or \
       (not industria and sector == "real estate"):
        return "P_BV", "REIT/inmobiliario — valor patrimonial (NAV)"
    # Gestoras de activos / alternativos (OWL, BAM, BX…) → P_FRE
    if has(industria, "asset management"):
        return "P_FRE", "gestora de activos — fee-related earnings"
    # Banca / seguros de balance (excl. corredores) → P_BV
    if (has(industria, "bank", "insurance") and not has(industria, "insurance brokers")) \
       or (not industria and (sector == "financial services"
                              or has(nombre_u, "BANCO", "BANCA"))):
        return "P_BV", "financiera de balance — valor contable"
    # Holdings / conglomerados → SOTP (suma de partes)
    if has(industria, "conglomerates") or has(nombre_u, "HOLDING", "HUTCHISON"):
        return "SOTP", "holding/conglomerado — suma de partes (NAV)"
    # EPS no representativo (≤0 o ausente) con FCF positivo → P_FCF
    if (eps is None or eps <= 0) and (fcf_ps is not None and fcf_ps > 0):
        return "P_FCF", "EPS no representativo; FCF positivo"
    return "PER", ""


# Banda del tilt de calidad sobre el múltiplo objetivo (Fase 2b). Modesto: la
# re-valoración por calidad es secundaria; el grueso del CAGR viene del
# crecimiento de la métrica. Los pares (comps IA) afinan esto on-demand.
_BANDA_CALIDAD = (Decimal("0.90"), Decimal("1.15"))
# Bandas de saneo del múltiplo objetivo por tipo (evita objetivos absurdos).
_BANDA_MULT = {"P_BV": (Decimal("0.4"), Decimal("5")),
               "P_FCF": (Decimal("5"), Decimal("35"))}


def _factor_calidad(f: dict) -> Decimal:
    """Tilt de calidad ∈ [0.90, 1.15] desde ROE + márgenes + crecimiento de
    ingresos (Fase 2b). Sin señales → 1.0 (neutro). Aproxima calidad ABSOLUTA;
    la calidad VS PARES llega con comps (on-demand)."""
    score = Decimal("0")
    roe = f.get("roe")
    if isinstance(roe, (int, float)):
        score += Decimal("0.06") if roe > 0.20 else Decimal("0.03") if roe > 0.12 else \
            Decimal("-0.05") if roe < 0.06 else Decimal("0")
    om = f.get("oper_margin")
    if isinstance(om, (int, float)):
        score += Decimal("0.05") if om > 0.25 else Decimal("0.02") if om > 0.12 else \
            Decimal("-0.04") if om < 0.05 else Decimal("0")
    rg = f.get("revenue_growth")
    if isinstance(rg, (int, float)):
        score += Decimal("0.06") if rg > 0.15 else Decimal("0.03") if rg > 0.06 else \
            Decimal("-0.05") if rg < 0 else Decimal("0")
    factor = Decimal("1") + score
    lo, hi = _BANDA_CALIDAD
    return max(lo, min(hi, factor))


def _crec_metrica_no_per(tipo: str, f: dict) -> float:
    """Crecimiento 4Y de la métrica no-PER (Fase 2b): P_BV vía crecimiento
    sostenible (ROE×retención), P_FCF vía crecimiento de ingresos. Acotado."""
    if tipo == "P_BV":
        roe = f.get("roe")
        if not isinstance(roe, (int, float)) or roe <= 0:
            return 0.0
        payout = f.get("payout")
        retencion = (1 - payout) if isinstance(payout, (int, float)) and 0 <= payout <= 1 else 0.6
        return max(0.0, min(0.12, roe * retencion))
    if tipo == "P_FCF":
        rg = f.get("revenue_growth")
        return max(-0.05, min(0.15, rg)) if isinstance(rg, (int, float)) else 0.0
    return 0.0


def _crecimiento_eps(eps_hist: list | None, forward_eps: float | None) -> float:
    """CAGR del BPA sobre la serie [histórico real + forward FY+1], acotado a
    `_BANDA_CAGR_EPS`. Incluir el forward como último punto captura algo del
    optimismo/pesimismo del próximo año sin extrapolar un solo año. Si no hay
    serie utilizable (≥2 puntos positivos) → 0% (proyección plana, conservadora)."""
    # Conservar el ÍNDICE temporal de cada punto: filtrar los BPA ≤ 0 sin
    # conservar la distancia en años inflaba el CAGR en cíclicas con años en
    # pérdidas ([5, −1, 6] salía 20% anual en vez de ~9,5% a 2 años) —
    # auditoría Cima 2026-06-11, D3.
    puntos = [(i, float(x)) for i, x in enumerate(eps_hist or [])
              if x is not None and float(x) > 0]
    if forward_eps is not None and float(forward_eps) > 0:
        puntos.append((len(eps_hist or []), float(forward_eps)))
    if len(puntos) < 2:
        return 0.0
    n = puntos[-1][0] - puntos[0][0]
    if n <= 0:
        return 0.0
    g = (puntos[-1][1] / puntos[0][1]) ** (1 / n) - 1
    lo, hi = _BANDA_CAGR_EPS
    return max(lo, min(hi, g))


# Banda del g_div sembrado desde DPS real: igual que la del derivado en
# `_calc_item` ([−5%, +20%]) — un recorte puntual o un dividendo inaugural
# no deben proyectarse 4 años tal cual.
_BANDA_G_DIV = (-0.05, 0.20)


def _crecimiento_dps(dps_hist: list | None) -> float | None:
    """CAGR del dividendo por acción anual real (oldest→newest, años completos),
    acotado a `_BANDA_G_DIV`. Pide ≥3 años con pago > 0 para no extrapolar un
    solo salto; None si no hay serie utilizable (el caller deriva o deja 0)."""
    puntos = [(i, float(x)) for i, x in enumerate(dps_hist or [])
              if x is not None and float(x) > 0]
    if len(puntos) < 3:
        return None
    n = puntos[-1][0] - puntos[0][0]
    if n <= 0:
        return None
    g = (puntos[-1][1] / puntos[0][1]) ** (1 / n) - 1
    lo, hi = _BANDA_G_DIV
    return max(lo, min(hi, g))


def _seed_estimacion(e: models.Estimacion, f: dict, c: dict | None) -> None:
    """Rellena (solo campos VACÍOS) una fila Estimacion desde consenso + fundamentales.
    Compartido por cartera y seguimiento. El consenso de EPS solo siembra
    multiplo/metrica si tipo_val=PER (P_FCF/P_BV/P_FRE quedan manuales). Heurística
    defensiva: si la industria es de métrica contable (gestoras/BDCs/REITs) y el
    usuario aún no ha confirmado el tipo, NO siembra como PER y marca para revisión."""
    # ¿Confirmó el usuario el tipo_val? Si sí, no re-marcamos (ver router.editar).
    prev: dict = {}
    if e.consenso_json:
        try:
            prev = json.loads(e.consenso_json) or {}
        except ValueError:
            prev = {}
    confirmado = bool(prev.get("tipo_confirmado"))
    # Campos que el usuario editó a mano (router.editar los registra). El prefill
    # RE-SIEMBRA los AUTO con dato fresco (arregla la rancidez) pero NUNCA pisa un
    # campo editado (auditoría 2026-06-18, 3D). Antes solo rellenaba si era None →
    # una vez sembrado, jamás se actualizaba.
    editado: set[str] = set(prev.get("editado", []))
    # Los fondos/ETF no se valoran por PER (su retorno = CAGR histórico, ver
    # `_enriquecer_etfs_historico`): nunca sembrar múltiplo/EPS sobre ellos.
    # La marca se INICIALIZA aquí por detección (auditoría Cima 2026-06-11,
    # A8: antes solo se re-persistía si ya era True y ningún código la
    # escribía la primera vez — la guarda era código muerto y un ETF con
    # trailingEps en yfinance se sembraba como PER).
    es_fondo_flag = bool(prev.get("es_fondo")) or _es_fondo(
        e.isin, f.get("nombre") or f.get("name") or f.get("shortName") or ""
    )

    industria = (f.get("industry") or "").lower()
    sector = (f.get("sector") or "").lower()
    nombre_u = (f.get("nombre") or f.get("name") or f.get("shortName") or e.isin or "").upper()

    eps = f.get("eps")                  # trailing (TTM)
    feps = f.get("forward_eps")         # forward FY+1
    eps_fiscal = f.get("eps_fiscal")    # último ejercicio fiscal cerrado (base)
    eps_hist = f.get("eps_hist")        # BPA real por año fiscal (oldest→newest)
    div = f.get("dividend")
    pe = f.get("pe")
    fcf_ps = f.get("fcf_ps")
    bvps = f.get("book_value_ps")

    # BPA base = último ejercicio fiscal real (respeta el año fiscal propio de
    # cada empresa); respaldo = trailing.
    base_eps = eps_fiscal if (eps_fiscal is not None and eps_fiscal > 0) else eps
    if "eps_actual" not in editado and base_eps is not None:
        e.eps_actual = Decimal(str(base_eps)).quantize(Decimal("0.0001"))

    # CLASIFICAR el tipo de múltiplo (ADR-005, 2a) y fijarlo si el usuario no lo
    # confirmó/editó y no es un fondo. Antes solo se MARCABA para revisar; ahora
    # se asigna el tipo correcto (overridable).
    razon_tipo = ""
    # Solo se reclasifica desde el DEFAULT (PER): un tipo no-PER ya presente lo
    # eligió alguien deliberadamente y se respeta (aunque no esté "confirmado").
    if (not confirmado and not es_fondo_flag and "tipo_val" not in editado
            and e.tipo_val == "PER"):
        tipo_sug, razon_tipo = _clasificar_tipo_val(industria, sector, nombre_u, eps, fcf_ps)
        if tipo_sug != "PER":
            e.tipo_val = tipo_sug

    # Sembrar métrica/múltiplo según el TIPO (respetando ediciones del usuario).
    if not es_fondo_flag:
        if e.tipo_val == "PER":
            # Múltiplo objetivo NORMALIZADO: min(forward de consenso, histórico
            # mediano), acotado a [5,45]. Respaldo no-US: forwardPE acotado.
            if "multiplo_objetivo" not in editado:
                mult, _ = _multiplo_consenso_hist(c)
                if mult is not None:
                    e.multiplo_objetivo = Decimal(str(mult)).quantize(Decimal("0.0001"))
                elif pe and pe > 0:
                    e.multiplo_objetivo = Decimal(str(max(5.0, min(45.0, pe)))).quantize(Decimal("0.0001"))
            # Métrica base 4A: EPS de consenso ~+4 o, sin consenso, proyección del
            # BPA base con CAGR real de la serie [histórico + forward].
            eps4 = c.get("eps_consenso_4y") if c else None
            if "metrica_base_4y" not in editado:
                if eps4 is not None and eps4 > 0:
                    e.metrica_base_4y = Decimal(str(eps4)).quantize(Decimal("0.0001"))
                elif base_eps is not None and base_eps > 0:
                    g = _crecimiento_eps(eps_hist, feps)
                    e.metrica_base_4y = Decimal(
                        str(float(base_eps) * (1 + g) ** 4)
                    ).quantize(Decimal("0.0001"))
        elif e.tipo_val in ("P_FCF", "P_BV"):
            # Fase 2b: proyectar la métrica 4Y (P_BV vía crecimiento sostenible,
            # P_FCF vía crecimiento de ingresos) y anclar el múltiplo objetivo al
            # ACTUAL ajustado por un factor de CALIDAD (ROE/márgenes/crecimiento).
            # Los pares (comps IA) afinan esto on-demand.
            actual_ps = fcf_ps if e.tipo_val == "P_FCF" else bvps
            mult_actual = f.get("p_fcf_actual") if e.tipo_val == "P_FCF" else f.get("price_to_book")
            if "metrica_base_4y" not in editado and actual_ps is not None and actual_ps > 0:
                g = _crec_metrica_no_per(e.tipo_val, f)
                e.metrica_base_4y = Decimal(str(float(actual_ps) * (1 + g) ** 4)).quantize(Decimal("0.0001"))
            if ("multiplo_objetivo" not in editado
                    and isinstance(mult_actual, (int, float)) and mult_actual > 0):
                lo, hi = _BANDA_MULT[e.tipo_val]
                obj = (Decimal(str(mult_actual)) * _factor_calidad(f))
                e.multiplo_objetivo = max(lo, min(hi, obj)).quantize(Decimal("0.0001"))
        # P_FRE / SOTP: métrica/múltiplo no derivables del feed → manuales (2b
        # por pares on-demand o el usuario).

    if "dividendo_share" not in editado and div is not None:
        e.dividendo_share = Decimal(str(div)).quantize(Decimal("0.000001"))

    # Divisa de la métrica = la de reporte del feed (= la del precio tras el fix
    # 1A). Permite reconciliar con la divisa del precio en _calc_item.
    if "metrica_divisa" not in editado and f.get("currency"):
        e.metrica_divisa = str(f.get("currency"))[:8]

    # g_div asistido: CAGR del DPS anual REAL (años completos) si el usuario
    # no lo ha fijado. Mejor base que el crecimiento implícito de la métrica
    # (capta política de dividendo, no de beneficios). Misma banda [−5%,+20%]
    # que el derivado; fondos fuera (su g_div es 0 por diseño).
    # g_div NO entra en el refresco 3D: es un juicio de política (no dato de
    # mercado que caduque) y hay decisión fijada de no pisar el del usuario
    # (test_seed_no_pisa_g_div_del_usuario). Se mantiene "rellena si está vacío".
    if e.crecimiento_div_pct is None and not es_fondo_flag:
        g_dps = _crecimiento_dps(f.get("dps_hist"))
        if g_dps is not None:
            e.crecimiento_div_pct = Decimal(str(g_dps)).quantize(Decimal("0.0001"))

    # Persistir referencias: consenso fresco + industria + marcas de estado.
    payload: dict = dict(c or {})
    if f.get("industry"):
        payload["industria"] = f.get("industry")
    if confirmado:
        payload["tipo_confirmado"] = True
    if es_fondo_flag:
        payload["es_fondo"] = True
    if razon_tipo:                    # por qué se clasificó este tipo (transparencia)
        payload["tipo_clasificado"] = razon_tipo
    else:
        payload.pop("revisar_tipo_val", None)   # ya no se usa (sustituido por clasificación)
    # Señales de calidad para la Fase 2b (prima/descuento de múltiplo vs pares).
    calidad = {k: f.get(k) for k in ("roe", "gross_margin", "oper_margin", "revenue_growth")
               if isinstance(f.get(k), (int, float))}
    if calidad:
        payload["calidad"] = calidad
    if editado:                       # preservar qué campos editó el usuario
        payload["editado"] = sorted(editado)
    e.consenso_json = json.dumps(payload) if payload else None


def prefill_seguimiento(db: Session, cartera_id: str, isin: str, ticker: str) -> None:
    """Autorrellena la estimación de una empresa en seguimiento, por ticker
    (consenso + fundamentales del símbolo directamente). También CALIENTA el
    precio: sin precio cacheado las lecturas (cache-only) no pueden calcular CAGR."""
    from app.services.precios import (
        consenso_simbolo, fundamentales_simbolo, precio_nativo_simbolo,
    )

    e = db.execute(
        select(models.Estimacion)
        .where(models.Estimacion.cartera_id == cartera_id)
        .where(models.Estimacion.isin == isin)
    ).scalars().first()
    nueva = e is None
    if nueva:
        e = models.Estimacion(cartera_id=cartera_id, isin=isin, tipo_val="PER")
    _seed_estimacion(e, fundamentales_simbolo(ticker) or {}, consenso_simbolo(ticker))
    if nueva:
        db.add(e)
    db.commit()
    # Refresca el precio del símbolo (con fallback IA si Yahoo/FMP fallan) →
    # la próxima lectura sí tendrá precio y por tanto CAGR.
    precio_nativo_simbolo(ticker, refrescar=True)


@dataclass
class AgregadoEstimaciones:
    yield_estimado_pct: Decimal | None
    cagr4_div_ponderado_pct: Decimal | None
    cobertura: Decimal     # fracción del valor de cartera con estimación válida


def agregado_cartera(
    db: Session, cartera_id: str, solo_estrategia: bool = False,
) -> AgregadoEstimaciones:
    """Yield estimado y CAGR4+Div ponderados por valor de mercado (EUR).

    Con `solo_estrategia=True` excluye las posiciones de bloques fuera de
    estrategia (colchón): la proyección IF aplica el retorno a `capital_if`
    (que las excluye) — mezclar bases sesgaba `anios_if` (auditoría D5)."""
    from app.services.precios import obtener_precios_eur

    precios_eur, _ = obtener_precios_eur(db, cartera_id)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}

    bloques_fuera: set[str] = set()
    if solo_estrategia:
        bloques_fuera = {
            b.id for b in db.execute(
                select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
            ).scalars() if not b.en_estrategia
        }

    total_valor = Decimal("0")
    valor_yield = Decimal("0")
    valor_cagr = Decimal("0")
    base_yield = Decimal("0")
    base_cagr = Decimal("0")
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if solo_estrategia and pos.bloque_id in bloques_fuera:
            continue
        est = estado_posicion(db, pos.id)
        cant = est["cantidad"]
        if cant <= 0:
            continue
        px = precios_eur.get(pos.isin)
        valor = (Decimal(str(px)) * cant) if px is not None else Decimal(str(est["coste_total_eur"]))
        total_valor += valor
        c = calcs.get(pos.isin)
        if c and c.div_yield_pct is not None:
            valor_yield += valor * c.div_yield_pct
            base_yield += valor
        if c and c.cagr4_div_pct is not None:
            valor_cagr += valor * c.cagr4_div_pct
            base_cagr += valor

    yield_est = (valor_yield / base_yield) if base_yield > 0 else None
    cagr_pond = (valor_cagr / base_cagr) if base_cagr > 0 else None
    cobertura = (base_cagr / total_valor) if total_valor > 0 else Decimal("0")
    return AgregadoEstimaciones(yield_est, cagr_pond, cobertura)


@dataclass
class BloqueAgg:
    cagr4_div_pct: Decimal | None       # CAGR4+Div ponderado por valor del bloque
    cobertura: Decimal | None           # fracción del valor del bloque con estimación válida
    n_con_estimacion: int               # posiciones del bloque con CAGR estimado


def agregado_por_bloque(db: Session, cartera_id: str) -> dict[str | None, BloqueAgg]:
    """CAGR4+Div ponderado por valor de mercado, AGRUPADO por bloque (misma
    ponderación que `agregado_cartera`). Clave = bloque_id (None = sin clasificar).
    Sirve para que el usuario vea qué bloque tira del retorno y estudie rotaciones
    dentro del bloque. La cobertura avisa si el CAGR se apoya en pocas estimaciones."""
    from app.services.precios import obtener_precios_eur

    precios_eur, _ = obtener_precios_eur(db, cartera_id)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}

    valor_total: dict[str | None, Decimal] = {}
    valor_cagr: dict[str | None, Decimal] = {}
    base_cagr: dict[str | None, Decimal] = {}
    n_con: dict[str | None, int] = {}
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        est = estado_posicion(db, pos.id)
        cant = est["cantidad"]
        if cant <= 0:
            continue
        bid = pos.bloque_id
        px = precios_eur.get(pos.isin)
        valor = (Decimal(str(px)) * cant) if px is not None else Decimal(str(est["coste_total_eur"]))
        valor_total[bid] = valor_total.get(bid, Decimal("0")) + valor
        c = calcs.get(pos.isin)
        if c and c.cagr4_div_pct is not None:
            valor_cagr[bid] = valor_cagr.get(bid, Decimal("0")) + valor * c.cagr4_div_pct
            base_cagr[bid] = base_cagr.get(bid, Decimal("0")) + valor
            n_con[bid] = n_con.get(bid, 0) + 1

    out: dict[str | None, BloqueAgg] = {}
    for bid, total in valor_total.items():
        bc = base_cagr.get(bid, Decimal("0"))
        cagr = (valor_cagr[bid] / bc) if bc > 0 else None
        cobertura = (bc / total) if total > 0 else None
        out[bid] = BloqueAgg(cagr, cobertura, n_con.get(bid, 0))
    return out
