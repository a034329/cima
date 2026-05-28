"""PASO 0: contexto web + clasificación coyuntural/estructural de una empresa."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError
from app.db import get_db, models
from app.services import paso0 as svc


router = APIRouter(prefix="/contexto", tags=["contexto"])


class ContextoOut(BaseModel):
    isin: str
    nombre: str
    resumen: str
    clasificacion: str                       # COYUNTURAL | GRIS | ESTRUCTURAL | SIN_DATOS
    preguntas: list[dict]
    riesgo_principal: str
    fuentes: list[str]
    fecha: str
    proveedor: str
    disclaimer: str | None = None


def _cartera(db: Session) -> models.Cartera:
    c = db.execute(select(models.Cartera)).scalars().first()
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No hay cartera. Llama primero a POST /api/bootstrap")
    return c


@router.get("/{isin}", response_model=ContextoOut,
            summary="PASO 0: busca contexto web reciente y clasifica coyuntural/estructural")
def contexto(isin: str, db: Session = Depends(get_db)) -> ContextoOut:
    try:
        a = svc.analizar_contexto(db, _cartera(db).id, isin)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"IA con búsqueda web: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return ContextoOut(
        isin=a.isin, nombre=a.nombre, resumen=a.resumen, clasificacion=a.clasificacion,
        preguntas=a.preguntas, riesgo_principal=a.riesgo_principal, fuentes=a.fuentes,
        fecha=a.fecha, proveedor=a.proveedor, disclaimer=a.disclaimer,
    )
