"""Dashboard agregado — la pantalla Resumen ("cockpit de decisión").

Reúne, con UN solo fetch de precios (cacheado), lo necesario para que el
usuario sepa de un vistazo: cómo va, cómo está compuesta su cartera, qué rinde
y qué hacer. Cada bloque navega a su sección en el frontend.

[YA] = construible hoy. [F2] = yield estimado / CAGR potencial (necesitan la
parte de Estimaciones); se devuelven nulos para reservar su sitio en la UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import estado_posicion


_TARGET_IF = Decimal("300000")
_RETORNO_IF = Decimal("0.07")   # supuesto anual para estimar años a IF


@dataclass
class CompBloque:
    nombre: str
    categoria_base: str
    valor_eur: Decimal
    peso: Decimal
    cagr4_div_pct: Decimal | None = None    # CAGR4+Div proyectado del bloque
    cobertura: Decimal | None = None        # fracción del valor del bloque con estimación


@dataclass
class PosicionPeso:
    nombre: str
    isin: str
    categoria_base: str | None       # bloque al que pertenece (para colorear la barra)
    valor_eur: Decimal               # valor de mercado
    peso: Decimal                    # fracción sobre el total invertido (mercado)


@dataclass
class PasoResumen:
    isin: str
    nombre: str
    decision: str
    prioridad: str


@dataclass
class OpcionRiesgo:
    simbolo: str
    tipo_op: str
    strike: str
    vencimiento: str
    dias_a_vencer: int | None
    moneyness: str | None      # 'ITM' | 'OTM' | None (sin precio subyacente)
    es_corta: bool
    riesgo_ejercicio: bool     # corta + ITM + vence pronto


@dataclass
class DashboardResultado:
    anio: int
    fecha_calculo: date
    # ¿Cómo voy?
    capital_mercado_eur: Decimal
    gp_no_realizada_eur: Decimal
    gp_no_realizada_pct: Decimal
    liquidez_eur: Decimal
    progreso_if_pct: Decimal
    anios_if: Decimal | None
    retorno_if_pct: Decimal = Decimal("0.07")    # rentabilidad supuesta en la proyección
    # ¿Cómo está compuesta?
    composicion: list[CompBloque] = field(default_factory=list)
    posiciones_peso: list[PosicionPeso] = field(default_factory=list)   # por acción/ETF, peso desc
    # ¿Qué rinde?
    yield_actual_pct: Decimal = Decimal("0")
    dividendos_brutos_anio: Decimal = Decimal("0")
    yield_estimado_pct: Decimal | None = None       # estimaciones
    cagr_anual_pct: Decimal | None = None            # CAGR4+Div ponderado (anual)
    retorno_5y_pct: Decimal | None = None            # acumulado a 5 años
    # ¿Qué hago?
    proximos_pasos: list[PasoResumen] = field(default_factory=list)
    gp_realizada_anio: Decimal = Decimal("0")
    perdidas_por_aflorar: Decimal = Decimal("0")
    compensable_ahora: Decimal = Decimal("0")
    perdida_a_arrastrar: Decimal = Decimal("0")
    opciones_riesgo: list[OpcionRiesgo] = field(default_factory=list)
    opciones_proximas_vencer: int = 0
    opciones_itm: int = 0


def _anios_a_if(
    capital: Decimal, aportacion_anual: Decimal,
    objetivo: Decimal = _TARGET_IF, retorno: Decimal | None = None,
) -> Decimal | None:
    """Años (FRACCIONADOS) hasta el objetivo. Delega en `proyeccion.anios_hasta`
    (fuente única compartida con el retorno requerido del onboarding)."""
    from app.services import proyeccion
    r = float(retorno if retorno is not None else _RETORNO_IF)
    return proyeccion.anios_hasta(capital, aportacion_anual, objetivo, r)


def capital_en_estrategia_eur(db: Session, cartera_id: str) -> Decimal:
    """Valor de MERCADO de las posiciones abiertas DENTRO de la estrategia IF
    (excluye colchón y bloques fuera de estrategia). Misma base que `capital_if`
    de la proyección del dashboard — la usa el onboarding para el retorno
    requerido, de modo que ambas cuentas parten del mismo capital."""
    from app.services.precios import obtener_precios_eur

    precios, _ = obtener_precios_eur(db, cartera_id)
    en_estrat = {b.id: b.en_estrategia for b in db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)).scalars()}
    total = Decimal("0")
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        est = estado_posicion(db, pos.id)
        cant = est["cantidad"]
        if cant <= 0:
            continue
        px = precios.get(pos.isin)
        valor = (px * cant) if px is not None else Decimal(str(est["coste_total_eur"]))
        if pos.bloque_id is None or en_estrat.get(pos.bloque_id, True):
            total += valor
    return total


def _venc_to_date(venc: str) -> date | None:
    s = (venc or "").strip()
    for fmt in ("%d%b%y", "%d%b%Y", "%Y-%m-%d", "%d/%m/%Y"):
        for cand in (s, s.title(), s.capitalize()):   # "19JUN26" → "19Jun26" para %b
            try:
                return datetime.strptime(cand, fmt).date()
            except (ValueError, AttributeError):
                continue
    return None


def calcular_dashboard(db: Session, cartera_id: str) -> DashboardResultado:
    from app.services.bloques import calcular_distribucion
    from app.services.fiscal import calcular_fiscal
    from app.services.fiscal_dividendos import calcular_dividendos
    from app.services.fiscal_optimizador import calcular_optimizador
    from app.services.fiscal_opciones import calcular_opciones
    from app.services.liquidez import calcular_liquidez
    from app.services.aportaciones import aportaciones_por_anio
    from app.services.plan import listar_pasos
    from app.services.precios import _precio_y_divisa, obtener_precios_eur

    anio = date.today().year
    precios, _no = obtener_precios_eur(db, cartera_id)   # cacheado

    # ── ¿Cómo voy? ──────────────────────────────────────────────────────
    # Bloques FUERA de la estrategia IF (Colchón por defecto, y los que el
    # usuario saque, p.ej. cripto a largo): NO entran en el progreso hacia la IF.
    # El KPI "Invertido" (capital_mercado) sí los incluye; el "capital IF" no.
    # Una posición sin bloque (None) cuenta como en estrategia.
    bloques_cartera = list(db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
    ).scalars())
    en_estrat_por_bloque = {b.id: b.en_estrategia for b in bloques_cartera}
    cat_por_bloque = {b.id: b.categoria_base for b in bloques_cartera}
    capital_coste = Decimal("0")
    capital_mercado = Decimal("0")
    capital_if = Decimal("0")   # invertido en estrategia (excluye colchón y liquidez)
    pos_valores: list[tuple[str, str, str | None, Decimal]] = []   # (nombre, isin, cat, valor)
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        est = estado_posicion(db, pos.id)
        cant = est["cantidad"]
        if cant <= 0:
            continue
        coste = Decimal(str(est["coste_total_eur"]))
        px = precios.get(pos.isin)
        valor = (px * cant) if px is not None else coste
        capital_coste += coste
        capital_mercado += valor
        pos_valores.append((pos.nombre or pos.isin, pos.isin,
                            cat_por_bloque.get(pos.bloque_id), valor))
        if pos.bloque_id is None or en_estrat_por_bloque.get(pos.bloque_id, True):
            capital_if += valor
    gp_no_real = capital_mercado - capital_coste
    gp_no_real_pct = (gp_no_real / capital_coste) if capital_coste > 0 else Decimal("0")

    liquidez = calcular_liquidez(db, cartera_id).total_disponible
    # Objetivo IF configurable por cartera (default 300k).
    _c = db.get(models.Cartera, cartera_id)
    objetivo_if = (Decimal(str(_c.objetivo_if_eur))
                   if _c and _c.objetivo_if_eur else _TARGET_IF)
    # Progreso/años a IF: SOLO sobre el capital invertido en estrategia
    # (capital_if). Excluye el colchón (Bloque F) y toda la liquidez —la
    # liquidez es munición, no avance; el colchón está fuera de la estrategia IF.
    progreso = (capital_if / objetivo_if) if objetivo_if > 0 else Decimal("0")
    # Aportación anual para la proyección: la PREVISTA configurada (mensual×12)
    # si existe; si no, las aportaciones reales del año en curso como ritmo.
    prevista_mensual = Decimal(str(_c.aportacion_mensual_eur)) if _c and _c.aportacion_mensual_eur else Decimal("0")
    if prevista_mensual > 0:
        aportacion = prevista_mensual * 12
    else:
        real = aportaciones_por_anio(db, cartera_id).get(anio, Decimal("0"))
        aportacion = real if real > 0 else Decimal("0")
    # Rentabilidad para la proyección: la PROYECTADA de la cartera (CAGR4+Div
    # ponderado de Estimaciones); si no hay (estimaciones sin curar), 7% por
    # defecto. Acotada a [0, 25%] para que estimaciones basura no la disparen.
    from app.services.estimaciones import agregado_cartera
    agg = agregado_cartera(db, cartera_id)
    cagr_anual = agg.cagr4_div_ponderado_pct
    retorno_if = _RETORNO_IF
    if cagr_anual is not None and cagr_anual > 0:
        retorno_if = min(Decimal(str(cagr_anual)), Decimal("0.25"))
    anios = _anios_a_if(capital_if, aportacion, objetivo_if, retorno_if)

    # ── ¿Cómo está compuesta? ───────────────────────────────────────────
    from app.services import bloques as bloques_svc
    from app.services.estimaciones import agregado_por_bloque

    dist = calcular_distribucion(db, cartera_id)
    aggs = agregado_por_bloque(db, cartera_id)   # CAGR4+Div proyectado por bloque

    def _agg(bloque_id: str):  # type: ignore[no-untyped-def]
        return aggs.get(None if bloque_id == bloques_svc.SIN_CLASIFICAR_ID else bloque_id)

    composicion = [
        CompBloque(b.nombre, b.categoria_base, b.valor_eur, b.peso_actual,
                   cagr4_div_pct=(a.cagr4_div_pct if (a := _agg(b.id)) else None),
                   cobertura=(a.cobertura if (a := _agg(b.id)) else None))
        for b in dist.bloques
    ]
    posiciones_peso = sorted(
        (PosicionPeso(n, i, cat, v,
                      (v / capital_mercado) if capital_mercado > 0 else Decimal("0"))
         for (n, i, cat, v) in pos_valores),
        key=lambda p: p.valor_eur, reverse=True,
    )

    # ── ¿Qué rinde? ─────────────────────────────────────────────────────
    dv = calcular_dividendos(db, cartera_id, anio)
    # Neto de dividendos = bruto − retención en origen (extranjera) − retención
    # española (que Cuádrate ahora reporta aparte en ret_es_total).
    div_neto = dv.bruto_total - dv.ret_origen_total - dv.ret_es_total
    yield_actual = (div_neto / capital_mercado) if capital_mercado > 0 else Decimal("0")

    # Estimaciones (Fase 2): yield estimado + CAGR4+Div ponderado (ya calculado
    # arriba como `agg`/`cagr_anual` para la proyección de años a IF).
    retorno_5y = (
        Decimal(str((1 + float(cagr_anual)) ** 5 - 1)) if cagr_anual is not None else None
    )

    # ── ¿Qué hago? — plan ───────────────────────────────────────────────
    pasos_activos = [
        p for p in listar_pasos(db, cartera_id)
        if p.estado in ("PENDIENTE", "EN_CURSO")
    ][:3]
    nombres = {
        p.isin: (p.nombre or p.isin) for p in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars()
    }
    proximos = [
        PasoResumen(p.isin, nombres.get(p.isin, p.isin), p.decision, p.prioridad)
        for p in pasos_activos
    ]

    # ── ¿Qué hago? — fiscal (reusa optimizador con precios ya fetchados) ─
    opt = calcular_optimizador(db, cartera_id, anio, precios=precios, no_resueltos=[])

    # ── ¿Qué hago? — opciones en riesgo ─────────────────────────────────
    opciones_riesgo: list[OpcionRiesgo] = []
    prox_vencer = 0
    n_itm = 0
    hoy = date.today()
    op = calcular_opciones(db, cartera_id, anio)
    cache_sub: dict[str, Decimal | None] = {}
    for c in op.por_contrato:
        n_abiertos = int(c.get("n_net_abiertos", 0) or 0)
        if n_abiertos == 0:
            continue   # solo contratos abiertos
        venc = str(c.get("vencimiento", ""))
        fvenc = _venc_to_date(venc)
        dias = (fvenc - hoy).days if fvenc else None
        if dias is not None and dias < 0:
            continue   # ya vencida (su cierre lo gestiona la pestaña Opciones)
        if dias is not None and dias <= 45:
            prox_vencer += 1
        # Moneyness: precio subyacente (nativo) vs strike.
        sub = str(c.get("subyacente", ""))
        moneyness = None
        if sub:
            if sub not in cache_sub:
                pv = _precio_y_divisa(sub)
                cache_sub[sub] = Decimal(str(pv[0])) if pv else None
            px_sub = cache_sub[sub]
            try:
                strike = Decimal(str(c.get("strike", "")).replace(",", "."))
            except Exception:
                strike = None
            if px_sub is not None and strike is not None:
                tipo = str(c.get("tipo_op", "")).upper()
                if tipo.startswith("C"):
                    moneyness = "ITM" if px_sub > strike else "OTM"
                elif tipo.startswith("P"):
                    moneyness = "ITM" if px_sub < strike else "OTM"
        if moneyness == "ITM":
            n_itm += 1
        es_corta = n_abiertos > 0   # n_net_abiertos > 0 = neto vendido (corta)
        riesgo = bool(es_corta and moneyness == "ITM" and dias is not None and 0 <= dias <= 45)
        # Mostrar solo lo relevante: próximas a vencer (no vencidas) o con riesgo.
        if (dias is not None and 0 <= dias <= 45) or riesgo:
            opciones_riesgo.append(OpcionRiesgo(
                simbolo=f"{sub} {c.get('tipo_op','')}{c.get('strike','')} {venc}".strip(),
                tipo_op=str(c.get("tipo_op", "")), strike=str(c.get("strike", "")),
                vencimiento=venc, dias_a_vencer=dias, moneyness=moneyness,
                es_corta=es_corta, riesgo_ejercicio=riesgo,
            ))
    opciones_riesgo.sort(key=lambda o: (o.dias_a_vencer if o.dias_a_vencer is not None else 9999))

    return DashboardResultado(
        anio=anio, fecha_calculo=hoy,
        capital_mercado_eur=capital_mercado, gp_no_realizada_eur=gp_no_real,
        gp_no_realizada_pct=gp_no_real_pct, liquidez_eur=liquidez,
        progreso_if_pct=progreso, anios_if=anios, retorno_if_pct=retorno_if,
        composicion=composicion,
        posiciones_peso=posiciones_peso,
        yield_actual_pct=yield_actual, dividendos_brutos_anio=dv.bruto_total,
        yield_estimado_pct=agg.yield_estimado_pct,
        cagr_anual_pct=cagr_anual, retorno_5y_pct=retorno_5y,
        proximos_pasos=proximos,
        gp_realizada_anio=Decimal(str(opt.gp_realizada_ytd)),
        perdidas_por_aflorar=Decimal(str(opt.diferidas_2m)),
        compensable_ahora=Decimal(str(opt.compensable_ahora)),
        perdida_a_arrastrar=Decimal(str(opt.perdida_a_arrastrar_anio)),
        opciones_riesgo=opciones_riesgo,
        opciones_proximas_vencer=prox_vencer, opciones_itm=n_itm,
    )
