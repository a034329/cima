"""Cima — punto de entrada FastAPI.

Ejecutar en desarrollo:
    cd /app/cima/backend
    uvicorn app.main:app --reload --port 8000

OpenAPI:
    http://localhost:8000/docs
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import settings
from app.db import init_db
from app.routers import (
    aportaciones, bills, bloques, bootstrap, cartera, complejos, config,
    dashboard, dividendos, estimaciones, fiscal, forex, health, importar,
    intereses, liquidez, mantenimiento, opciones, plan, posiciones,
    seguimiento, transacciones,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown del backend."""
    # Crear tablas si no existen (idempotente). En cuanto el schema evolucione
    # con datos en producción, sustituir por Alembic migrations.
    init_db()
    print(
        f"[cima] arrancado en modo {settings.mode.value} · "
        f"env={settings.environment} · v{__version__} · db={settings.database_url}"
    )
    yield
    print("[cima] apagando")


app = FastAPI(
    title="Cima API",
    description=(
        "Tracker con estrategia desde el primer día + motor fiscal español "
        "completo para inversores con cartera compleja."
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

# ── CORS ───────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────
app.include_router(health.router, prefix="/api")
app.include_router(cartera.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(transacciones.router, prefix="/api")
app.include_router(importar.router, prefix="/api")
app.include_router(bootstrap.router, prefix="/api")
app.include_router(fiscal.router, prefix="/api")
app.include_router(opciones.router, prefix="/api")
app.include_router(dividendos.router, prefix="/api")
app.include_router(posiciones.router, prefix="/api")
app.include_router(aportaciones.router, prefix="/api")
app.include_router(liquidez.router, prefix="/api")
app.include_router(forex.router, prefix="/api")
app.include_router(intereses.router, prefix="/api")
app.include_router(bills.router, prefix="/api")
app.include_router(complejos.router, prefix="/api")
app.include_router(bloques.router, prefix="/api")
app.include_router(plan.router, prefix="/api")
app.include_router(config.router, prefix="/api")
app.include_router(estimaciones.router, prefix="/api")
app.include_router(seguimiento.router, prefix="/api")
app.include_router(mantenimiento.router, prefix="/api")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Redirige al documento OpenAPI."""
    return {
        "service": "cima-api",
        "version": __version__,
        "mode": settings.mode.value,
        "docs": "/docs",
    }
