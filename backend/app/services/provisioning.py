"""Provisioning de usuario: crea User + Cartera + brokers + catálogo de bloques.

Lógica extraída de `/api/bootstrap` (ADR-003) para reutilizarla desde el signup
(modo SaaS) y desde el puente del modo owner. Idempotente: si el email ya existe
devuelve el usuario tal cual (rellenando lo que falte: cartera, brokers, bloques).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia.prompt import FICHAS
from app.db import models

_BROKERS_DEFAULT = ("degiro", "ibkr", "tr", "trading212", "ing", "myinvestor")


def provision_user(
    db: Session,
    email: str,
    modo: str,
    *,
    nombre_cartera: str = "Cartera principal",
    password_hash: str | None = None,
) -> tuple[models.User, bool]:
    """Crea (o completa) user + cartera + brokers + bloques base. Devuelve
    (user, creado). `creado` es True si se creó algo nuevo. No hace commit final
    de forma agresiva: deja el commit al caller salvo que cree filas (entonces
    commitea para fijar los ids)."""
    email = email.strip().lower()
    creado = False

    user = db.execute(
        select(models.User).where(models.User.email == email)
    ).scalar_one_or_none()
    if user is None:
        user = models.User(email=email, modo=modo, password_hash=password_hash)
        db.add(user)
        db.flush()
        creado = True

    cartera = db.execute(
        select(models.Cartera).where(models.Cartera.user_id == user.id)
    ).scalar_one_or_none()
    if cartera is None:
        cartera = models.Cartera(user_id=user.id, nombre=nombre_cartera)
        db.add(cartera)
        db.flush()
        creado = True

    existentes = {
        b.broker_tipo for b in db.execute(
            select(models.Broker).where(models.Broker.user_id == user.id)
        ).scalars()
    }
    for tipo in _BROKERS_DEFAULT:
        if tipo not in existentes:
            db.add(models.Broker(user_id=user.id, broker_tipo=tipo,
                                 alias=tipo.upper()))
            creado = True

    tiene_bloques = db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera.id)
    ).first()
    if tiene_bloques is None:
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

    if creado:
        db.commit()
        db.refresh(user)
    return user, creado
