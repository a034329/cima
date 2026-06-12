"""Fricción conductual: avisa, rebate 2 veces, te deja, captura el override.

El mayor riesgo de la cartera es el propio inversor. Cuando va a hacer una
tontería (vender en pánico, romper la tesis de un compounder, tocar el colchón),
`evaluar_friccion` devuelve dos rebates de ángulos distintos (datos/doctrina y
tú/fiscal). NUNCA bloquea: el frontend deja proceder y registra el `EventoFriccion`.
Rara y merecida — si no hay señal de "no deberías", devuelve None (deja pasar).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

DECISIONES_PELIGROSAS = ("VENDER", "RECORTAR")
DECISIONES_PROTECTORAS = ("MANTENER", "COMPRAR", "REFORZAR")
_DECISION_LABEL = {
    "MANTENER": "Mantener", "COMPRAR": "Comprar", "REFORZAR": "Reforzar",
    "VENDER": "Vender", "RECORTAR": "Recortar",
}
_UMBRAL_COSTE = Decimal("50")   # coste fiscal € por debajo del cual no se menciona


@dataclass
class FriccionResultado:
    severidad: str                 # ALTA | MEDIA
    titulo: str
    rebate1: str                   # datos / doctrina
    rebate2: str                   # tú / fiscal
    etiquetas: list[str] = field(default_factory=list)


def evaluar_friccion(
    db: Session, cartera_id: str, isin: str, decision: str
) -> FriccionResultado | None:
    """Evalúa si una decisión VENDER/RECORTAR merece fricción. None = deja pasar."""
    if decision not in DECISIONES_PELIGROSAS:
        return None

    from app.services import estimaciones as est_svc
    from app.services import fiscal_rotacion, plan

    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    nombre = (pos.nombre if pos else None) or isin
    bloque = db.get(models.Bloque, pos.bloque_id) if pos and pos.bloque_id else None

    es_colchon = bloque is not None and (
        bloque.categoria_base == "colchon" or not bloque.en_estrategia
    )
    es_compounder = bloque is not None and bloque.categoria_base == "growth"

    calcs = {c.isin: c for c in est_svc.calcular_estimaciones(db, cartera_id)}
    cagr = calcs[isin].cagr4_div_pct if isin in calcs else None   # fracción
    cagr_alto = cagr is not None and cagr > Decimal("0.10")

    da = plan.decisiones_activas(db, cartera_id).get(isin)
    plan_protege = da is not None and da.decision in DECISIONES_PROTECTORAS

    ri = None
    try:
        rot = fiscal_rotacion.calcular_rotacion(db, cartera_id)
        ri = next((i for i in rot.items if i.isin == isin), None)
    except Exception:
        ri = None
    coste = ri.coste_fiscal_eur if ri else None
    umbral = ri.umbral_4y_pct if ri else None
    coste_relevante = coste is not None and coste > _UMBRAL_COSTE

    # ¿Dispara? Solo si hay alguna señal de "no deberías".
    if not (es_colchon or es_compounder or cagr_alto or plan_protege or coste_relevante):
        return None

    severidad = "ALTA" if (es_colchon or (es_compounder and plan_protege)) else "MEDIA"
    etiquetas: list[str] = []
    if bloque:
        etiquetas.append(bloque.nombre)
    if da:
        etiquetas.append(f"Plan: {_DECISION_LABEL.get(da.decision, da.decision)}")

    # ── Rebate 1: datos / doctrina ──
    r1: list[str] = []
    if cagr is not None:
        r1.append(f"{nombre} tiene un retorno total esperado de "
                  f"{cagr * 100:.1f}% anual (CAGR4+Div).")
    if es_compounder:
        r1.append("Es un Compounder: vender corta la composición y reentrar suele "
                  "salir peor (anti-churn).")
    r1.append("Regla −14%: SI lo que te empuja a vender es una caída sistémica "
              "del −10/−14% sin recesión a la vista, históricamente es una "
              "oportunidad de carga, no una señal de venta — comprueba el "
              "régimen macro antes de decidir.")
    rebate1 = " ".join(r1)

    # ── Rebate 2: tú / fiscal ──
    r2: list[str] = []
    if es_colchon:
        r2.append("REGLA ABSOLUTA del colchón: nunca lo vendas para reinvertir. La paz "
                  "mental es el activo más rentable en una crisis; solo se liquida en "
                  "una emergencia vital.")
    if plan_protege:
        r2.append(f"Tu propio Plan dice «{_DECISION_LABEL.get(da.decision, da.decision)}» "
                  "para este valor — vender lo contradice.")
    if coste is not None and coste > 0 and umbral is not None:
        r2.append(f"Realizar la plusvalía cuesta {coste:.0f} € en impuestos; el destino "
                  f"tendría que rendir más de {umbral * 100:.1f}% anual a 4 años para "
                  "que la rotación compense.")
        # V2: el coste en la unidad que importa — años de IF.
        delta = ri.delta_anios_if if ri else None
        if delta is not None and delta > 0:
            r2.append(f"Ese coste fiscal retrasa tu Independencia Financiera "
                      f"~{delta} años con la proyección actual.")
    rebate2 = " ".join(r2) or ("Comprueba que esta venta encaja con tu estrategia antes "
                               "de ejecutarla.")

    return FriccionResultado(
        severidad=severidad,
        titulo=f"Vas a {_DECISION_LABEL.get(decision, decision).lower()} {nombre}",
        rebate1=rebate1, rebate2=rebate2, etiquetas=etiquetas,
    )


def registrar_evento(
    db: Session, cartera_id: str, isin: str, decision: str,
    severidad: str, motivo: str | None, rebatido: bool = True,
) -> None:
    """Registra que el usuario procedió a pesar de la fricción (no hace commit)."""
    db.add(models.EventoFriccion(
        cartera_id=cartera_id, isin=isin, decision=decision,
        severidad=severidad, rebatido=rebatido, motivo=motivo,
    ))
