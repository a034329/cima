"""Contexto fiscal para la RECOMENDACIÓN (asesor, auditoría de venta, hoja de ruta).

Resume, en pocos números, la situación que debe condicionar cualquier consejo de
vender/rotar:
  · base del ahorro YTD (lo que ya tributa este año),
  · pérdidas pendientes de años anteriores y cuánto CADUCA este ejercicio (úsalo
    o piérdelo),
  · `buffer_disponible`: pérdidas que absorberán automáticamente la PRÓXIMA
    plusvalía realizada (carryforward + arrastre del año) → reduce el switching cost,
  · `cosechable_latente`: pérdidas latentes que podrías realizar para neutralizar
    plusvalías (tax-loss harvesting), y `compensable_ahora`,
  · avisos de la regla de los 2 meses (posiciones cuya pérdida NO es deducible si
    recompras / acabas de comprar).

Reusa `calcular_fiscal` (compensación) y `calcular_optimizador` (cosecha + 2M).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.services.fiscal import calcular_fiscal
from app.services.fiscal_optimizador import calcular_optimizador

_CENT = Decimal("0.01")


@dataclass
class FiscalContexto:
    ejercicio: int
    base_ahorro_ytd_eur: Decimal          # gp+rcm tras compensación (lo que tributa hoy)
    gp_realizada_ytd_eur: Decimal
    perdidas_pendientes_eur: Decimal      # Σ pendiente de años anteriores (disponible)
    caducan_este_anio_eur: Decimal        # de las anteriores, las que expiran este ejercicio
    arrastre_anio_eur: Decimal            # pérdida de este año que se arrastra (magnitud)
    buffer_disponible_eur: Decimal        # absorbe la PRÓXIMA plusvalía: pendientes + arrastre
    cosechable_latente_eur: Decimal       # pérdidas latentes sin bloqueo 2M (magnitud)
    compensable_ahora_eur: Decimal        # min(realizado+, cosechable)
    diferidas_2m_eur: Decimal
    bloqueo_2m: list[str] = field(default_factory=list)   # nombres con pérdida NO deducible por 2M
    resumen: str = ""


def _q(v) -> Decimal:  # type: ignore[no-untyped-def]
    return Decimal(str(v)).quantize(_CENT)


def calcular_contexto(
    db: Session, cartera_id: str, ejercicio: int | None = None,
    precios: dict[str, Decimal] | None = None,
) -> FiscalContexto:
    if ejercicio is None:
        ejercicio = date.today().year

    f = calcular_fiscal(db, cartera_id, ejercicio)
    comp = f.resultado_compensacion
    opt = calcular_optimizador(db, cartera_id, ejercicio, precios=precios)

    base = _q(comp.base_ahorro_gp) + _q(comp.base_ahorro_rcm)
    pendientes = sum((_q(p.pendiente_eur) for p in comp.perdidas_actualizadas), Decimal("0"))
    # Caducan este ejercicio = pendientes cuyo último año compensable es el actual.
    caducan = sum((_q(p.pendiente_eur) for p in comp.perdidas_actualizadas
                   if p.expira == ejercicio), Decimal("0"))
    arrastre = _q(abs(comp.nuevo_saldo_negativo))
    buffer = pendientes + arrastre
    cosechable = _q(abs(opt.perdida_latente_cosechable))
    bloqueo = [lat.nombre for lat in opt.latentes if lat.es_perdida and lat.bloqueo_2m]

    ctx = FiscalContexto(
        ejercicio=ejercicio,
        base_ahorro_ytd_eur=base,
        gp_realizada_ytd_eur=_q(opt.gp_realizada_ytd),
        perdidas_pendientes_eur=pendientes,
        caducan_este_anio_eur=caducan,
        arrastre_anio_eur=arrastre,
        buffer_disponible_eur=buffer,
        cosechable_latente_eur=cosechable,
        compensable_ahora_eur=_q(opt.compensable_ahora),
        diferidas_2m_eur=_q(opt.diferidas_2m),
        bloqueo_2m=bloqueo,
    )
    ctx.resumen = _resumen(ctx)
    return ctx


def _eur(v: Decimal) -> str:
    return f"{float(v):,.0f} €".replace(",", ".")


def _resumen(c: FiscalContexto) -> str:
    """Texto compacto para los prompts de la IA."""
    p = [f"Base del ahorro YTD: {_eur(c.base_ahorro_ytd_eur)} (lo que ya tributa este año)."]
    if c.buffer_disponible_eur > 0:
        p.append(f"Tienes {_eur(c.buffer_disponible_eur)} en pérdidas que absorberán la PRÓXIMA "
                 f"plusvalía que realices (sin coste fiscal hasta ese importe) → si vendes un ganador "
                 f"dentro de ese margen, el switching cost fiscal es ~0.")
    if c.caducan_este_anio_eur > 0:
        p.append(f"OJO: {_eur(c.caducan_este_anio_eur)} de esas pérdidas CADUCAN este ejercicio "
                 f"({c.ejercicio}) — úsalas o se pierden (realiza plusvalías para compensarlas).")
    if c.cosechable_latente_eur > 0:
        p.append(f"Pérdidas latentes cosechables (sin bloqueo 2M): {_eur(c.cosechable_latente_eur)}; "
                 f"podrías realizarlas para neutralizar plusvalías ahora (compensable: "
                 f"{_eur(c.compensable_ahora_eur)}).")
    if c.bloqueo_2m:
        p.append(f"Regla 2M (Art. 33.5.f LIRPF): la pérdida de {', '.join(c.bloqueo_2m)} no es "
                 f"deducible AHORA (compra homogénea reciente <2 meses), queda DIFERIDA — no se pierde: "
                 f"se computará al transmitir definitivamente esas acciones sin recomprar en 2 meses.")
    if c.buffer_disponible_eur == 0 and c.cosechable_latente_eur == 0:
        p.append("No hay pérdidas pendientes ni cosechables: vender un ganador tributa al marginal.")
    return " ".join(p)
