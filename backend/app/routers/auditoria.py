"""Auditoría pre-operación: corre los filtros de la doctrina para una compra."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError
from app.db import get_db, models
from app.services import auditoria as svc


router = APIRouter(prefix="/auditoria", tags=["auditoria"])


class ChequeoOut(BaseModel):
    filtro: str
    estado: str            # OK | AVISO | INFO | VERIFICAR
    titulo: str
    detalle: str


class AuditoriaOut(BaseModel):
    isin: str
    nombre: str
    decision: str
    bloque_objetivo: str | None = None
    chequeos: list[ChequeoOut]
    resumen: str


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


@router.get("/{isin}", response_model=AuditoriaOut,
            summary="Auditoría pre-operación (los filtros de la doctrina) para una compra")
def auditar(isin: str, decision: str = "COMPRAR", bloque: str | None = None,
            db: Session = Depends(get_db)) -> AuditoriaOut:
    cid = _cartera(db).id
    try:
        if decision in ("VENDER", "RECORTAR"):
            a = svc.auditar_venta(db, cid, isin)
        elif decision == "COMPRAR":
            a = svc.auditar_compra(db, cid, isin, bloque)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"decisión no auditada: {decision}")
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Clasificador IA: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return AuditoriaOut(
        isin=a.isin, nombre=a.nombre, decision=a.decision, bloque_objetivo=a.bloque_objetivo,
        chequeos=[ChequeoOut(filtro=c.filtro, estado=c.estado, titulo=c.titulo, detalle=c.detalle)
                  for c in a.chequeos],
        resumen=a.resumen,
    )
