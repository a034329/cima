"""Frescura de los datos que alimentan las pantallas (U8, mejoras 2026-06).

El usuario decide con precios y fundamentales cacheados (TTL 6h) y con lo
último que importó: este endpoint le dice DE CUÁNDO es cada cosa para que
sepa si está mirando datos de hace una hora o de la semana pasada.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models

router = APIRouter(prefix="/salud-datos", tags=["salud-datos"])


class SaludDatos(BaseModel):
    precios_ts: str | None          # último precio bajado del feed
    fx_ts: str | None               # último tipo de cambio
    fundamentales_ts: str | None    # últimos fundamentales (prefill)
    ultimo_import_ts: str | None    # último extracto subido
    ultimo_import_desc: str | None  # "degiro_transacciones 2026 (fichero.csv)"
    ultima_transaccion: str | None  # fecha de la tx confirmada más reciente


def _max_ts_cache(cache: dict, prefijo: str) -> str | None:
    ts = [e.get("ts", 0) for k, e in cache.items()
          if k.startswith(prefijo) and isinstance(e, dict)]
    if not ts or max(ts) == 0:
        return None
    return datetime.fromtimestamp(max(ts), tz=timezone.utc).isoformat()


def _cartera_o_404(db: Session) -> models.Cartera:
    cartera = db.execute(select(models.Cartera)).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera. Llama primero a POST /api/bootstrap",
        )
    return cartera


def _construir_salud(db: Session, cartera: models.Cartera) -> SaludDatos:
    from app.services.precios import _leer_cache

    cache = _leer_cache()

    ultimo = db.execute(
        select(models.ExtractoArchivo)
        .where(models.ExtractoArchivo.cartera_id == cartera.id)
        .order_by(models.ExtractoArchivo.uploaded_at.desc())
        .limit(1)
    ).scalars().first()

    ultima_tx = db.execute(
        select(models.Transaccion.fecha)
        .where(models.Transaccion.cartera_id == cartera.id)
        .where(models.Transaccion.estado == "confirmada")
        .order_by(models.Transaccion.fecha.desc())
        .limit(1)
    ).scalar()

    return SaludDatos(
        precios_ts=_max_ts_cache(cache, "px:"),
        fx_ts=_max_ts_cache(cache, "fx:"),
        fundamentales_ts=_max_ts_cache(cache, "fund:"),
        ultimo_import_ts=(ultimo.uploaded_at.isoformat() if ultimo else None),
        ultimo_import_desc=(
            f"{ultimo.kind} {ultimo.ejercicio} ({ultimo.filename_original})"
            if ultimo else None
        ),
        ultima_transaccion=(ultima_tx.isoformat() if ultima_tx else None),
    )


@router.get("", response_model=SaludDatos, summary="Frescura de precios/FX/fundamentales/imports")
def get_salud_datos(db: Session = Depends(get_db)) -> SaludDatos:
    return _construir_salud(db, _cartera_o_404(db))


@router.post("/refrescar", response_model=SaludDatos,
             summary="Refresca precios + tipos de cambio desde el feed y devuelve la frescura")
def refrescar(db: Session = Depends(get_db)) -> SaludDatos:
    """Refresco LIGERO de mercado disparado desde el badge de frescura: vuelve a
    bajar precios y FX de las posiciones abiertas (`forzar=True`), sin re-sembrar
    estimaciones ni fundamentales (eso es el prefill, más pesado). Devuelve la
    frescura actualizada para que el badge se repinte."""
    from app.services.precios import obtener_precios_eur

    cartera = _cartera_o_404(db)
    obtener_precios_eur(db, cartera.id, forzar=True)
    return _construir_salud(db, cartera)
