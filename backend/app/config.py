"""Configuración global de Cima backend.

Carga desde variables de entorno (con prefijo `CIMA_`) y/o `.env`.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    """Modo de ejecución del backend.

    - SAAS  : producción con clientes externos. IA capada, disclaimers MiFID,
              decisión humana explícita.
    - OWNER : instancia personal del fundador. IA sin restricciones, agente
              externo puede operar vía API/MCP. Defensible legalmente porque
              el usuario es a la vez prestador y cliente.
    """

    SAAS = "saas"
    OWNER = "owner"


class Settings(BaseSettings):
    """Settings cargados desde env vars / .env."""

    model_config = SettingsConfigDict(
        env_prefix="CIMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Modo de ejecución ───────────────────────────────────────────────
    mode: Mode = Field(default=Mode.SAAS, description="Modo SaaS o Owner")

    # ── Servidor ────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    environment: Literal["dev", "staging", "production"] = "dev"

    # ── CORS ────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000"]

    # ── BD ──────────────────────────────────────────────────────────────
    # Default: SQLite local (archivo `cima.db` en backend/). Producción: Postgres.
    database_url: str = "sqlite:///./cima.db"

    # ── IA: proveedor del clasificador de bloques ──────────────────────
    # claude_cli : dev — tira de la suscripción Claude Max vía el CLI headless
    #              `claude -p` (no consume API key). Por defecto.
    # anthropic  : prod — SDK de Anthropic con API key (adaptador stub por ahora).
    # mock       : tests — regla determinista offline.
    # El swap es solo cambiar esta variable: ambos adaptadores comparten el mismo
    # constructor de prompt (adapters/ia/prompt.py).
    ia_provider: Literal["claude_cli", "anthropic", "mock"] = "claude_cli"
    ia_cli_path: str = "claude"        # binario del CLI de Claude Code
    ia_timeout_s: int = 150            # timeout por llamada (clasificación; rápido)
    ia_chat_timeout_s: int = 300       # asesor/onboarding: generaciones largas (plan multi-paso) sin web
    ia_web_timeout_s: int = 600        # timeout para PASO 0/one-pager/valoración/comps (búsqueda web: hasta 10 min)
    ia_chat_web_timeout_s: int = 180   # asesor con web: conversacional, 3 min máx (no análisis profundo)
    ia_lote_chunk: int = 12            # empresas por llamada en autoclasificar (trocea
    #                                    el lote para que cada generación quede acotada)
    # Modelo del autoclasificar en lote. Experimento 2026-05-24 (n=31): opus en
    # lote terso salió más preciso, rápido y barato que sonnet (sonnet ignoraba el
    # "sé conciso" y se enrollaba). El puntual usa anthropic_default_model.
    ia_lote_model: str = "claude-opus-4-7"

    # ── Anthropic IA (usado por el adaptador 'anthropic') ──────────────
    anthropic_api_key: str = ""
    anthropic_default_model: str = "claude-sonnet-4-6"

    # ── Datos de mercado: Financial Modeling Prep ──────────────────────
    # Clave vía env (CIMA_FMP_API_KEY). NUNCA hardcodear en el repo. Sin clave,
    # el feed cae a yfinance (degradado, sin consenso de analistas).
    fmp_api_key: str = ""

    # ── Motor fiscal de Cuádrate (vendorizado) ─────────────────────────
    # Vacío → usa la COPIA vendorizada en backend/vendor/cuadrate (commiteada,
    # disponible en el contenedor). Se puede apuntar al origen en dev con
    # CIMA_CUADRATE_IRPF_PATH=/app/720/irpf. Sincronizar con scripts/sync_cuadrate.py.
    cuadrate_irpf_path: str = ""

    # ── Storage de extractos brutos (Roadmap 1.9 CSV approach) ─────────
    # Donde Cima guarda los CSVs originales de broker para re-pasárselos a
    # `generar_irpf.main()` y entregar la declaración completa. Estructura:
    #   {storage_dir}/extractos/{cartera_id}/{ejercicio}/{kind}.csv
    # Vacío → backend/storage/. Override en producción con un volumen montado.
    storage_dir: str = ""

    # ── Sentry (placeholder) ───────────────────────────────────────────
    sentry_dsn: str = ""

    @property
    def is_owner_mode(self) -> bool:
        """True si el backend está en modo Owner — desbloquea IA sin capar."""
        return self.mode == Mode.OWNER

    @property
    def is_saas_mode(self) -> bool:
        return self.mode == Mode.SAAS


# Instancia singleton. Importar `settings` desde el resto del código.
settings = Settings()
