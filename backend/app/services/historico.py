"""Histórico de cierres mensuales y evolución de la cartera (ADR-004).

Caché GLOBAL compartida entre usuarios (`precios_mensuales` / `fx_mensuales`):
dato de mercado público, una descarga sirve a todos. La valoración por cartera
se calcula al vuelo con las transacciones del usuario.

- `poblar_historico(db, cartera_id)` — cuerpo del job: descarga los meses que
  falten para los valores de la cartera y los cachea en las tablas globales.
- `serie_cartera` / `serie_posicion` — leen del caché y valoran mes a mes.
- `meses_faltantes` — qué (símbolo, mes) hay que bajar todavía.

Cierres en divisa NATIVA y CRUDOS (sin ajustar por splits): la cantidad
histórica ya refleja las acciones reales de cada momento (ver ADR-004).
"""
from __future__ import annotations

import datetime as _dt
import warnings
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services import precios
from app.services.fifo import cantidad_a_fecha, estado_posicion


# ── helpers de calendario ───────────────────────────────────────────────────

def _ym(d: _dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _hoy() -> _dt.date:
    return _dt.date.today()


def _fin_de_mes(anio_mes: str) -> _dt.date:
    y, m = (int(x) for x in anio_mes.split("-"))
    if m == 12:
        return _dt.date(y, 12, 31)
    return _dt.date(y, m + 1, 1) - _dt.timedelta(days=1)


def _meses_entre(inicio: str, fin: str) -> list[str]:
    """Lista de 'YYYY-MM' de `inicio` a `fin` inclusive."""
    yi, mi = (int(x) for x in inicio.split("-"))
    yf, mf = (int(x) for x in fin.split("-"))
    out: list[str] = []
    y, m = yi, mi
    while (y, m) <= (yf, mf):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


# ── franja de tenencia por cartera ───────────────────────────────────────────

@dataclass
class _PosInfo:
    posicion: models.Posicion
    primer_mes: str
    ultimo_mes: str       # 'now' = mes actual (sigue en cartera)


def _posiciones_con_franja(db: Session, cartera_id: str) -> list[_PosInfo]:
    """Posiciones que ALGUNA vez se tuvieron, con su franja [primer BUY, último
    mes con cantidad>0]. Si sigue abierta hoy → hasta el mes actual."""
    posiciones = db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars().all()
    hoy = _hoy()
    mes_actual = _ym(hoy)
    out: list[_PosInfo] = []
    for pos in posiciones:
        txs = db.execute(
            select(models.Transaccion.fecha)
            .where(models.Transaccion.posicion_id == pos.id)
            .where(models.Transaccion.estado == "confirmada")
            .order_by(models.Transaccion.fecha)
        ).scalars().all()
        if not txs:
            continue
        primer_mes = _ym(txs[0])
        abierta_hoy = estado_posicion(db, pos.id)["cantidad"] > 0
        if abierta_hoy:
            ultimo_mes = mes_actual
        else:
            # último mes (entre el primero y hoy) en que aún quedaba cantidad>0
            ultimo_mes = primer_mes
            for ym in _meses_entre(primer_mes, mes_actual):
                if cantidad_a_fecha(db, pos.id, _fin_de_mes(ym)) > 0:
                    ultimo_mes = ym
        out.append(_PosInfo(pos, primer_mes, ultimo_mes))
    return out


# ── fetch de mercado (yfinance) — aislado para poder mockear en tests ─────────

def _fetch_cierres_mensuales(
    simbolo: str, inicio: str, fin: str
) -> list[tuple[str, Decimal, str]]:
    """[(anio_mes, cierre_crudo, divisa)] de `inicio` a `fin` (inclusive).
    Cierre EOM = Close de la barra mensual de yfinance, SIN ajustar por splits."""
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        start = f"{inicio}-01"
        fin_d = _fin_de_mes(fin) + _dt.timedelta(days=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = yf.Ticker(simbolo)
            hist = t.history(start=start, end=fin_d.isoformat(),
                             interval="1mo", auto_adjust=False)
            if hist is None or hist.empty:
                return []
            cur = (t.history_metadata or {}).get("currency") or "USD"
            out: list[tuple[str, Decimal, str]] = []
            for idx, val in hist["Close"].dropna().items():
                out.append((f"{idx.year:04d}-{idx.month:02d}", Decimal(str(val)), cur))
            return out
    except Exception:
        return []


def _fetch_fx_mensual(base: str, inicio: str, fin: str) -> list[tuple[str, Decimal]]:
    """[(anio_mes, rate_eur)] = EUR por 1 unidad de `base`. EUR → 1."""
    if base == "EUR":
        return [(ym, Decimal("1")) for ym in _meses_entre(inicio, fin)]
    try:
        import yfinance as yf  # type: ignore[import-not-found]
        start = f"{inicio}-01"
        fin_d = _fin_de_mes(fin) + _dt.timedelta(days=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t = yf.Ticker(f"EUR{base}=X")          # base por EUR
            hist = t.history(start=start, end=fin_d.isoformat(), interval="1mo")
            if hist is None or hist.empty:
                return []
            out: list[tuple[str, Decimal]] = []
            for idx, val in hist["Close"].dropna().items():
                v = Decimal(str(val))
                if v > 0:
                    out.append((f"{idx.year:04d}-{idx.month:02d}", Decimal("1") / v))
            return out
    except Exception:
        return []


# ── backfill (cuerpo del job) ────────────────────────────────────────────────

def meses_faltantes(db: Session, cartera_id: str) -> int:
    """Cuántos (símbolo, mes) de la cartera faltan en el caché global. 0 = al día."""
    return _planificar(db, cartera_id)[0]


def _planificar(db: Session, cartera_id: str):
    """Devuelve (n_faltan, plan_precios, bases) sin tocar la red.
    plan_precios: {simbolo: (divisa_hint, [meses_que_faltan])}."""
    infos = _posiciones_con_franja(db, cartera_id)
    isines = list({i.posicion.isin for i in infos})
    sim_por_isin = precios.resolver_simbolos(isines)

    # meses requeridos por símbolo (unión de las franjas de las posiciones)
    req: dict[str, set[str]] = {}
    for info in infos:
        sim = sim_por_isin.get(info.posicion.isin)
        if not sim:
            continue
        req.setdefault(sim, set()).update(_meses_entre(info.primer_mes, info.ultimo_mes))

    n_faltan = 0
    plan: dict[str, list[str]] = {}
    for sim, meses in req.items():
        ya = set(db.execute(
            select(models.PrecioMensual.anio_mes)
            .where(models.PrecioMensual.simbolo == sim)
        ).scalars())
        faltan = sorted(meses - ya)
        if faltan:
            plan[sim] = faltan
            n_faltan += len(faltan)
    return n_faltan, plan, req


def poblar_historico(db: Session, cartera_id: str, _isin: str = "") -> dict:
    """Cuerpo del job: descarga y cachea los cierres mensuales que falten para
    los valores de la cartera, más el FX mensual de sus divisas. Idempotente."""
    _, plan, _req = _planificar(db, cartera_id)

    divisas_meses: dict[str, set[str]] = {}   # base → meses a asegurar
    insertados = 0
    for sim, faltan in plan.items():
        cierres = _fetch_cierres_mensuales(sim, faltan[0], faltan[-1])
        faltan_set = set(faltan)
        for anio_mes, cierre, divisa in cierres:
            if anio_mes not in faltan_set:
                continue
            db.add(models.PrecioMensual(
                simbolo=sim, anio_mes=anio_mes, cierre=cierre,
                divisa=divisa, fuente="yfinance",
            ))
            insertados += 1
            base, _esc = precios.base_y_escala(divisa)
            divisas_meses.setdefault(base, set()).add(anio_mes)
    if insertados:
        db.commit()

    # FX mensual de las divisas tocadas (las que falten)
    fx_insertados = 0
    for base, meses in divisas_meses.items():
        if base == "EUR":
            continue
        ya = set(db.execute(
            select(models.FxMensual.anio_mes)
            .where(models.FxMensual.divisa == base)
        ).scalars())
        faltan = sorted(meses - ya)
        if not faltan:
            continue
        for anio_mes, rate in _fetch_fx_mensual(base, faltan[0], faltan[-1]):
            if anio_mes in set(faltan):
                db.add(models.FxMensual(divisa=base, anio_mes=anio_mes, rate_eur=rate))
                fx_insertados += 1
    if fx_insertados:
        db.commit()
    return {"precios": insertados, "fx": fx_insertados}


# ── series valoradas (lectura) ───────────────────────────────────────────────

@dataclass
class PuntoSerie:
    anio_mes: str
    valor_eur: Decimal
    aportado_eur: Decimal
    completo: bool = True           # False si faltó el cierre de algún valor poseído


@dataclass
class SerieEvolucion:
    puntos: list[PuntoSerie] = field(default_factory=list)
    meses_pendientes: int = 0       # backfill aún en curso → la serie crecerá


def _cierre_cache(db: Session, simbolo: str) -> dict[str, tuple[Decimal, str]]:
    return {
        r.anio_mes: (r.cierre, r.divisa)
        for r in db.execute(
            select(models.PrecioMensual).where(models.PrecioMensual.simbolo == simbolo)
        ).scalars()
    }


def _fx_cache(db: Session) -> dict[tuple[str, str], Decimal]:
    return {
        (r.divisa, r.anio_mes): r.rate_eur
        for r in db.execute(select(models.FxMensual)).scalars()
    }


def _factor_eur(divisa: str, anio_mes: str, fx: dict[tuple[str, str], Decimal]) -> Decimal | None:
    base, escala = precios.base_y_escala(divisa)
    if base == "EUR":
        return escala
    rate = fx.get((base, anio_mes))
    return rate * escala if rate is not None else None


def _aportado_hasta(db: Session, cartera_id: str, fin: _dt.date) -> Decimal:
    """Capital neto invertido acumulado a `fin`: Σ compras − Σ ventas (EUR)."""
    total = Decimal("0")
    txs = db.execute(
        select(models.Transaccion.tipo, models.Transaccion.importe_eur)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.fecha <= fin)
    ).all()
    for tipo, importe in txs:
        if tipo == "BUY":
            total += importe
        elif tipo == "SELL":
            total -= importe
    return total


def serie_cartera(db: Session, cartera_id: str) -> SerieEvolucion:
    """Evolución mensual de la cartera: valor de mercado (EUR) a cierre de cada
    mes y capital neto aportado acumulado. Lee del caché global (no descarga)."""
    infos = _posiciones_con_franja(db, cartera_id)
    if not infos:
        return SerieEvolucion()
    inicio = min(i.primer_mes for i in infos)
    fin = _ym(_hoy())
    meses = _meses_entre(inicio, fin)

    sim_por_isin = precios.resolver_simbolos([i.posicion.isin for i in infos])
    cierres = {sim: _cierre_cache(db, sim) for sim in set(sim_por_isin.values())}
    fx = _fx_cache(db)

    puntos: list[PuntoSerie] = []
    for ym in meses:
        eom = _fin_de_mes(ym)
        valor = Decimal("0")
        completo = True
        for info in infos:
            q = cantidad_a_fecha(db, info.posicion.id, eom)
            if q <= 0:
                continue
            sim = sim_por_isin.get(info.posicion.isin)
            par = cierres.get(sim, {}).get(ym) if sim else None
            if par is None:
                completo = False
                continue
            cierre, divisa = par
            fac = _factor_eur(divisa, ym, fx)
            if fac is None:
                completo = False
                continue
            valor += q * cierre * fac
        aportado = _aportado_hasta(db, cartera_id, eom)
        puntos.append(PuntoSerie(ym, valor, aportado, completo))

    return SerieEvolucion(puntos=puntos, meses_pendientes=meses_faltantes(db, cartera_id))


def valor_cartera_mes(
    db: Session, cartera_id: str, anio_mes: str
) -> tuple[Decimal | None, bool]:
    """Valor de mercado EUR de la cartera al CIERRE de `anio_mes`, con los
    cierres mensuales cacheados. Devuelve (valor, completo): valor None si no
    había posiciones ese mes; `completo` False si faltó el cierre de algún valor
    poseído. Lee del caché global (no descarga)."""
    # Fast path: sin NINGÚN cierre cacheado aún (backfill no ejecutado) no hay
    # nada que valorar → evita resolver símbolos (red) en cada informe.
    if db.execute(select(models.PrecioMensual.id).limit(1)).first() is None:
        return None, True
    infos = _posiciones_con_franja(db, cartera_id)
    if not infos:
        return None, True
    sim_por_isin = precios.resolver_simbolos([i.posicion.isin for i in infos])
    cierres = {sim: _cierre_cache(db, sim) for sim in set(sim_por_isin.values())}
    fx = _fx_cache(db)
    eom = _fin_de_mes(anio_mes)

    valor = Decimal("0")
    completo = True
    alguna = False
    for info in infos:
        q = cantidad_a_fecha(db, info.posicion.id, eom)
        if q <= 0:
            continue
        alguna = True
        sim = sim_por_isin.get(info.posicion.isin)
        par = cierres.get(sim, {}).get(anio_mes) if sim else None
        if par is None:
            completo = False
            continue
        cierre, divisa = par
        fac = _factor_eur(divisa, anio_mes, fx)
        if fac is None:
            completo = False
            continue
        valor += q * cierre * fac
    return (valor if alguna else None), completo


def serie_posicion(db: Session, cartera_id: str, isin: str) -> SerieEvolucion:
    """Evolución mensual del valor de mercado (EUR) de UNA posición."""
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalar_one_or_none()
    if pos is None:
        return SerieEvolucion()
    infos = [i for i in _posiciones_con_franja(db, cartera_id) if i.posicion.id == pos.id]
    if not infos:
        return SerieEvolucion()
    info = infos[0]
    sim = precios.resolver_simbolos([isin]).get(isin)
    cierres = _cierre_cache(db, sim) if sim else {}
    fx = _fx_cache(db)

    puntos: list[PuntoSerie] = []
    for ym in _meses_entre(info.primer_mes, _ym(_hoy())):
        eom = _fin_de_mes(ym)
        q = cantidad_a_fecha(db, pos.id, eom)
        if q <= 0:
            puntos.append(PuntoSerie(ym, Decimal("0"), Decimal("0"), True))
            continue
        par = cierres.get(ym)
        if par is None:
            puntos.append(PuntoSerie(ym, Decimal("0"), Decimal("0"), False))
            continue
        cierre, divisa = par
        fac = _factor_eur(divisa, ym, fx)
        valor = q * cierre * fac if fac is not None else Decimal("0")
        puntos.append(PuntoSerie(ym, valor, Decimal("0"), fac is not None))
    return SerieEvolucion(puntos=puntos, meses_pendientes=meses_faltantes(db, cartera_id))
