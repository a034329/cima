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
        ))
    alertas.sort(key=lambda a: abs(a.cambio_pct), reverse=True)
    return alertas, (desde.date().isoformat() if desde else None)


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
