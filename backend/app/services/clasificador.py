"""Servicio del clasificador IA de bloques (Roadmap 1.6).

Ensambla el contexto de una empresa desde el feed que Cima YA tiene (precios +
estimaciones) + el catálogo de bloques de la cartera, y delega en el adaptador
de IA configurado. NO asigna: devuelve una sugerencia que el usuario aprueba.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import (
    BloqueOpcion,
    ContextoEmpresa,
    SugerenciaBloque,
    get_clasificador,
)
from app.adapters.ia.prompt import FICHAS, ROLES_CATEGORIA
from app.db import models
from app.services import creditos
from app.services.criterios import CriterioCheck, evaluar_criterios


def _f(v) -> float | None:  # type: ignore[no-untyped-def]
    return float(v) if isinstance(v, (int, float, Decimal)) else None


def catalogo_bloques(db: Session, cartera_id: str) -> list[BloqueOpcion]:
    """Bloques de la cartera como opciones para el modelo, con su rol WG."""
    bloques = db.execute(
        select(models.Bloque)
        .where(models.Bloque.cartera_id == cartera_id)
        .order_by(models.Bloque.orden)
    ).scalars()
    return [
        BloqueOpcion(
            id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
            rol=ROLES_CATEGORIA.get(b.categoria_base, ""),
        )
        for b in bloques
    ]


def _contexto(pos, fund: dict, e) -> ContextoEmpresa:  # type: ignore[no-untyped-def]
    """Arma un ContextoEmpresa desde posición + fundamentales + estimación."""
    from app.services.posiciones import _tipo_activo

    return ContextoEmpresa(
        isin=pos.isin,
        nombre=(e.nombre if e else None) or pos.nombre or pos.isin,
        sector=fund.get("sector"),
        industria=fund.get("industry"),
        divisa=(e.divisa if e else None) or pos.divisa_local,
        yield_pct=_f(e.div_yield_pct) if e else None,
        dividendo_share=_f(e.dividendo_share) if e else _f(fund.get("dividend")),
        per=_f(fund.get("pe")),
        crecimiento_eps_pct=_f(e.crecimiento_pct) if e else None,
        cagr4_div_pct=_f(e.cagr4_div_pct) if e else None,
        tipo_activo=_tipo_activo(pos.isin, pos.nombre),
        beta=_f(fund.get("beta")),
        roe=_f(fund.get("roe")),
    )


# ── compuertas deterministas (lo inequívoco; el resto lo juzga la IA) ───────

# Palabras que delatan un ETF temático/sectorial (→ Satélite). Heurístico por
# nombre: hoy no tenemos nº de holdings para distinguir amplio vs temático.
_PALABRAS_TEMATICAS = (
    "tech", "tecnolog", "semicond", "robo", "biotech", "cyber", "cloud",
    "tecdax", "fintech", "genom", "clean", "solar", "uranium", "gaming",
)


def _es_etf_tematico(nombre: str | None) -> bool:
    n = (nombre or "").lower()
    return any(p in n for p in _PALABRAS_TEMATICAS)


# Familia de ETFs "de colchón" (min-vol, covered call, premium income): la
# compuerta NO debe forzarlos a Índice/Núcleo con confianza 0.95 — pueden ser
# vehículos del colchón (Bloque F) o de rentas según el contexto del usuario
# (caso real: JEPQ es high-yield para Angel, no colchón). Se delegan a la IA
# con los few-shots (auditoría Cima 2026-06-11, D1).
_PALABRAS_COLCHON_ETF = (
    "min vol", "minimum volatility", "min volatility", "low vol",
    "low volatility", "covered call", "buywrite", "buy-write",
    "premium income", "equity premium",
)


def _es_etf_familia_colchon(nombre: str | None) -> bool:
    n = (nombre or "").lower()
    return any(p in n for p in _PALABRAS_COLCHON_ETF)


def pregate(ctx: ContextoEmpresa, cripto_disponible: bool = False) -> str | None:
    """Compuertas DURAS y deterministas (las computables hoy). Devuelve la
    categoria_base si una regla aplica con certeza, o None para delegar el juicio
    cualitativo en la IA. NO usa beta/ROIC (no disponibles aún). La cripto va al
    bloque Cripto si existe en la cartera; si no, a Satélite."""
    if ctx.tipo_activo == "CRYPTO":
        return "cripto" if cripto_disponible else "satelite"
    if ctx.tipo_activo == "ETF":
        if _es_etf_tematico(ctx.nombre):
            return "satelite"
        if _es_etf_familia_colchon(ctx.nombre):
            return None   # min-vol/covered-call: juicio cualitativo (IA + overrides)
        return "indice"
    if ctx.tipo_activo == "STOCK" and (ctx.yield_pct or 0.0) > 0.06:
        return "aggressive"
    return None


def _razon_regla(ctx: ContextoEmpresa, cat: str) -> str:
    if cat == "cripto":
        return "Cripto → bloque Cripto (regla por clase de activo)."
    if cat == "satelite" and ctx.tipo_activo == "CRYPTO":
        return "Cripto → Satélite/Alternativos (regla por clase de activo)."
    if cat == "satelite":
        return "ETF temático/sectorial → Satélite (regla por clase de activo)."
    if cat == "indice":
        return "ETF amplio y diversificado → Índice/Núcleo pasivo (regla)."
    if cat == "aggressive":
        y = (ctx.yield_pct or 0.0) * 100
        return (f"Yield {y:.1f}% > 6% → High Yield/Rentas (regla determinista; "
                "verifica la cobertura del dividendo).")
    return "Compuerta determinista por clase de activo."


def _sugerencia_regla(
    ctx: ContextoEmpresa, cat: str, catalogo: list[BloqueOpcion]
) -> SugerenciaBloque:
    """Sugerencia de una compuerta dura: sin llamar a la IA, sin gastar crédito.
    Alta confianza y proveedor 'regla' (distinguible de las de la IA)."""
    bloque_id = next((b.id for b in catalogo if b.categoria_base == cat), None)
    return SugerenciaBloque(
        categoria_base=cat, bloque_id=bloque_id, razonamiento=_razon_regla(ctx, cat),
        confianza=0.95, modelo="regla", proveedor="regla", isin=ctx.isin,
        distribucion=[{"categoria": cat, "prob": 1.0}],
    )


def _contexto_seg(seg, fund: dict, e) -> ContextoEmpresa:  # type: ignore[no-untyped-def]
    """ContextoEmpresa desde un Seguimiento (watchlist) + estimación + fundamentales."""
    from app.services.posiciones import _tipo_activo

    return ContextoEmpresa(
        isin=seg.isin,
        nombre=(e.nombre if e else None) or seg.nombre or seg.ticker or seg.isin,
        sector=fund.get("sector"),
        industria=fund.get("industry"),
        divisa=(e.divisa if e else None) or seg.divisa,
        yield_pct=_f(e.div_yield_pct) if e else None,
        dividendo_share=_f(e.dividendo_share) if e else _f(fund.get("dividend")),
        per=_f(fund.get("pe")),
        crecimiento_eps_pct=_f(e.crecimiento_pct) if e else None,
        cagr4_div_pct=_f(e.cagr4_div_pct) if e else None,
        tipo_activo=_tipo_activo(seg.isin, seg.nombre),
        beta=_f(fund.get("beta")),
        roe=_f(fund.get("roe")),
    )


def construir_contexto(db: Session, cartera_id: str, isin: str) -> ContextoEmpresa:
    """Contexto de la empresa desde precios + estimaciones. Sirve tanto a una
    posición de la cartera como a una empresa del watchlist (Seguimiento)."""
    from app.services import estimaciones as est_svc
    from app.services.precios import fundamentales_por_isin

    from app.services.fifo import estado_posicion

    fund = fundamentales_por_isin(db, cartera_id).get(isin, {})
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    seg = db.execute(
        select(models.Seguimiento)
        .where(models.Seguimiento.cartera_id == cartera_id)
        .where(models.Seguimiento.isin == isin)
    ).scalars().first()

    def _ctx_pos():
        calcs = {c.isin: c for c in est_svc.calcular_estimaciones(db, cartera_id)}
        return _contexto(pos, fund, calcs.get(isin))

    # Posición ABIERTA manda; una cerrada no debe tapar al seguimiento.
    if pos is not None and estado_posicion(db, pos.id)["cantidad"] > 0:
        return _ctx_pos()
    if seg is not None:
        calcs = {c.isin: c for c in est_svc.calcular_estimaciones_seguimiento(db, cartera_id)}
        return _contexto_seg(seg, fund, calcs.get(isin))
    if pos is not None:               # posición cerrada sin seguimiento (borde)
        return _ctx_pos()

    raise HTTPException(status.HTTP_404_NOT_FOUND,
                        f"{isin} no es posición ni seguimiento")


def sugerir(db: Session, cartera_id: str, isin: str) -> SugerenciaBloque:
    """Sugiere un bloque para la posición `isin`. No muta nada en BD. Si una
    compuerta determinista acierta, devuelve esa regla SIN llamar a la IA (ni
    consumir crédito); si no, delega en la IA con el few-shot de overrides."""
    from app.services import bloques as bloques_svc

    ctx = construir_contexto(db, cartera_id, isin)
    catalogo = catalogo_bloques(db, cartera_id)
    cat = pregate(ctx, any(b.categoria_base == "cripto" for b in catalogo))
    if cat is not None:
        return _sugerencia_regla(ctx, cat, catalogo)
    ejemplos = bloques_svc.overrides_recientes(db, cartera_id)
    s = get_clasificador().clasificar(ctx, catalogo, ejemplos)
    s.isin = isin
    creditos.registrar_uso_ia(db, cartera_id, "puntual", 1)
    return s


# ── evaluación de encaje (¿está en el bloque y cumple sus criterios?) ────────

@dataclass
class EvaluacionCandidato:
    """Resultado de evaluar un valor que el usuario ELIGIÓ: en qué bloque encaja
    (juicio de la IA) y si cumple los criterios medibles de ese bloque. No nombra
    valores ni empuja a comprar — analiza el que el usuario trajo."""
    isin: str
    nombre: str
    categoria_sugerida: str
    confianza: float
    razonamiento: str
    criterios_texto: str             # texto humano de la ficha del bloque
    checks: list[CriterioCheck]
    n_cumplidos: int
    n_medibles: int
    veredicto: str
    cualitativo: str                 # lo que el dato no cubre, a verificar tú
    target_categoria: str | None     # bloque-objetivo si vienes de un déficit
    cubre_target: bool | None        # ¿el bloque sugerido == el que querías llenar?


_CUALITATIVO = (
    "Verifica tú lo que el dato no cubre: payout y cobertura del dividendo "
    "(FCF/OCF), ROIC exacto, deuda próxima y la solidez del moat."
)


def evaluar_candidato(
    db: Session, cartera_id: str, isin: str, target_categoria: str | None = None,
) -> EvaluacionCandidato:
    """Evalúa el encaje de un candidato: usa `sugerir` para el bloque (qué ES) y
    `evaluar_criterios` para el chequeo medible de ese bloque. Si vienes del
    déficit de un bloque concreto (`target_categoria`), avisa si no lo cubre."""
    ctx = construir_contexto(db, cartera_id, isin)
    s = sugerir(db, cartera_id, isin)
    cat = s.categoria_base
    nombre_cat = FICHAS[cat].nombre if cat in FICHAS else cat
    criterios_texto = FICHAS[cat].criterios if cat in FICHAS else ""

    checks = evaluar_criterios(ctx, cat)
    medibles = [c for c in checks if c.cumple is not None]
    n_medibles = len(medibles)
    n_cumplidos = sum(1 for c in medibles if c.cumple)

    if n_medibles == 0:
        veredicto = (f"Encaja en {nombre_cat}, pero no hay criterios medibles con "
                     "los datos disponibles: júzgalo de forma cualitativa.")
    elif n_cumplidos == n_medibles:
        veredicto = f"Ejemplo limpio de {nombre_cat}: cumple los {n_medibles} criterios medibles."
    elif n_cumplidos == 0:
        veredicto = f"No cumple los criterios medibles de {nombre_cat} (0/{n_medibles})."
    else:
        veredicto = (f"Encaja parcialmente en {nombre_cat}: cumple "
                     f"{n_cumplidos}/{n_medibles} criterios medibles.")

    cubre_target = (cat == target_categoria) if target_categoria else None
    if cubre_target is False:
        objetivo_nombre = FICHAS[target_categoria].nombre if target_categoria in FICHAS else target_categoria
        veredicto += f" Ojo: NO cubre tu déficit de {objetivo_nombre} (la IA lo ve como {nombre_cat})."

    return EvaluacionCandidato(
        isin=isin, nombre=ctx.nombre, categoria_sugerida=cat, confianza=s.confianza,
        razonamiento=s.razonamiento, criterios_texto=criterios_texto, checks=checks,
        n_cumplidos=n_cumplidos, n_medibles=n_medibles, veredicto=veredicto,
        cualitativo=_CUALITATIVO, target_categoria=target_categoria, cubre_target=cubre_target,
    )


def isines_autoclasificables(
    db: Session, cartera_id: str, solo_sin_clasificar: bool = True
) -> list[str]:
    """ISINs de posiciones abiertas candidatas a autoclasificar. El frontend lo
    usa para trocear y pedir lotes pequeños, viendo el progreso por batch."""
    from app.services.fifo import estado_posicion

    out: list[str] = []
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if solo_sin_clasificar and pos.bloque_id is not None:
            continue
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        out.append(pos.isin)
    return out


def autoclasificar(
    db: Session, cartera_id: str, solo_sin_clasificar: bool = True,
    isines: list[str] | None = None,
) -> list[SugerenciaBloque]:
    """Clasifica EN LOTE posiciones abiertas. Si `isines` se da, clasifica solo
    esas (un batch dirigido por el frontend); si no, todas las candidatas según
    `solo_sin_clasificar`. No muta nada: devuelve sugerencias."""
    from app.services import estimaciones as est_svc
    from app.services.fifo import estado_posicion
    from app.services.precios import fundamentales_por_isin

    pedidos = set(isines) if isines is not None else None
    funds = fundamentales_por_isin(db, cartera_id)
    calcs = {c.isin: c for c in est_svc.calcular_estimaciones(db, cartera_id)}

    contextos: list[ContextoEmpresa] = []
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        if pedidos is not None:
            if pos.isin not in pedidos:
                continue
        else:
            if solo_sin_clasificar and pos.bloque_id is not None:
                continue
        if estado_posicion(db, pos.id)["cantidad"] <= 0:
            continue
        contextos.append(_contexto(pos, funds.get(pos.isin, {}), calcs.get(pos.isin)))

    if not contextos:
        return []
    catalogo = catalogo_bloques(db, cartera_id)
    # Las compuertas deterministas resuelven sin IA; solo el resto va al modelo.
    cripto_disp = any(b.categoria_base == "cripto" for b in catalogo)
    out: list[SugerenciaBloque] = []
    para_ia: list[ContextoEmpresa] = []
    for ctx in contextos:
        cat = pregate(ctx, cripto_disp)
        if cat is not None:
            out.append(_sugerencia_regla(ctx, cat, catalogo))
        else:
            para_ia.append(ctx)
    if para_ia:
        out.extend(get_clasificador().clasificar_lote(para_ia, catalogo))
        creditos.registrar_uso_ia(db, cartera_id, "lote", len(para_ia))
    return out
