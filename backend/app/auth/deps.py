"""Dependencia `get_current_user` (ADR-003).

- **Modo owner**: la auth se PUENTEA — se provisiona/devuelve el usuario único
  `settings.owner_email` sin exigir token. El uso local del fundador no depende
  del login.
- **Modo saas**: exige `Authorization: Bearer <jwt>`; verifica el token y busca
  el usuario por su id (`sub`). 401 si falta, es inválido o el usuario ya no
  existe. El alta ocurre en el signup (no aquí).
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.tokens import TokenError, decode_token
from app.config import settings
from app.db import get_db, models


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    # ── Modo owner: sin login, usuario único provisionado ──
    if settings.is_owner_mode:
        from app.services.provisioning import provision_user
        user, _ = provision_user(db, settings.owner_email, modo="owner")
        return user

    # ── Modo saas: Bearer obligatorio ──
    token = _bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token de autenticación",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(token)
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token inválido: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    user = db.get(models.User, claims.get("sub"))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El usuario del token ya no existe",
        )
    return user


def get_current_cartera(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> models.Cartera:
    """La cartera del usuario autenticado (1:1 por ahora) — SCOPING multi-tenant.
    Sustituye los `select(Cartera).first()` que cogían "la primera" (IDOR por
    diseño). En modo owner `user` es el owner provisionado → su cartera; en saas
    es el del token. 404 si el usuario aún no tiene cartera (no debería tras el
    provisioning)."""
    from sqlalchemy import select

    cartera = db.execute(
        select(models.Cartera).where(models.Cartera.user_id == user.id)
    ).scalars().first()
    if cartera is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay cartera para este usuario. Llama primero a POST /api/bootstrap",
        )
    return cartera
