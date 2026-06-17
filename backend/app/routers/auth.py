"""Endpoints de autenticación propia (ADR-003): signup, login, me.

Auth propia con JWT HS256 + scrypt (stdlib, sin vendor). Magic link y OAuth
quedan fuera del MVP (requieren SMTP/proveedor). El endurecimiento (rate-limit,
lockout) es Fase D.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.passwords import hash_password, verify_password
from app.auth.ratelimit import LoginRateLimiter, make_key
from app.auth.tokens import create_access_token
from app.config import settings
from app.db import get_db, models

router = APIRouter(prefix="/auth", tags=["auth"])

# Limitador de login en memoria (Fase D). Instancia única por proceso.
_login_limiter = LoginRateLimiter(
    max_fails=settings.login_max_fails,
    window_s=settings.login_window_s,
    lockout_s=settings.login_lockout_s,
)


def _client_ip(request: Request) -> str | None:
    # Detrás de proxy (Railway) la IP real va en X-Forwarded-For (primer salto).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


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
        # Cuenta SIN contraseña (provisionada por /bootstrap, p.ej. squatting o
        # un seed de dev): el signup la "reclama" fijando la contraseña en vez
        # de devolver 409. Cierra el squatting de email de la Fase B/D.
        if existente.password_hash is None:
            existente.password_hash = hash_password(cred.password)
            db.commit()
            token = create_access_token(sub=existente.id, email=existente.email)
            return TokenOut(access_token=token, user_id=existente.id, email=existente.email)
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
def login(cred: Credenciales, request: Request,
          db: Session = Depends(get_db)) -> TokenOut:
    email = cred.email.strip().lower()
    key = make_key(_client_ip(request), email)

    # Lockout: si este (IP, email) está bloqueado por fallos previos → 429.
    retry = _login_limiter.retry_after(key)
    if retry > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos. Prueba de nuevo más tarde.",
            headers={"Retry-After": str(retry)},
        )

    user = db.execute(
        select(models.User).where(models.User.email == email)
    ).scalar_one_or_none()
    # Mismo mensaje para usuario inexistente y contraseña incorrecta (no
    # revelar qué emails existen). verify_password es constante en tiempo.
    if user is None or not verify_password(cred.password, user.password_hash):
        _login_limiter.record_fail(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos.",
        )
    _login_limiter.reset(key)  # éxito → limpia el contador
    token = create_access_token(sub=user.id, email=user.email)
    return TokenOut(access_token=token, user_id=user.id, email=user.email)


@router.get("/me", response_model=MeOut, summary="Usuario autenticado actual")
def me(user: models.User = Depends(get_current_user)) -> MeOut:
    return MeOut(user_id=user.id, email=user.email, modo=user.modo)


@router.post("/refresh", response_model=TokenOut,
             summary="Renueva el access token (ventana deslizante)")
def refresh(user: models.User = Depends(get_current_user)) -> TokenOut:
    """Emite un token nuevo si el actual aún es válido. El front lo llama de
    forma proactiva para que la sesión no caduque mientras se usa (evita el 401
    en SSR por token expirado). Un token ya caducado NO se renueva (get_current_user
    lo rechaza) → re-login."""
    token = create_access_token(sub=user.id, email=user.email)
    return TokenOut(access_token=token, user_id=user.id, email=user.email)
