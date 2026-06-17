"""Chat con el asesor financiero IA (hilo por cartera)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError
from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import asesor as svc


router = APIRouter(prefix="/asesor", tags=["asesor"])


class MensajeOut(BaseModel):
    rol: str                 # user | assistant
    contenido: str
    created_at: str


class AccionOut(BaseModel):
    tipo: str
    isin: str
    descripcion: str
    params: dict


class RespuestaOut(BaseModel):
    mensaje: MensajeOut
    acciones: list[AccionOut]


class EnviarIn(BaseModel):
    mensaje: str
    # True si la pregunta entró por VOZ → la IA responde de forma conversacional
    # (sin markdown, sin URLs, frases naturales para sintetizar a audio).
    por_voz: bool = False
    # True si el usuario pulsó el toggle 🌐 del chat → fuerza `investigar` (web)
    # aunque la heurística no haya disparado. Necesario para preguntas tipo
    # "investiga X" o "qué herramientas tienes" que la heurística no cubre.
    forzar_web: bool = False


def _out(m: models.MensajeAsesor) -> MensajeOut:
    return MensajeOut(rol=m.rol, contenido=m.contenido, created_at=m.created_at.isoformat())


@router.get("", response_model=list[MensajeOut], summary="Historial del chat con el asesor")
def get_historial(limit: int = 200, db: Session = Depends(get_db),
                  cartera: models.Cartera = Depends(get_current_cartera)) -> list[MensajeOut]:
    return [_out(m) for m in svc.historial(db, cartera.id, limit=min(limit, 1000))]


@router.post("", response_model=RespuestaOut, summary="Enviar un mensaje al asesor y obtener su respuesta")
def enviar(payload: EnviarIn, db: Session = Depends(get_db),
           cartera: models.Cartera = Depends(get_current_cartera)) -> RespuestaOut:
    texto = payload.mensaje.strip()
    if not texto:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mensaje vacío")
    try:
        m, acciones = svc.responder(db, cartera.id, texto,
                                    por_voz=payload.por_voz,
                                    forzar_web=payload.forzar_web)
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Asesor IA: {e}")
    except NotImplementedError as e:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(e))
    return RespuestaOut(
        mensaje=_out(m),
        acciones=[AccionOut(tipo=a.tipo, isin=a.isin, descripcion=a.descripcion, params=a.params)
                  for a in acciones],
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, summary="Limpiar la conversación")
def limpiar(db: Session = Depends(get_db),
            cartera: models.Cartera = Depends(get_current_cartera)) -> None:
    svc.limpiar(db, cartera.id)
