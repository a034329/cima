"""Onboarding IA (1.5): proponer estrategia con la IA y firmarla."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError
from app.auth.deps import get_current_cartera
from app.db import get_db, models
from app.services import onboarding as svc

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class PerfilIn(BaseModel):
    objetivo_if_eur: Decimal | None = None
    horizonte_anios: float | None = None     # admite medios años (p.ej. 2,5)
    aportacion_mensual_eur: Decimal | None = None
    tolerancia: str | None = None        # conservador | moderado | agresivo
    fase: str | None = None              # acumulacion | preservacion


class PropuestaBloqueOut(BaseModel):
    categoria_base: str
    peso_objetivo: float
    razon: str


class ViabilidadOut(BaseModel):
    capital_actual_eur: float
    aportaciones_eur: float
    cagr_requerido_pct: float | None
    viable: bool
    veredicto: str


class PropuestaOut(BaseModel):
    bloques: list[PropuestaBloqueOut]
    resumen: str
    disclaimer: str | None = None
    viabilidad: ViabilidadOut | None = None


class FirmarIn(BaseModel):
    perfil: PerfilIn
    objetivos: dict[str, float]          # {categoria_base: peso}


class PlanFirmadoOut(BaseModel):
    version: int
    perfil: dict
    objetivos: dict
    resumen: str | None = None
    fecha: str


@router.post("/proponer", response_model=PropuestaOut,
             summary="La IA propone un reparto de bloques según el perfil")
def proponer(payload: PerfilIn, db: Session = Depends(get_db),
             cartera: models.Cartera = Depends(get_current_cartera)) -> PropuestaOut:
    try:
        p = svc.proponer_estrategia(db, cartera.id, payload.model_dump())
    except ClasificadorError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"IA no disponible: {e}") from e
    v = p.viabilidad
    return PropuestaOut(
        bloques=[PropuestaBloqueOut(categoria_base=b.categoria_base,
                                    peso_objetivo=b.peso_objetivo, razon=b.razon)
                 for b in p.bloques],
        resumen=p.resumen, disclaimer=p.disclaimer,
        viabilidad=ViabilidadOut(
            capital_actual_eur=v.capital_actual_eur, aportaciones_eur=v.aportaciones_eur,
            cagr_requerido_pct=v.cagr_requerido_pct, viable=v.viable, veredicto=v.veredicto,
        ) if v else None,
    )


@router.post("/firmar", response_model=PlanFirmadoOut, status_code=status.HTTP_201_CREATED,
             summary="Firma el plan: aplica los objetivos a los bloques y lo versiona")
def firmar(payload: FirmarIn, db: Session = Depends(get_db),
           cartera: models.Cartera = Depends(get_current_cartera)) -> PlanFirmadoOut:
    plan = svc.firmar_plan(db, cartera.id, payload.perfil.model_dump(), payload.objetivos)
    return _plan_out(plan)


@router.get("/plan", response_model=PlanFirmadoOut | None,
            summary="Último plan firmado (o null)")
def plan(db: Session = Depends(get_db),
         cartera: models.Cartera = Depends(get_current_cartera)) -> PlanFirmadoOut | None:
    p = svc.plan_firmado_actual(db, cartera.id)
    return _plan_out(p) if p else None


def _plan_out(p: models.PlanFirmado) -> PlanFirmadoOut:
    import json
    return PlanFirmadoOut(
        version=p.version, perfil=json.loads(p.perfil_json),
        objetivos=json.loads(p.objetivos_json), resumen=p.resumen,
        fecha=p.created_at.isoformat(),
    )
