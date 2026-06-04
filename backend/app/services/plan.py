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
from sqlalchemy import func, select
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
    estado: str = "PENDIENTE"
    fecha_objetivo: date | None = None


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
    fecha_objetivo: date | None = None        # deadline del paso (manual)
    proximo_tramo_fecha: date | None = None   # solo DCA en curso: última tx + espaciado régimen


# Días promedio del espaciado por régimen (centro de la horquilla WG):
# VERDE 2-3 sem → 18d · AMARILLO 3-4 sem → 25d · ROJO 4-6 sem → 35d.
_DIAS_ESPACIADO_REGIMEN = {"VERDE": 18, "AMARILLO": 25, "ROJO": 35}

# Decisiones que "consume" una transacción: una BUY avanza COMPRAR/REFORZAR; una
# SELL avanza VENDER/RECORTAR. MANTENER/MONITORIZAR/ESPERAR no se ven afectadas.
_AVANZA_BUY = {"COMPRAR", "REFORZAR"}
_AVANZA_SELL = {"VENDER", "RECORTAR"}
_TOLERANCIA_COMPLETADO = Decimal("0.05")   # ±5 % del objetivo = completado


def _proximo_tramo_fecha(
    db: Session, cartera_id: str, isin: str, decision: str, estado: str,
    regimen: str = "AMARILLO",
) -> date | None:
    """Fecha estimada del siguiente tramo de DCA = última operación BUY/SELL del
    ISIN + espaciado del régimen. Solo aplica a pasos de DCA en marcha (estado
    EN_CURSO con decisión COMPRAR/REFORZAR/VENDER/RECORTAR). Devuelve None si
    no aplica o si no hay ninguna operación previa del ISIN."""
    from datetime import timedelta
    if estado != "EN_CURSO":
        return None
    if decision not in (_AVANZA_BUY | _AVANZA_SELL):
        return None
    ultima = db.execute(
        select(func.max(models.Transaccion.fecha))
        .join(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo.in_(("BUY", "SELL")))
    ).scalar()
    if ultima is None:
        return None
    dias = _DIAS_ESPACIADO_REGIMEN.get(regimen, 25)
    return ultima + timedelta(days=dias)


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
            razon=p.razon, estado=p.estado, fecha_objetivo=p.fecha_objetivo,
        )
    return out


def posiciones_con_plan(db: Session, cartera_id: str) -> list[PosicionPlan]:
    from app.services.regimen import estado_regimen
    bloques = {
        b.id: b.nombre for b in db.execute(
            select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
        ).scalars()
    }
    activas = decisiones_activas(db, cartera_id)
    posiciones = list(db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars())
    regimen = estado_regimen(db, cartera_id).regimen

    out: list[PosicionPlan] = []
    isines_pos: set[str] = set()
    for pos in posiciones:
        est = estado_posicion(db, pos.id)
        if est["cantidad"] <= 0:
            continue
        isines_pos.add(pos.isin)
        da = activas.get(pos.isin)
        bid = pos.bloque_id if pos.bloque_id in bloques else None
        proximo = _proximo_tramo_fecha(
            db, cartera_id, pos.isin,
            da.decision if da else DECISION_DEFECTO,
            da.estado if da else "PENDIENTE",
            regimen,
        ) if da else None
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
            fecha_objetivo=da.fecha_objetivo if da else None,
            proximo_tramo_fecha=proximo,
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
    reemplazar: bool = True,
    nombre: str | None = None, ticker: str | None = None,
) -> models.PlanPaso:
    """Crea un paso del plan. `nombre`/`ticker` se usan SOLO cuando el ISIN
    es nuevo (ni en cartera ni en seguimiento) y la decisión es de compra/hold:
    en ese caso lo añadimos automáticamente al watchlist con esos datos, en
    vez de rechazar con 404. Para VENDER/RECORTAR mantenemos el 404 — no se
    puede vender lo que no se tiene."""
    _validar_enums(decision, prioridad, None)
    # El paso puede ser sobre una posición (cartera) o sobre una empresa del
    # watchlist (Seguimiento) que planeas comprar. Solo se rechaza si no es ninguna
    # Y además la decisión no admite watchlist (vender/recortar).
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
            # VENDER/RECORTAR requieren posición en cartera — no tiene sentido
            # crear un paso de venta sobre algo que no se tiene.
            if decision in _AVANZA_SELL:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    f"No tienes {isin} en cartera; no se puede crear un paso "
                    f"de {decision} sobre un valor sin posición.",
                )
            # COMPRAR/REFORZAR/MANTENER/MONITORIZAR/ESPERAR: doctrina watchlist-first,
            # auto-añadir al watchlist con lo que la IA o el usuario nos den.
            db.add(models.Seguimiento(
                cartera_id=cartera_id, isin=isin,
                ticker=(ticker or isin[:8]),     # ticker placeholder; el usuario lo refina
                nombre=(nombre or None),
                divisa=None,
                notas="Añadido automáticamente al crear un paso del plan",
            ))
            db.flush()
    # Un valor tiene UNA decisión vigente: el paso nuevo REEMPLAZA a los activos
    # anteriores del mismo ISIN (se cancelan) para que no convivan decisiones
    # contradictorias (p.ej. VENDER viejo + MANTENER tras re-evaluar). El histórico
    # queda como CANCELADO, no se borra.
    if reemplazar:
        previos = db.execute(
            select(models.PlanPaso)
            .where(models.PlanPaso.cartera_id == cartera_id)
            .where(models.PlanPaso.isin == isin)
            .where(models.PlanPaso.estado.in_(ESTADOS_ACTIVOS))
        ).scalars().all()
        for p in previos:
            p.estado = "CANCELADO"
            p.notas = ((p.notas + " · ") if p.notas else "") + f"Reemplazado por nuevo paso {decision}"
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
    criterios: str = ""                      # requisitos del bloque (ficha) para la guía de compra


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
    from app.adapters.ia.prompt import FICHAS
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
            criterios=(FICHAS[b.categoria_base].criterios if b.categoria_base in FICHAS else ""),
        ))
    # Mayor déficit primero (los sin objetivo, al final).
    out.sort(key=lambda h: (h.deficit_pct is None, -(h.deficit_pct or Decimal("0"))))
    return HuecoResultado(
        total_actual_eur=total_actual, total_planeado_eur=total_planeado,
        total_proyectado_eur=total_proy, sin_clasificar_planeado_eur=sin_clasif,
        bloques=out,
    )


# ── Aplicar transacción confirmada a los pasos del plan ─────────────────────
def aplicar_transaccion(db: Session, cartera_id: str, isin: str) -> int:
    """Actualiza los pasos activos del ISIN tras una transacción confirmada.

    Para cada paso PENDIENTE/EN_CURSO cuya `decision` se vea afectada por
    transacciones del ISIN:
      - Suma el desplegado: Σ `importe_eur` de las txs confirmadas del ISIN del
        tipo correspondiente (BUY o SELL) desde `created_at` del paso.
      - Decide nuevo estado:
        · sin desplegado → sigue PENDIENTE.
        · desplegado y posición a 0 (solo en VENDER total) → COMPLETADO.
        · desplegado ≥ objetivo·(1−tolerancia) → COMPLETADO.
        · desplegado entre objetivo·tolerancia y casi-objetivo → EN_CURSO.
        · sin `capital_objetivo_eur` → al primer movimiento pasa a EN_CURSO
          (queda a criterio del usuario marcar COMPLETADO).
      - Actualiza `notas` con el progreso ("desplegado X € / objetivo Y €").
    Devuelve el nº de pasos actualizados. Idempotente.
    """
    from app.services.fifo import estado_posicion
    activos = list(db.execute(
        select(models.PlanPaso)
        .where(models.PlanPaso.cartera_id == cartera_id)
        .where(models.PlanPaso.isin == isin)
        .where(models.PlanPaso.estado.in_(ESTADOS_ACTIVOS))
    ).scalars())
    if not activos:
        return 0
    n = 0
    for paso in activos:
        if paso.decision in _AVANZA_BUY:
            tipos = ["BUY"]
        elif paso.decision in _AVANZA_SELL:
            tipos = ["SELL"]
        else:
            continue                       # MANTENER/MONITORIZAR/ESPERAR no avanzan
        # Suma de importes EUR de las txs confirmadas del ISIN posteriores a
        # la creación del paso. Sin join explícito: filtro por posicion.isin.
        pos_ids = [
            p.id for p in db.execute(
                select(models.Posicion)
                .where(models.Posicion.cartera_id == cartera_id)
                .where(models.Posicion.isin == isin)
            ).scalars()
        ]
        if not pos_ids:
            continue
        desplegado = db.execute(
            select(func.coalesce(func.sum(models.Transaccion.importe_eur), 0))
            .where(models.Transaccion.cartera_id == cartera_id)
            .where(models.Transaccion.posicion_id.in_(pos_ids))
            .where(models.Transaccion.estado == "confirmada")
            .where(models.Transaccion.tipo.in_(tipos))
            .where(models.Transaccion.fecha >= paso.created_at.date())
        ).scalar() or 0
        desplegado = Decimal(str(desplegado))
        # Caso especial: VENDER y la posición ha quedado a 0 → COMPLETADO total.
        cant_actual = Decimal("0")
        if paso.decision in _AVANZA_SELL:
            pos = db.execute(
                select(models.Posicion)
                .where(models.Posicion.cartera_id == cartera_id)
                .where(models.Posicion.isin == isin)
            ).scalars().first()
            if pos is not None:
                cant_actual = estado_posicion(db, pos.id)["cantidad"]

        nuevo_estado = paso.estado
        nota_progreso: str | None = None
        if desplegado <= 0:
            continue                                       # sin cambios reales
        if paso.decision in _AVANZA_SELL and cant_actual <= 0:
            nuevo_estado = "COMPLETADO"
            nota_progreso = f"Cerrada por completo (vendido {_eur(desplegado)})."
        elif paso.capital_objetivo_eur and paso.capital_objetivo_eur > 0:
            obj = Decimal(str(paso.capital_objetivo_eur))
            ratio = desplegado / obj
            restante = max(obj - desplegado, Decimal("0"))
            if ratio >= (Decimal("1") - _TOLERANCIA_COMPLETADO):
                nuevo_estado = "COMPLETADO"
                nota_progreso = f"Completado: {_eur(desplegado)} / objetivo {_eur(obj)}."
            else:
                nuevo_estado = "EN_CURSO"
                nota_progreso = (f"En curso: {_eur(desplegado)} / objetivo {_eur(obj)} "
                                 f"({float(ratio) * 100:.0f} %). Restan {_eur(restante)}.")
        else:
            # Sin objetivo de capital: el primer movimiento lo deja en curso.
            nuevo_estado = "EN_CURSO"
            nota_progreso = f"En curso: {_eur(desplegado)} desplegados (sin objetivo)."

        if nuevo_estado != paso.estado or nota_progreso:
            paso.estado = nuevo_estado
            paso.notas = nota_progreso
            n += 1
    if n:
        db.commit()
    return n


def _eur(v: Decimal) -> str:
    return f"{float(v):,.0f} €".replace(",", ".")
