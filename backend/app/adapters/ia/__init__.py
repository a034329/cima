"""Adaptadores de IA + factory por configuración.

El swap CLI(Max) → API key es solo cambiar `CIMA_IA_PROVIDER`.
"""
from __future__ import annotations

from app.adapters.ia.base import (
    BloqueOpcion,
    ClasificadorError,
    ClasificadorIA,
    ContextoEmpresa,
    SugerenciaBloque,
)
from app.config import settings

__all__ = [
    "BloqueOpcion",
    "ClasificadorError",
    "ClasificadorIA",
    "ContextoEmpresa",
    "SugerenciaBloque",
    "get_clasificador",
]


def get_clasificador(proveedor: str | None = None) -> ClasificadorIA:
    """Devuelve el adaptador según `settings.ia_provider` (o el override dado)."""
    prov = proveedor or settings.ia_provider
    if prov == "claude_cli":
        from app.adapters.ia.claude_cli import ClaudeCliClasificador
        return ClaudeCliClasificador()
    if prov == "anthropic":
        from app.adapters.ia.anthropic_api import AnthropicClasificador
        return AnthropicClasificador()
    if prov == "mock":
        from app.adapters.ia.mock import MockClasificador
        return MockClasificador()
    raise ClasificadorError(f"Proveedor IA desconocido: {prov!r}")
