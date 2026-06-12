"""Vigilancia de cartera: detecta movimientos de precio sustanciales desde el
último 'visto'. Como `vigilancia_cartera.py` de WG. Alimenta el dashboard y el asesor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, UTC
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

_ALERTA = Decimal("0.05")        # |Δ| ≥ 5% → ALERTA
_CRITICA = Decimal("0.10")       # |Δ| ≥ 10% → CRÍTICA


@dataclass
class Alerta:
    isin: str
    nombre: str
    precio_anterior: Decimal
    precio_actual: Decimal
    cambio_pct: Decimal          # fracción (−0.12 = −12%)
    nivel: str                   # ALERTA | CRITICA
    # Dos carriles complementarios:
    #   'baseline' = vs el último "visto" (cubre lapsos entre sesiones)
    #   'intradia' = vs cierre del día anterior (cubre "está subiendo HOY")
    modo: str = "baseline"


def _precios_actuales(db: Session, cartera_id: str) -> dict[str, Decimal]:
    from app.services.precios import obtener_precios_eur
    precios, _ = obtener_precios_eur(db, cartera_id)
    return precios


def _baseline(db: Session, cartera_id: str) -> dict[str, models.SnapshotPrecio]:
    return {s.isin: s for s in db.execute(
        select(models.SnapshotPrecio).where(models.SnapshotPrecio.cartera_id == cartera_id)
    ).scalars()}


def evaluar(db: Session, cartera_id: str) -> tuple[list[Alerta], str | None]:
    """Alertas de movimiento vs el baseline. Si no hay baseline aún, lo crea con los
    precios actuales y devuelve sin alertas (primer uso, sin ruido). No actualiza el
    baseline en usos posteriores (la alerta persiste hasta `marcar_visto`)."""
    precios = _precios_actuales(db, cartera_id)
    base = _baseline(db, cartera_id)
    if not base:
        for isin, px in precios.items():
            db.add(models.SnapshotPrecio(cartera_id=cartera_id, isin=isin, precio_eur=px))
        db.commit()
        return [], None

    nombres = {p.isin: p.nombre for p in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)).scalars()}
    desde = min((s.ts for s in base.values()), default=None)
    alertas: list[Alerta] = []
    for isin, px in precios.items():
        snap = base.get(isin)
        if snap is None or snap.precio_eur <= 0:
            continue
        cambio = (px - snap.precio_eur) / snap.precio_eur
        if abs(cambio) < _ALERTA:
            continue
        alertas.append(Alerta(
            isin=isin, nombre=nombres.get(isin, isin),
            precio_anterior=snap.precio_eur, precio_actual=px, cambio_pct=cambio,
            nivel="CRITICA" if abs(cambio) >= _CRITICA else "ALERTA",
            modo="baseline",
        ))
    alertas.sort(key=lambda a: abs(a.cambio_pct), reverse=True)
    return alertas, (desde.date().isoformat() if desde else None)


def evaluar_intradia(db: Session, cartera_id: str) -> list[Alerta]:
    """Alertas de movimiento del DÍA: precio actual vs cierre del día anterior.
    Complementario a `evaluar()` (baseline run-to-run). Útil para reaccionar a
    movimientos del día aunque no hayas marcado "visto" hace tiempo. Sin
    persistencia: cada llamada lo recalcula desde el cache de precios.
    Las posiciones sin `prev_close` cacheado se omiten en silencio."""
    from app.services.precios import obtener_cierres_anteriores_eur
    precios = _precios_actuales(db, cartera_id)
    cierres = obtener_cierres_anteriores_eur(db, cartera_id)
    nombres = {p.isin: p.nombre for p in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)).scalars()}
    alertas: list[Alerta] = []
    for isin, px in precios.items():
        prev = cierres.get(isin)
        if prev is None or prev <= 0:
            continue
        cambio = (px - prev) / prev
        if abs(cambio) < _ALERTA:
            continue
        alertas.append(Alerta(
            isin=isin, nombre=nombres.get(isin, isin),
            precio_anterior=prev, precio_actual=px, cambio_pct=cambio,
            nivel="CRITICA" if abs(cambio) >= _CRITICA else "ALERTA",
            modo="intradia",
        ))
    alertas.sort(key=lambda a: abs(a.cambio_pct), reverse=True)
    return alertas


def marcar_visto(db: Session, cartera_id: str) -> None:
    """Actualiza el baseline = precios actuales de las posiciones abiertas (limpia alertas)."""
    precios = _precios_actuales(db, cartera_id)
    base = _baseline(db, cartera_id)
    ahora = datetime.now(UTC)
    for isin, px in precios.items():
        snap = base.get(isin)
        if snap is None:
            db.add(models.SnapshotPrecio(cartera_id=cartera_id, isin=isin, precio_eur=px))
        else:
            snap.precio_eur = px
            snap.ts = ahora
    db.commit()

# ── Alertas plan↔precio (V4) ────────────────────────────────────────────────

@dataclass
class AlertaPlan:
    isin: str
    nombre: str
    decision: str                # COMPRAR / REFORZAR / VENDER / RECORTAR
    precio_alerta_eur: Decimal
    precio_actual_eur: Decimal
    paso_id: str
    razon: str | None = None


_GATILLO_BAJA = {"COMPRAR", "REFORZAR"}     # dispara si px ≤ gatillo
_GATILLO_SUBE = {"VENDER", "RECORTAR", "VENTA LIMIT"}   # dispara si px ≥ gatillo


def evaluar_plan_precio(db: Session, cartera_id: str) -> list[AlertaPlan]:
    """Cruza los pasos ACTIVOS del plan que tienen precio gatillo con los
    precios actuales: el plan dice QUÉ hacer; esto avisa de CUÁNDO el precio
    lo habilita. Sin persistencia — la alerta vive mientras la condición se
    cumpla y el paso siga activo."""
    pasos = list(db.execute(
        select(models.PlanPaso)
        .where(models.PlanPaso.cartera_id == cartera_id)
        .where(models.PlanPaso.estado.in_(("PENDIENTE", "EN_CURSO")))
        .where(models.PlanPaso.precio_alerta_eur.is_not(None))
    ).scalars())
    if not pasos:
        return []
    precios = _precios_actuales(db, cartera_id)
    nombres = {p.isin: p.nombre for p in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)).scalars()}
    alertas: list[AlertaPlan] = []
    for paso in pasos:
        px = precios.get(paso.isin)
        if px is None or px <= 0:
            continue
        gatillo = Decimal(str(paso.precio_alerta_eur))
        dispara = (
            (paso.decision in _GATILLO_BAJA and px <= gatillo)
            or (paso.decision in _GATILLO_SUBE and px >= gatillo)
        )
        if dispara:
            alertas.append(AlertaPlan(
                isin=paso.isin, nombre=nombres.get(paso.isin, paso.isin),
                decision=paso.decision, precio_alerta_eur=gatillo,
                precio_actual_eur=px, paso_id=paso.id, razon=paso.razon,
            ))
    return alertas
