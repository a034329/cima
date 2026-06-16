"""Endpoints de autenticación propia (ADR-003): signup, login, me.

Auth propia con JWT HS256 + scrypt (stdlib, sin vendor). Magic link y OAuth
quedan fuera del MVP (requieren SMTP/proveedor). El endurecimiento (rate-limit,
lockout) es Fase D.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import create_access_token
from app.config import settings
from app.db import get_db, models

router = APIRouter(prefix="/auth", tags=["auth"])


class Credenciales(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str


class MeOut(BaseModel):
    user_id: str
    email: str
    modo: str


@router.post("/signup", response_model=TokenOut, status_code=status.HTTP_201_CREATED,
             summary="Alta de usuario (email + contraseña) y emisión de token")
def signup(cred: Credenciales, db: Session = Depends(get_db)) -> TokenOut:
    from app.services.provisioning import provision_user

    email = cred.email.strip().lower()
    existente = db.execute(
        select(models.User).where(models.User.email == email)
    ).scalar_one_or_none()
    if existente is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe una cuenta con ese email.",
        )
    user, _ = provision_user(
        db, email, modo=settings.mode.value,
        password_hash=hash_password(cred.password),
    )
    token = create_access_token(sub=user.id, email=user.email)
    return TokenOut(access_token=token, user_id=user.id, email=user.email)


@router.post("/login", response_model=TokenOut,
             summary="Login (email + contraseña) y emisión de token")
def login(cred: Credenciales, db: Session = Depends(get_db)) -> TokenOut:
    email = cred.email.strip().lower()
    user = db.execute(
        select(models.User).where(models.User.email == email)
    ).scalar_one_or_none()
    # Mismo mensaje para usuario inexistente y contraseña incorrecta (no
    # revelar qué emails existen). verify_password es constante en tiempo.
    if user is None or not verify_password(cred.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos.",
        )
    token = create_access_token(sub=user.id, email=user.email)
    return TokenOut(access_token=token, user_id=user.id, email=user.email)


@router.get("/me", response_model=MeOut, summary="Usuario autenticado actual")
def me(user: models.User = Depends(get_current_user)) -> MeOut:
    return MeOut(user_id=user.id, email=user.email, modo=user.modo)
