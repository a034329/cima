"""Endpoints de seguimiento/watchlist: empresas que sigues sin tener en cartera,
para estudiarlas antes de comprar. Reutiliza la valoración de estimaciones."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, models
from app.routers.estimaciones import EstimacionOut, _to_out
from app.services import estimaciones as svc
from app.services import precios as precios_svc
from app.services.fifo import estado_posicion

router = APIRouter(prefix="/seguimiento", tags=["seguimiento"])


def _q(x, places: str):  # type: ignore[no-untyped-def]
    return None if x is None else Decimal(str(x)).quantize(Decimal(places), ROUND_HALF_UP)


class SeguimientoOut(BaseModel):
    isin: str
    ticker: str
    nombre: str | None
    divisa: str | None
    notas: str | None
    bloque_id: str | None = None
    bloque_nombre: str | None = None
    estimacion: EstimacionOut


class AltaSeguimiento(BaseModel):
    ticker: str
    notas: str | None = None


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay cartera. POST /api/bootstrap")
    return c


def _bloques_nombre(db: Session, cid: str) -> dict[str, str]:
    return {
        b.id: b.nombre for b in db.execute(
            select(models.Bloque).where(models.Bloque.cartera_id == cid)
        ).scalars()
    }


@router.get("", response_model=list[SeguimientoOut],
            summary="Lista de empresas en seguimiento con su valoración")
def listar(db: Session = Depends(get_db)) -> list[SeguimientoOut]:
    cid = _cartera(db).id
    segs = {
        s.isin: s for s in db.execute(
            select(models.Seguimiento).where(models.Seguimiento.cartera_id == cid)
        ).scalars()
    }
    bloques = _bloques_nombre(db, cid)
    calcs = {c.isin: c for c in svc.calcular_estimaciones_seguimiento(db, cid)}
    out = []
    for isin, s in segs.items():
        c = calcs.get(isin)
        if c is None:   # sin precio/estimación todavía
            continue
        out.append(SeguimientoOut(
            isin=s.isin, ticker=s.ticker, nombre=s.nombre, divisa=s.divisa,
            notas=s.notas, bloque_id=s.bloque_id,
            bloque_nombre=bloques.get(s.bloque_id) if s.bloque_id else None,
            estimacion=_to_out(c),
        ))
    out.sort(key=lambda x: (x.nombre or x.ticker).lower())
    return out


@router.post("", response_model=SeguimientoOut, status_code=status.HTTP_201_CREATED,
             summary="Añadir empresa al seguimiento por ticker (autorrellena estimación)")
def anadir(payload: AltaSeguimiento, db: Session = Depends(get_db)) -> SeguimientoOut:
    cid = _cartera(db).id
    info = precios_svc.resolver_ticker(payload.ticker)
    if not info or not info.get("isin"):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No se pudo resolver el ticker '{payload.ticker}'.")
    isin = info["isin"]

    # No duplicar con una posición ABIERTA. Una posición cerrada (vendida del todo)
    # sí se puede seguir: la tuviste, pero no está en la foto actual.
    ya_pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cid)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    if ya_pos is not None and estado_posicion(db, ya_pos.id)["cantidad"] > 0:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{info.get('nombre') or isin} ya está en tu cartera (posición abierta).")

    s = db.execute(
        select(models.Seguimiento)
        .where(models.Seguimiento.cartera_id == cid)
        .where(models.Seguimiento.isin == isin)
    ).scalars().first()
    if s is None:
        s = models.Seguimiento(
            cartera_id=cid, isin=isin, ticker=info["ticker"],
            nombre=info.get("nombre"), divisa=info.get("divisa"), notas=payload.notas,
        )
        db.add(s)
    else:   # re-alta: actualiza notas
        if payload.notas is not None:
            s.notas = payload.notas
    db.commit()

    svc.prefill_seguimiento(db, cid, isin, s.ticker)

    calcs = {c.isin: c for c in svc.calcular_estimaciones_seguimiento(db, cid)}
    c = calcs.get(isin)
    if c is None:   # no hubo precio; devuelve estimación vacía calculada
        c = svc._calc_item(isin, s.nombre or s.ticker, None, None, s.divisa)
    bloques = _bloques_nombre(db, cid)
    return SeguimientoOut(
        isin=s.isin, ticker=s.ticker, nombre=s.nombre, divisa=s.divisa,
        notas=s.notas, bloque_id=s.bloque_id,
        bloque_nombre=bloques.get(s.bloque_id) if s.bloque_id else None,
        estimacion=_to_out(c),
    )


@router.delete("/{isin}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Quitar empresa del seguimiento")
def quitar(isin: str, db: Session = Depends(get_db)) -> None:
    cid = _cartera(db).id
    s = db.execute(
        select(models.Seguimiento)
        .where(models.Seguimiento.cartera_id == cid)
        .where(models.Seguimiento.isin == isin)
    ).scalars().first()
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No está en seguimiento.")
    db.delete(s)
    db.commit()
