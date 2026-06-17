"""Configuración de la cartera: perfil, objetivo IF, modo y brokers.

Sin auth todavía: opera sobre la primera cartera/usuario disponibles. El modo
(SaaS/Owner) viene del entorno (CIMA_MODE) y es de solo lectura aquí.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_cartera
from app.config import settings
from app.db import get_db, models


router = APIRouter(prefix="/config", tags=["config"])


class BrokerOut(BaseModel):
    broker_tipo: str
    alias: str | None = None
    saldo_reportado_eur: Decimal | None = None
    saldo_fecha: str | None = None


class ConfigOut(BaseModel):
    email: str
    nombre_cartera: str
    modo: str                       # 'saas' | 'owner' (del entorno, solo lectura)
    objetivo_if_eur: Decimal = Field(decimal_places=2)
    aportacion_mensual_eur: Decimal = Field(decimal_places=2)
    brokers: list[BrokerOut]


class ConfigIn(BaseModel):
    nombre_cartera: str | None = None
    objetivo_if_eur: Decimal | None = None
    aportacion_mensual_eur: Decimal | None = None


def _serializar(db: Session, cartera: models.Cartera) -> ConfigOut:
    user = db.get(models.User, cartera.user_id)
    brokers = db.execute(
        select(models.Broker).where(models.Broker.user_id == cartera.user_id)
    ).scalars().all()
    return ConfigOut(
        email=user.email if user else "—",
        nombre_cartera=cartera.nombre,
        modo=settings.mode.value,
        objetivo_if_eur=Decimal(str(cartera.objetivo_if_eur)).quantize(Decimal("0.01")),
        aportacion_mensual_eur=Decimal(str(cartera.aportacion_mensual_eur or 0)).quantize(Decimal("0.01")),
        brokers=[
            BrokerOut(
                broker_tipo=b.broker_tipo, alias=b.alias,
                saldo_reportado_eur=b.saldo_reportado_eur,
                saldo_fecha=b.saldo_fecha.isoformat() if b.saldo_fecha else None,
            )
            for b in brokers
        ],
    )


@router.get("", response_model=ConfigOut, summary="Configuración de la cartera")
def get_config(db: Session = Depends(get_db),
               cartera: models.Cartera = Depends(get_current_cartera)) -> ConfigOut:
    return _serializar(db, cartera)


@router.patch("", response_model=ConfigOut, summary="Actualizar configuración")
def patch_config(payload: ConfigIn, db: Session = Depends(get_db),
                 cartera: models.Cartera = Depends(get_current_cartera)) -> ConfigOut:
    if payload.nombre_cartera is not None:
        nombre = payload.nombre_cartera.strip()
        if not nombre:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "El nombre no puede estar vacío")
        cartera.nombre = nombre
    if payload.objetivo_if_eur is not None:
        if payload.objetivo_if_eur <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "El objetivo IF debe ser > 0")
        cartera.objetivo_if_eur = Decimal(str(payload.objetivo_if_eur)).quantize(
            Decimal("0.01"), ROUND_HALF_UP)
    if payload.aportacion_mensual_eur is not None:
        if payload.aportacion_mensual_eur < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "La aportación no puede ser negativa")
        cartera.aportacion_mensual_eur = Decimal(str(payload.aportacion_mensual_eur)).quantize(
            Decimal("0.01"), ROUND_HALF_UP)
    db.commit()
    return _serializar(db, cartera)
