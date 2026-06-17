"""Endpoint de bootstrap — crea user + cartera + brokers iniciales.

Esto es un atajo de desarrollo. En producción, la creación del user llegará
vía Supabase Auth, y la cartera + brokers se crean en el onboarding IA.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia.prompt import FICHAS
from app.db import get_db, models


router = APIRouter(prefix="/bootstrap", tags=["bootstrap (dev)"])


class BootstrapOut(BaseModel):
    user_id: str
    cartera_id: str
    brokers: dict[str, str]      # tipo → broker_id
    creado: bool                 # True si se creó nuevo, False si ya existía


class BootstrapIn(BaseModel):
    email: EmailStr = "owner@cima.local"
    nombre_cartera: str = "Cartera principal"


@router.post(
    "",
    response_model=BootstrapOut,
    summary="Bootstrap user + cartera + brokers (idempotente)",
)
def bootstrap(
    payload: BootstrapIn | None = None,
    db: Session = Depends(get_db),
) -> BootstrapOut:
    """Crea user + cartera + brokers (DEGIRO, IBKR, TR, Trading 212, ING,
    MyInvestor) si no existen. Idempotente — si ya hay user con ese email,
    devuelve sus IDs.
    """
    payload = payload or BootstrapIn()
    from app.config import settings
    from app.services.provisioning import provision_user

    # Atajo de dev/seed. En SaaS NO concede acceso: no emite token y deja
    # password_hash=None (login imposible, get_current_user exige token). El
    # alta real con credenciales es POST /api/auth/signup. (El squatting de
    # email — provisionar un email ajeno sin password para bloquear su signup
    # — queda anotado para el endurecimiento de Fase D.)
    user, creado = provision_user(
        db, payload.email, modo=settings.mode.value,
        nombre_cartera=payload.nombre_cartera,
    )
    cartera = db.execute(
        select(models.Cartera).where(models.Cartera.user_id == user.id)
    ).scalar_one()
    brokers_out = {
        b.broker_tipo: b.id
        for b in db.execute(
            select(models.Broker).where(models.Broker.user_id == user.id)
        ).scalars()
    }
    return BootstrapOut(
        user_id=user.id,
        cartera_id=cartera.id,
        brokers=brokers_out,
        creado=creado,
    )
