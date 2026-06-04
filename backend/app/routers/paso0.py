"""PASO 0: contexto web + clasificación coyuntural/estructural de una empresa.

PASO 0B: segunda búsqueda dirigida a la causa raíz, activada manualmente por
el usuario cuando el backend marca `requiere_0b` (modo híbrido).
"""
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
    requiere_0b: bool = False
    motivo_0b: str = ""


class ContextoPrevio(BaseModel):
    """Datos del PASO 0 que el frontend pasa al 0B para no rehacer la 1ª pasada."""
    resumen: str = ""
    clasificacion: str = ""
    riesgo_principal: str = ""


class CausaRaizOut(BaseModel):
    isin: str
    nombre: str
    causa_exacta: str
    profundidad: str                         # LIGERA | MEDIA | GRAVE | SIN_DATOS
    horizonte_resolucion: str
    segmentos_afectados: list[dict]
    evidencias: list[str]
    conclusion: str
    nueva_clasificacion: str                 # "" = mantiene la previa
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
        requiere_0b=a.requiere_0b, motivo_0b=a.motivo_0b,
    )


@router.post("/{isin}/causa-raiz", response_model=CausaRaizOut,
             summary="PASO 0B: 2ª búsqueda dirigida a la causa raíz (segmento, profundidad, horizonte)")
def causa_raiz(
    isin: str,
    contexto_previo: ContextoPrevio | None = None,
    db: Session = Depends(get_db),
) -> CausaRaizOut:
    """POST porque acepta body opcional con el contexto del PASO 0. Sin body, la
    IA arranca a frío y descubre el evento de partida también — más lento."""
    prev = contexto_previo.model_dump() if contexto_previo else None
    try:
        a = svc.analizar_causa_raiz(db, _cartera(db).id, isin, prev)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"IA con búsqueda web: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return CausaRaizOut(
        isin=a.isin, nombre=a.nombre, causa_exacta=a.causa_exacta,
        profundidad=a.profundidad, horizonte_resolucion=a.horizonte_resolucion,
        segmentos_afectados=a.segmentos_afectados, evidencias=a.evidencias,
        conclusion=a.conclusion, nueva_clasificacion=a.nueva_clasificacion,
        fuentes=a.fuentes, fecha=a.fecha, proveedor=a.proveedor,
        disclaimer=a.disclaimer,
    )
