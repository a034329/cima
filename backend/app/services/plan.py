"""Plan por valor: pasos del plan de inversión por posición.

Cola de pasos (como la hoja Plan de WG). La 'decisión vigente' de una posición
es la del paso activo (PENDIENTE/EN_CURSO) de mayor prioridad; sin paso activo
→ MANTENER. Reutiliza `estado_posicion` para el valor de posiciones abiertas.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import estado_posicion


PRIO_NUM = {"CRITICA": 1, "ALTA": 2, "MEDIA": 3, "BAJA": 4}
ESTADOS_ACTIVOS = ("PENDIENTE", "EN_CURSO")
DECISION_DEFECTO = "MANTENER"


@dataclass
class DecisionActiva:
    decision: str
    paso_id: str
    prioridad: str
    capital_objetivo_eur: Decimal | None
    razon: str | None


@dataclass
class PosicionPlan:
    isin: str
    nombre: str
    valor_eur: Decimal
    bloque_id: str | None
    bloque_nombre: str | None
    decision: str
    capital_objetivo_eur: Decimal | None
    razon: str | None
    prioridad: str | None
    paso_id: str | None
    en_cartera: bool = True          # False = candidato del watchlist (compra planeada)


def _prio(p: str) -> int:
    return PRIO_NUM.get(p, 99)


def listar_pasos(
    db: Session, cartera_id: str, estado: str | None = None
) -> list[models.PlanPaso]:
    pasos = list(db.execute(
        select(models.PlanPaso).where(models.PlanPaso.cartera_id == cartera_id)
    ).scalars())
    if estado is not None:
        pasos = [p for p in pasos if p.estado == estado]
    pasos.sort(key=lambda p: (_prio(p.prioridad), p.orden))
    return pasos


def decisiones_activas(db: Session, cartera_id: str) -> dict[str, DecisionActiva]:
    """Por ISIN, el paso activo (PENDIENTE/EN_CURSO) de mayor prioridad
    (desempate por orden de creación)."""
    pasos = [
        p for p in db.execute(
            select(models.PlanPaso).where(models.PlanPaso.cartera_id == cartera_id)
        ).scalars()
        if p.estado in ESTADOS_ACTIVOS
    ]
    pasos.sort(key=lambda p: (_prio(p.prioridad), p.orden))
    out: dict[str, DecisionActiva] = {}
    for p in pasos:
        if p.isin in out:
            continue  # ya tenemos el de mayor prioridad para este isin
        out[p.isin] = DecisionActiva(
            decision=p.decision, paso_id=p.id, prioridad=p.prioridad,
            capital_objetivo_eur=(
                Decimal(str(p.capital_objetivo_eur))
                if p.capital_objetivo_eur is not None else None
            ),
            razon=p.razon,
        )
    return out


def posiciones_con_plan(db: Session, cartera_id: str) -> list[PosicionPlan]:
    bloques = {
        b.id: b.nombre for b in db.execute(
            select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
        ).scalars()
    }
    activas = decisiones_activas(db, cartera_id)
    posiciones = list(db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars())

    out: list[PosicionPlan] = []
    isines_pos: set[str] = set()
    for pos in posiciones:
        est = estado_posicion(db, pos.id)
        if est["cantidad"] <= 0:
            continue
        isines_pos.add(pos.isin)
        da = activas.get(pos.isin)
        bid = pos.bloque_id if pos.bloque_id in bloques else None
        out.append(PosicionPlan(
            isin=pos.isin,
            nombre=pos.nombre or pos.isin,
            valor_eur=Decimal(str(est["coste_total_eur"])),
            bloque_id=bid,
            bloque_nombre=bloques.get(bid) if bid else None,
            decision=da.decision if da else DECISION_DEFECTO,
            capital_objetivo_eur=da.capital_objetivo_eur if da else None,
            razon=da.razon if da else None,
            prioridad=da.prioridad if da else None,
            paso_id=da.paso_id if da else None,
        ))
    out.sort(key=lambda p: p.valor_eur, reverse=True)

    # Candidatos del watchlist con paso activo: compras planeadas que aún no
    # están en cartera (valor 0). Van al final, tras las posiciones reales.
    segs = {
        s.isin: s for s in db.execute(
            select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
        ).scalars()
    }
    for isin, da in activas.items():
        if isin in isines_pos:
            continue
        s = segs.get(isin)
        if s is None:
            continue
        bid = s.bloque_id if s.bloque_id in bloques else None
        out.append(PosicionPlan(
            isin=s.isin, nombre=s.nombre or s.ticker or s.isin,
            valor_eur=Decimal("0"), bloque_id=bid,
            bloque_nombre=bloques.get(bid) if bid else None,
            decision=da.decision, capital_objetivo_eur=da.capital_objetivo_eur,
            razon=da.razon, prioridad=da.prioridad, paso_id=da.paso_id,
            en_cartera=False,
        ))
    return out


def _validar_enums(decision: str | None, prioridad: str | None, estado: str | None) -> None:
    if decision is not None and decision not in models.DECISIONES_PLAN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"decision inválida: {decision}")
    if prioridad is not None and prioridad not in models.PRIORIDADES_PLAN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"prioridad inválida: {prioridad}")
    if estado is not None and estado not in models.ESTADOS_PLAN:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"estado inválido: {estado}")


def crear_paso(
    db: Session, cartera_id: str, isin: str, decision: str, prioridad: str,
    *, razon: str | None = None, capital_objetivo_eur: Decimal | None = None,
    fecha_objetivo: date | None = None, notas: str | None = None,
) -> models.PlanPaso:
    _validar_enums(decision, prioridad, None)
    # El paso puede ser sobre una posición (cartera) o sobre una empresa del
    # watchlist (Seguimiento) que planeas comprar. Solo se rechaza si no es ninguna.
    es_posicion = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first() is not None
    if not es_posicion:
        es_seguimiento = db.execute(
            select(models.Seguimiento)
            .where(models.Seguimiento.cartera_id == cartera_id)
            .where(models.Seguimiento.isin == isin)
        ).scalars().first() is not None
        if not es_seguimiento:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"No tienes ni sigues {isin}")
    orden = (db.execute(
        select(models.PlanPaso).where(models.PlanPaso.cartera_id == cartera_id)
    ).scalars().all())
    siguiente = max((p.orden for p in orden), default=0) + 1
    paso = models.PlanPaso(
        cartera_id=cartera_id, isin=isin, decision=decision, prioridad=prioridad,
        razon=razon, capital_objetivo_eur=capital_objetivo_eur,
        fecha_objetivo=fecha_objetivo, notas=notas, estado="PENDIENTE",
        orden=siguiente,
    )
    db.add(paso)
    db.commit()
    return paso


def actualizar_paso(
    db: Session, cartera_id: str, paso_id: str, **campos: object
) -> models.PlanPaso:
    paso = db.get(models.PlanPaso, paso_id)
    if paso is None or paso.cartera_id != cartera_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paso no existe")
    _validar_enums(
        campos.get("decision"), campos.get("prioridad"), campos.get("estado"),  # type: ignore[arg-type]
    )
    for k in ("decision", "prioridad", "estado", "razon",
              "capital_objetivo_eur", "fecha_objetivo", "notas"):
        if k in campos:
            setattr(paso, k, campos[k])
    db.commit()
    return paso


def eliminar_paso(db: Session, cartera_id: str, paso_id: str) -> None:
    paso = db.get(models.PlanPaso, paso_id)
    if paso is None or paso.cartera_id != cartera_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paso no existe")
    db.delete(paso)
    db.commit()


# ── hueco de asignación (plan top-down) ─────────────────────────────────────

DECISIONES_COMPRA = ("COMPRAR", "REFORZAR")


@dataclass
class HuecoBloque:
    bloque_id: str
    nombre: str
    categoria_base: str
    objetivo_pct: Decimal | None             # fracción; None = sin objetivo fijado
    actual_pct: Decimal                      # sobre el total PROYECTADO
    planeado_pct: Decimal
    proyectado_pct: Decimal                  # actual + planeado
    deficit_pct: Decimal | None              # objetivo − proyectado (positivo = falta comprar)
    valor_actual_eur: Decimal
    planeado_eur: Decimal
    deficit_eur: Decimal | None


@dataclass
class HuecoResultado:
    total_actual_eur: Decimal
    total_planeado_eur: Decimal
    total_proyectado_eur: Decimal
    sin_clasificar_planeado_eur: Decimal     # compras planeadas de empresas sin bloque
    bloques: list[HuecoBloque]


def hueco_asignacion(db: Session, cartera_id: str) -> HuecoResultado:
    """Top-down: por bloque, cuánto falta comprar para llegar al objetivo, contando
    las compras ya PLANEADAS. déficit = objetivo% − proyectado% (proyectado =
    actual + planeado). El tamaño del tramo no sale de aquí (lo da el régimen macro);
    el déficit marca DÓNDE y en qué prioridad. Bloques sin peso_objetivo → déficit None."""
    from app.services import bloques as bloques_svc

    dist = bloques_svc.calcular_distribucion(db, cartera_id)
    # Base = capital DENTRO de la estrategia (incluye 'sin clasificar', excluye
    # Colchón y bloques fuera de estrategia). Así los % y el déficit son sobre el
    # tamaño de la cartera-estrategia, no del patrimonio total.
    total_actual = sum((b.valor_eur for b in dist.bloques if b.en_estrategia), Decimal("0"))

    # bloque de cada empresa: posición o seguimiento (None = sin clasificar).
    pos_bloque = {
        p.isin: p.bloque_id for p in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars()
    }
    seg_bloque = {
        s.isin: s.bloque_id for s in db.execute(
            select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
        ).scalars()
    }
    # Solo los bloques DENTRO de la estrategia IF guían el déficit (excluye
    # Colchón y los que el usuario haya sacado, p.ej. cripto a largo).
    bloque_ids = {
        b.id for b in dist.bloques
        if b.id != bloques_svc.SIN_CLASIFICAR_ID and b.en_estrategia
    }

    planeado: dict[str, Decimal] = {}
    sin_clasif = Decimal("0")
    for isin, da in decisiones_activas(db, cartera_id).items():
        if da.decision not in DECISIONES_COMPRA or da.capital_objetivo_eur is None:
            continue
        cap = da.capital_objetivo_eur
        bid = pos_bloque.get(isin) or seg_bloque.get(isin)
        if bid in bloque_ids:
            planeado[bid] = planeado.get(bid, Decimal("0")) + cap
        else:
            sin_clasif += cap

    # Solo el planeado que cae en bloques de estrategia entra en el tamaño base.
    total_planeado = sum(planeado.values(), Decimal("0"))
    total_proy = total_actual + total_planeado

    def pct(v: Decimal) -> Decimal:
        return (v / total_proy) if total_proy > 0 else Decimal("0")

    out: list[HuecoBloque] = []
    for b in dist.bloques:
        if b.id == bloques_svc.SIN_CLASIFICAR_ID or not b.en_estrategia:
            continue
        valor = b.valor_eur
        plan = planeado.get(b.id, Decimal("0"))
        proy = valor + plan
        objetivo = b.peso_objetivo
        if objetivo is not None:
            deficit_pct = objetivo - pct(proy)
            deficit_eur = objetivo * total_proy - proy
        else:
            deficit_pct = deficit_eur = None
        out.append(HuecoBloque(
            bloque_id=b.id, nombre=b.nombre, categoria_base=b.categoria_base,
            objetivo_pct=objetivo, actual_pct=pct(valor), planeado_pct=pct(plan),
            proyectado_pct=pct(proy), deficit_pct=deficit_pct,
            valor_actual_eur=valor, planeado_eur=plan, deficit_eur=deficit_eur,
        ))
    # Mayor déficit primero (los sin objetivo, al final).
    out.sort(key=lambda h: (h.deficit_pct is None, -(h.deficit_pct or Decimal("0"))))
    return HuecoResultado(
        total_actual_eur=total_actual, total_planeado_eur=total_planeado,
        total_proyectado_eur=total_proy, sin_clasificar_planeado_eur=sin_clasif,
        bloques=out,
    )
