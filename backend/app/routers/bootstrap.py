"""Endpoint de bootstrap — crea user + cartera + brokers iniciales.

Esto es un atajo de desarrollo. En producción, la creación del user llegará
vía Supabase Auth, y la cartera + brokers se crean en el onboarding IA.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
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

    user = db.execute(
        select(models.User).where(models.User.email == payload.email)
    ).scalar_one_or_none()
    creado = False
    if user is None:
        # El modo del usuario refleja el modo de despliegue (S1 auditoría:
        # "owner" hardcodeado persistía usuarios owner en BD aunque el
        # backend corriera como SaaS — mordería al llegar multi-usuario).
        from app.config import settings
        user = models.User(email=payload.email, modo=settings.mode.value)
        db.add(user)
        db.flush()
        creado = True

    cartera = db.execute(
        select(models.Cartera).where(models.Cartera.user_id == user.id)
    ).scalar_one_or_none()
    if cartera is None:
        cartera = models.Cartera(user_id=user.id, nombre=payload.nombre_cartera)
        db.add(cartera)
        db.flush()
        creado = True

    brokers_existentes = {
        b.broker_tipo: b
        for b in db.execute(
            select(models.Broker).where(models.Broker.user_id == user.id)
        ).scalars()
    }

    brokers_out: dict[str, str] = {}
    for tipo in ("degiro", "ibkr", "tr", "trading212", "ing", "myinvestor"):
        if tipo in brokers_existentes:
            brokers_out[tipo] = brokers_existentes[tipo].id
            continue
        broker = models.Broker(user_id=user.id, broker_tipo=tipo, alias=tipo.upper())
        db.add(broker)
        db.flush()
        brokers_out[tipo] = broker.id
        creado = True

    # Sembrar catálogo base de bloques (idempotente: solo si la cartera no
    # tiene ninguno). 'Sin clasificar' NO es una fila — es bloque_id NULL.
    tiene_bloques = db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera.id)
    ).first()
    if tiene_bloques is None:
        # Catálogo base = las fichas con es_base (6). Las opcionales (indice,
        # renta_fija) NO se siembran: el usuario las añade si las usa.
        base = sorted(
            ((cod, f) for cod, f in FICHAS.items() if f.es_base),
            key=lambda cf: cf[1].orden,
        )
        for cod, f in base:
            db.add(models.Bloque(
                cartera_id=cartera.id, nombre=f.nombre, categoria_base=cod,
                orden=f.orden, es_base=True, en_estrategia=f.en_estrategia,
            ))
        creado = True

    db.commit()
    return BootstrapOut(
        user_id=user.id,
        cartera_id=cartera.id,
        brokers=brokers_out,
        creado=creado,
    )


@router.get("", summary="Estado del bootstrap")
def estado_bootstrap(db: Session = Depends(get_db)) -> dict[str, object]:
    user = db.execute(select(models.User)).scalars().first()
    cartera = db.execute(select(models.Cartera)).scalars().first()
    brokers = db.execute(select(models.Broker)).scalars().all()
    return {
        "tiene_user": user is not None,
        "tiene_cartera": cartera is not None,
        "n_brokers": len(brokers),
        "brokers_tipos": sorted({b.broker_tipo for b in brokers}),
    }
