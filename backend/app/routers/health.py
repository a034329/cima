"""Endpoints de salud — sin auth, accesibles públicamente."""
from __future__ import annotations

from datetime import datetime, UTC

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.config import settings


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    mode: str
    environment: str
    timestamp: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Endpoint de liveness/readiness check.

    Devuelve siempre 200 mientras el proceso esté vivo. Lo usan Railway
    health checks y cualquier monitor externo.
    """
    return HealthResponse(
        status="ok",
        service="cima-api",
        version=__version__,
        mode=settings.mode.value,
        environment=settings.environment,
        timestamp=datetime.now(UTC).isoformat(),
    )
