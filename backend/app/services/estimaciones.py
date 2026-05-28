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
    cagr4_div_pct: Decimal | None
    notas: str | None
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
) -> EstimacionCalc:
    """Calcula una estimación (precio objetivo, CAGR4, yield…) a partir de la fila
    Estimacion + precio nativo. Compartido por cartera y seguimiento/watchlist."""
    tipo = e.tipo_val if e else "PER"
    eps = Decimal(str(e.eps_actual)) if e and e.eps_actual is not None else None
    mult = Decimal(str(e.multiplo_objetivo)) if e and e.multiplo_objetivo is not None else None
    base = Decimal(str(e.metrica_base_4y)) if e and e.metrica_base_4y is not None else None
    div = Decimal(str(e.dividendo_share)) if e and e.dividendo_share is not None else None

    precio_obj = (mult * base) if (mult is not None and base is not None) else None
    cagr4 = _cagr(precio_obj, precio, 4) if (precio_obj is not None and precio) else None
    yld = (div / precio) if (div is not None and precio and precio > 0) else None
    cagr4_div = (cagr4 + yld) if (cagr4 is not None and yld is not None) else cagr4
    crec = _cagr(base, eps, 4) if (base is not None and eps is not None) else None

    c: dict = {}
    if e and e.consenso_json:
        try:
            c = json.loads(e.consenso_json) or {}
        except ValueError:
            c = {}

    # Alerta del múltiplo. La marca defensiva (familia de métrica contable sin
    # confirmar) tiene prioridad: avisa de que NO se autocompletó y hay que fijar
    # el tipo_val. Si no, la alerta de divergencia consenso/histórico (solo PER).
    revisar = c.get("revisar_tipo_val")
    if revisar:
        alerta = f"posible métrica contable ({revisar}?) — fija el tipo_val, no autocompletado"
    elif tipo == "PER":
        alerta = _multiplo_consenso_hist(c)[1]
    else:
        alerta = None

    return EstimacionCalc(
        isin=isin, nombre=nombre, tipo_val=tipo, divisa=divisa,
        precio_actual=precio, eps_actual=eps, multiplo_objetivo=mult,
        metrica_base_4y=base, dividendo_share=div, precio_objetivo=precio_obj,
        crecimiento_pct=crec, cagr4_pct=cagr4, div_yield_pct=yld,
        cagr4_div_pct=cagr4_div, notas=(e.notas if e else None),
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
    from app.services.precios import precios_nativos

    natives = precios_nativos(db, cartera_id)
    filas = _filas_estimacion(db, cartera_id)
    out: list[EstimacionCalc] = []
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        precio, divisa = natives.get(pos.isin, (None, None))
        out.append(_calc_item(pos.isin, pos.nombre or pos.isin,
                              filas.get(pos.isin), precio, divisa))
    _enriquecer_etfs_historico(db, cartera_id, out)
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


def _enriquecer_etfs_historico(db: Session, cartera_id: str,
                               calcs: list[EstimacionCalc]) -> None:
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
        c.cagr4_div_pct = cagr_precio + (yld or Decimal("0"))
        c.notas = ((c.notas + " · ") if c.notas else "") + "CAGR histórico (precio) + yield"


def calcular_estimaciones_seguimiento(db: Session, cartera_id: str) -> list[EstimacionCalc]:
    """Estimaciones de las empresas en seguimiento (watchlist). Precio nativo por
    ticker (no por resolución ISIN, ya conocemos el símbolo)."""
    from app.services.precios import precio_nativo_simbolo

    filas = _filas_estimacion(db, cartera_id)
    out: list[EstimacionCalc] = []
    for s in db.execute(
        select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
    ).scalars():
        pv = precio_nativo_simbolo(s.ticker)
        precio, divisa = (pv[0], pv[1]) if pv else (None, s.divisa)
        out.append(_calc_item(s.isin, s.nombre or s.ticker,
                              filas.get(s.isin), precio, divisa))
    out.sort(key=lambda x: x.nombre.lower())
    return out


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
_IND_CONTABLE = ("asset management", "reit")


def _crecimiento_eps(eps_hist: list | None, forward_eps: float | None) -> float:
    """CAGR del BPA sobre la serie [histórico real + forward FY+1], acotado a
    `_BANDA_CAGR_EPS`. Incluir el forward como último punto captura algo del
    optimismo/pesimismo del próximo año sin extrapolar un solo año. Si no hay
    serie utilizable (≥2 puntos positivos) → 0% (proyección plana, conservadora)."""
    serie = [float(x) for x in (eps_hist or []) if x is not None and float(x) > 0]
    if forward_eps is not None and float(forward_eps) > 0:
        serie = serie + [float(forward_eps)]
    if len(serie) < 2:
        return 0.0
    n = len(serie) - 1
    g = (serie[-1] / serie[0]) ** (1 / n) - 1
    lo, hi = _BANDA_CAGR_EPS
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
    # Los fondos/ETF no se valoran por PER (su retorno = CAGR histórico, ver
    # `_enriquecer_etfs_historico`): nunca sembrar múltiplo/EPS sobre ellos.
    es_fondo_flag = bool(prev.get("es_fondo"))

    # Familia de métrica contable: el feed no distingue P/BV/P/FRE/PER dentro de
    # ella, así que frenamos el sembrado-como-PER en vez de plantar EPS donde va
    # NAV/FRE. Solo aplica si el tipo no está confirmado.
    industria = (f.get("industry") or "").lower()
    contable = (not confirmado) and any(k in industria for k in _IND_CONTABLE)

    eps = f.get("eps")                  # trailing (TTM)
    feps = f.get("forward_eps")         # forward FY+1
    eps_fiscal = f.get("eps_fiscal")    # último ejercicio fiscal cerrado (base)
    eps_hist = f.get("eps_hist")        # BPA real por año fiscal (oldest→newest)
    div = f.get("dividend")
    pe = f.get("pe")

    # BPA base = último ejercicio fiscal real (respeta el año fiscal propio de
    # cada empresa); respaldo = trailing.
    base_eps = eps_fiscal if (eps_fiscal is not None and eps_fiscal > 0) else eps
    if e.eps_actual is None and base_eps is not None:
        e.eps_actual = Decimal(str(base_eps)).quantize(Decimal("0.0001"))

    if e.tipo_val == "PER" and not contable and not es_fondo_flag:
        # Múltiplo objetivo NORMALIZADO: min(forward de consenso, histórico mediano),
        # acotado a [5,45]. Respaldo no-US: forwardPE acotado.
        if e.multiplo_objetivo is None:
            mult, _ = _multiplo_consenso_hist(c)
            if mult is not None:
                e.multiplo_objetivo = Decimal(str(mult)).quantize(Decimal("0.0001"))
            elif pe and pe > 0:
                e.multiplo_objetivo = Decimal(str(max(5.0, min(45.0, pe)))).quantize(Decimal("0.0001"))

        # Métrica base 4A. Con consenso de analistas → EPS del año ~+4. Sin
        # consenso → proyectar el BPA base con un CAGR REAL: el de la serie
        # [BPA histórico + forward FY+1], acotado a banda (quita el ruido de
        # años atípicos y captura algo del optimismo/pesimismo próximo). NO se
        # extrapola un crecimiento de un solo año.
        eps4 = c.get("eps_consenso_4y") if c else None
        if e.metrica_base_4y is None:
            if eps4 is not None and eps4 > 0:
                e.metrica_base_4y = Decimal(str(eps4)).quantize(Decimal("0.0001"))
            elif base_eps is not None and base_eps > 0:
                g = _crecimiento_eps(eps_hist, feps)
                e.metrica_base_4y = Decimal(
                    str(float(base_eps) * (1 + g) ** 4)
                ).quantize(Decimal("0.0001"))

    if e.dividendo_share is None and div is not None:
        e.dividendo_share = Decimal(str(div)).quantize(Decimal("0.000001"))

    # Persistir referencias: consenso fresco + industria + marcas de estado.
    payload: dict = dict(c or {})
    if f.get("industry"):
        payload["industria"] = f.get("industry")
    if confirmado:
        payload["tipo_confirmado"] = True
    if es_fondo_flag:
        payload["es_fondo"] = True
    if contable and e.tipo_val == "PER":
        payload["revisar_tipo_val"] = "P/BV·P/FRE"
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


def agregado_cartera(db: Session, cartera_id: str) -> AgregadoEstimaciones:
    """Yield estimado y CAGR4+Div ponderados por valor de mercado (EUR)."""
    from app.services.precios import obtener_precios_eur

    precios_eur, _ = obtener_precios_eur(db, cartera_id)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}

    total_valor = Decimal("0")
    valor_yield = Decimal("0")
    valor_cagr = Decimal("0")
    base_yield = Decimal("0")
    base_cagr = Decimal("0")
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
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
