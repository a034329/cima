"""Auditoría pre-operación: la 'criba' de la doctrina WG en un solo veredicto.

Sintetiza los filtros del Protocolo de Razonamiento para una COMPRA reusando los
servicios existentes (encaje de bloque, fase, 15/15, tamaño, macro). NO bloquea:
informa. El lado VENDER/rotación lo cubre la fricción + la pestaña Rotación.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

# Rangos de tamaño de posición por bloque (doctrina WG). None = sin límite por ese lado.
RANGOS_TAMANO: dict[str, tuple[Decimal | None, Decimal | None]] = {
    "growth": (Decimal("2000"), Decimal("13000")),     # secundaria 2-4k .. núcleo 6-13k
    "aggressive": (None, Decimal("5000")),             # Bloque D: máx 5.000 €
}

_VERIFICAR = ("Verifica tú lo que Cima no mide: cobertura del dividendo (FCF/OCF), "
              "deuda próxima, tendencia de ingresos y solidez del moat.")


@dataclass
class Chequeo:
    filtro: str
    estado: str            # OK | AVISO | INFO | VERIFICAR
    titulo: str
    detalle: str


@dataclass
class Auditoria:
    isin: str
    nombre: str
    decision: str
    bloque_objetivo: str | None
    chequeos: list[Chequeo] = field(default_factory=list)
    resumen: str = ""


def _fase(db: Session, cartera_id: str) -> str:
    from app.services.onboarding import plan_firmado_actual

    pf = plan_firmado_actual(db, cartera_id)
    if pf and pf.perfil_json:
        try:
            return (json.loads(pf.perfil_json) or {}).get("fase") or "acumulacion"
        except (ValueError, TypeError):
            pass
    return "acumulacion"


def _valor_posicion(db: Session, cartera_id: str, isin: str) -> Decimal:
    """Valor de mercado de la posición abierta (0 si es solo watchlist)."""
    from app.services.fifo import estado_posicion
    from app.services.precios import obtener_precios_eur

    pos = db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    if pos is None:
        return Decimal("0")
    cant = estado_posicion(db, pos.id)["cantidad"]
    if cant <= 0:
        return Decimal("0")
    precios, _ = obtener_precios_eur(db, cartera_id)
    px = precios.get(isin)
    return (px * cant) if px is not None else Decimal("0")


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def auditar_compra(db: Session, cartera_id: str, isin: str,
                   bloque_cat: str | None = None) -> Auditoria:
    """Corre los filtros aplicables a una COMPRA y devuelve un veredicto. El bloque
    efectivo es `bloque_cat` (el que quieres llenar) o, si no se da, el que sugiere la IA."""
    from app.services import regimen as regimen_svc
    from app.services.clasificador import construir_contexto, evaluar_candidato
    from app.services.plan import listar_pasos

    ctx = construir_contexto(db, cartera_id, isin)
    ev = evaluar_candidato(db, cartera_id, isin, bloque_cat)
    cat = bloque_cat or ev.categoria_sugerida
    fase = _fase(db, cartera_id)
    chequeos: list[Chequeo] = []

    # F1 — Plan activo: pasos CRÍTICOS pendientes
    criticos = [p for p in listar_pasos(db, cartera_id, "PENDIENTE") if p.prioridad == "CRITICA"]
    if criticos:
        chequeos.append(Chequeo("Plan activo", "AVISO",
            f"{len(criticos)} paso(s) CRÍTICO(s) pendientes",
            "Resuelve los pasos críticos del plan antes de abrir compras nuevas."))
    else:
        chequeos.append(Chequeo("Plan activo", "OK", "Sin pasos críticos pendientes", ""))

    # F2 — Encaje de bloque + criterios
    if bloque_cat and ev.cubre_target is False:
        chequeos.append(Chequeo("Encaje de bloque", "AVISO",
            f"No cubre el bloque objetivo (la IA lo ve como {ev.categoria_sugerida})", ev.veredicto))
    elif ev.n_medibles and ev.n_cumplidos == ev.n_medibles:
        chequeos.append(Chequeo("Encaje de bloque", "OK", ev.veredicto, ev.criterios_texto))
    else:
        chequeos.append(Chequeo("Encaje de bloque", "AVISO", ev.veredicto, ev.criterios_texto))

    # F3 — Filtro de fase
    if fase == "acumulacion" and cat == "aggressive":
        chequeos.append(Chequeo("Fase", "AVISO", "High Yield en fase de acumulación",
            "En acumulación prioriza Compounders/Dividend Growth; el flujo de caja de High Yield rinde más en 2028."))
    elif fase == "acumulacion" and cat in ("growth", "income"):
        chequeos.append(Chequeo("Fase", "OK", "Alineado con la fase de acumulación", ""))
    else:
        chequeos.append(Chequeo("Fase", "INFO", f"Fase {fase}", ""))

    # F4 — Cláusula 15/15 (solo Bloque A / Estable)
    if cat == "defensivo":
        cg = ctx.cagr4_div_pct
        if cg is not None and cg >= 0.15:
            chequeos.append(Chequeo("Cláusula 15/15", "INFO", "Compra prioritaria (cláusula de oro)",
                f"Retorno total {_pct(cg)} ≥ 15% en un Bloque A: se autoriza incluso en acumulación."))
        else:
            chequeos.append(Chequeo("Cláusula 15/15", "INFO",
                f"No cumple 15/15 (retorno total {_pct(cg)})", "Para Estable, el 15/15 es la compra prioritaria."))

    # F5 — Tamaño de posición
    rmin, rmax = RANGOS_TAMANO.get(cat, (None, None))
    valor = _valor_posicion(db, cartera_id, isin)
    if rmax is not None and valor >= rmax:
        chequeos.append(Chequeo("Tamaño", "AVISO",
            f"Ya en el tope del rango ({_eur(valor)} / máx {_eur(rmax)})",
            "Añadir más sobreponderaría esta posición frente a su rango recomendado."))
    elif rmin is not None or rmax is not None:
        rango = f"{_eur(rmin) if rmin else '—'}–{_eur(rmax) if rmax else '—'}"
        chequeos.append(Chequeo("Tamaño", "INFO", f"Rango recomendado {rango}",
            f"Posición actual: {_eur(valor)}."))
    else:
        chequeos.append(Chequeo("Tamaño", "INFO", "Tamaño caso a caso", f"Posición actual: {_eur(valor)}."))

    # F8 — Abogado del diablo (yield alto en acumulación)
    y = ctx.yield_pct
    if y is not None and y > 0.07 and fase == "acumulacion":
        chequeos.append(Chequeo("Abogado del diablo", "AVISO",
            f"Yield {_pct(y)} > 7% en acumulación",
            "¿No rinde más este capital en Compounders hasta 2028? Cuestiona el coste de oportunidad."))

    # F9 — Filtro macro (régimen + ventana −14%)
    from app.services.precios import mercado_correccion
    est = regimen_svc.estado_regimen(db, cartera_id)
    corr = regimen_svc.evaluar_correccion(est, mercado_correccion())
    detalle_macro = f"Tramos de {est.tramo_min}–{est.tramo_max} € cada {est.espaciado}."
    if corr.activa and corr.escalado_min:
        detalle_macro += f" Ventana −14% activa: puedes escalar a {corr.escalado_min}–{corr.escalado_max} € en nombres coyunturales."
    chequeos.append(Chequeo("Macro", "INFO", f"Régimen {est.regimen}", detalle_macro))

    # Recordatorio cualitativo (lo que Cima no mide)
    chequeos.append(Chequeo("Calidad (cualitativo)", "VERIFICAR", "A verificar por ti", _VERIFICAR))

    avisos = sum(1 for c in chequeos if c.estado == "AVISO")
    if avisos == 0:
        resumen = "Luz verde: la compra está alineada con tu estrategia. Revisa lo cualitativo."
    elif avisos == 1:
        resumen = "Con una reserva: revisa el aviso antes de comprar."
    else:
        resumen = f"Con {avisos} reservas: reconsidera o ajusta antes de comprar."

    return Auditoria(isin=isin, nombre=ctx.nombre, decision="COMPRAR",
                     bloque_objetivo=cat, chequeos=chequeos, resumen=resumen)


def auditar_venta(db: Session, cartera_id: str, isin: str) -> Auditoria:
    """Filtros de la doctrina aplicables a una VENTA/rotación: fiscal de rotación
    (umbrales R-U), anti-churn, regla del colchón y calidad de rotación. Complementa
    a la fricción (gate emocional); esto es el análisis."""
    from app.services.clasificador import construir_contexto
    from app.services.fiscal_rotacion import calcular_rotacion
    from app.services.plan import listar_pasos

    ctx = construir_contexto(db, cartera_id, isin)
    chequeos: list[Chequeo] = []

    # F1 — Plan activo
    criticos = [p for p in listar_pasos(db, cartera_id, "PENDIENTE") if p.prioridad == "CRITICA"]
    if criticos:
        chequeos.append(Chequeo("Plan activo", "AVISO",
            f"{len(criticos)} paso(s) CRÍTICO(s) pendientes", "Revisa el plan antes de rotar."))
    else:
        chequeos.append(Chequeo("Plan activo", "OK", "Sin pasos críticos pendientes", ""))

    # Regla del colchón / fuera de estrategia (regla absoluta)
    pos = db.execute(select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
                     .where(models.Posicion.isin == isin)).scalars().first()
    bloque = db.get(models.Bloque, pos.bloque_id) if pos and pos.bloque_id else None
    if bloque is not None and not bloque.en_estrategia:
        chequeos.append(Chequeo("Regla del colchón", "AVISO", "Posición FUERA de la estrategia IF",
            "Regla absoluta: nunca vendas el colchón para reinvertir en la cartera IF — la paz "
            "mental es el activo más rentable en una crisis. Solo en emergencia vital."))

    # Filtro fiscal de rotación (umbrales R-U que el destino debe batir)
    rot = calcular_rotacion(db, cartera_id)
    item = next((it for it in rot.items if it.isin == isin), None)
    if item is None:
        chequeos.append(Chequeo("Fiscal de rotación", "INFO", "Sin datos de rotación",
            "No es una posición con estimación; no puedo calcular el coste fiscal."))
    elif item.gp_latente_eur <= 0:
        chequeos.append(Chequeo("Fiscal de rotación", "OK",
            f"Sin plusvalía latente ({_eur(item.gp_latente_eur)}): rotación sin coste fiscal", ""))
    else:
        umbrales = (f"1A {_pct(item.umbral_1y_pct)} · 2A {_pct(item.umbral_2y_pct)} · "
                    f"3A {_pct(item.umbral_3y_pct)} · 4A {_pct(item.umbral_4y_pct)}")
        cubierta = rot.buffer_perdidas_eur >= item.gp_latente_eur and rot.buffer_perdidas_eur > 0
        estado_rot = "OK" if (item.coste_fiscal_eur <= 0 and cubierta) else "INFO"
        nota_buffer = (f" Tus pérdidas pendientes ({_eur(rot.buffer_perdidas_eur)}) "
                       + ("CUBREN esta plusvalía → coste fiscal ~0, el switching cost desaparece."
                          if cubierta else
                          "la absorben en parte; el coste mostrado ya descuenta ese buffer.")
                       ) if rot.buffer_perdidas_eur > 0 else ""
        chequeos.append(Chequeo("Fiscal de rotación", estado_rot,
            f"Plusvalía latente {_eur(item.gp_latente_eur)} · coste fiscal {_eur(item.coste_fiscal_eur)}"
            f" ({_pct(item.tipo_efectivo_pct)})",
            f"El destino debe batir en CAGR4+Div: {umbrales}. Si no lo supera, rotar destruye valor."
            + nota_buffer))

    # Contexto fiscal global: cosecha, caducidad de pérdidas y regla 2M
    try:
        from app.services.fiscal_contexto import calcular_contexto
        fc = calcular_contexto(db, cartera_id)
    except Exception:  # noqa: BLE001 — sin histórico fiscal → omitir
        fc = None
    if fc is not None:
        es_perdida = item is not None and item.gp_latente_eur < 0
        if fc.caducan_este_anio_eur > 0:
            chequeos.append(Chequeo("Pérdidas que caducan", "AVISO",
                f"{_eur(fc.caducan_este_anio_eur)} caducan en {fc.ejercicio}",
                "Úsalas o se pierden: realizar plusvalías este año las compensa (coste fiscal ~0)."))
        if item is not None and item.gp_latente_eur > 0 and fc.cosechable_latente_eur > 0:
            chequeos.append(Chequeo("Emparejar con pérdidas", "INFO",
                f"Pérdidas latentes cosechables: {_eur(fc.cosechable_latente_eur)}",
                "Puedes realizar pérdidas latentes a la vez que esta plusvalía para neutralizar la "
                "cuota (tax-loss harvesting) y reducir el switching cost a ~0."))
        if es_perdida or fc.bloqueo_2m:
            bloqueada = ctx.nombre in fc.bloqueo_2m
            chequeos.append(Chequeo("Regla de los 2 meses",
                "AVISO" if bloqueada else "VERIFICAR",
                "Pérdida diferida: no deducible ahora (compra <2 meses), no anulada" if bloqueada
                else "Vigila la recompra",
                "Recomprar un valor homogéneo vendido en pérdidas dentro de 2 meses NO anula la "
                "pérdida: la DIFIERE (Art. 33.5.f LIRPF) — la computarás cuando transmitas "
                "definitivamente esas acciones sin recomprar en 2 meses. Si quieres aflorar la "
                "pérdida YA (p. ej. para compensar una plusvalía este año), no recompres en ese plazo."))

    # Anti-churn
    cg = ctx.cagr4_div_pct
    if cg is not None and cg > 0.10:
        chequeos.append(Chequeo("Anti-churn", "AVISO", f"Esta posición rinde {_pct(cg)} (> 10%)",
            "No la vendas por una diferencia < 2% con la alternativa: el churn erosiona por coste "
            "fiscal y de oportunidad."))

    # Calidad de rotación (cualitativo)
    chequeos.append(Chequeo("Calidad de rotación", "VERIFICAR", "Verifica el destino antes de rotar",
        "Calidad del negocio destino, cobertura del dividendo (FCF/OCF), tendencia de ingresos/deuda, "
        "y el yield on cost a 3-4 años (un 4% creciente puede batir un 12% plano)."))

    avisos = sum(1 for c in chequeos if c.estado == "AVISO")
    if avisos == 0:
        resumen = "Sin objeciones a la venta; revisa lo cualitativo y la fiscalidad."
    elif avisos == 1:
        resumen = "Con una reserva: revísala antes de vender/rotar."
    else:
        resumen = f"Con {avisos} reservas: reconsidera antes de vender/rotar."

    return Auditoria(isin=isin, nombre=ctx.nombre, decision="VENDER",
                     bloque_objetivo=None, chequeos=chequeos, resumen=resumen)


def _eur(v: Decimal | None) -> str:
    return "—" if v is None else f"{float(v):,.0f} €".replace(",", ".")
