"""Puerto del clasificador IA de bloques + tipos compartidos.

El backend depende SOLO de esta interfaz (`ClasificadorIA`); los adaptadores
concretos (CLI de Claude Max, API de Anthropic, mock) son intercambiables por
config. La IA nunca asigna: devuelve una SUGERENCIA que el usuario aprueba.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ContextoEmpresa:
    """Datos de la empresa que se pasan al modelo (retrieval, no foraging).
    Se ensamblan desde el feed que Cima ya tiene (precios + estimaciones)."""
    isin: str
    nombre: str
    sector: str | None = None
    industria: str | None = None
    divisa: str | None = None
    yield_pct: float | None = None          # dividendo / precio (fracción)
    dividendo_share: float | None = None
    per: float | None = None
    crecimiento_eps_pct: float | None = None  # CAGR del BPA (fracción)
    cagr4_div_pct: float | None = None        # retorno total anual estimado (fracción)
    market_cap: float | None = None
    tipo_activo: str | None = None            # STOCK | ETF | CRYPTO (para las compuertas)
    beta: float | None = None                 # volatilidad vs mercado (señal Estable)
    roe: float | None = None                  # return on equity (fracción) — proxy de calidad


@dataclass
class BloqueOpcion:
    """Un bloque candidato de la cartera (lo que el modelo puede elegir)."""
    id: str
    nombre: str
    categoria_base: str       # defensivo | income | growth | aggressive | colchon
    rol: str                  # descripción del rol WG de esa categoría


@dataclass
class SugerenciaBloque:
    """Salida del clasificador. `bloque_id` puede ser None si la categoría
    sugerida no tiene un bloque concreto en la cartera todavía. `isin` lo lleva
    cada sugerencia en modo lote (autoclasificar) para mapearla a su posición."""
    categoria_base: str
    bloque_id: str | None
    razonamiento: str
    confianza: float          # 0..1
    modelo: str
    proveedor: str            # claude_cli | anthropic | mock | regla
    isin: str | None = None
    distribucion: list[dict] | None = None   # [{categoria, prob}] opcional


class ClasificadorError(RuntimeError):
    """Fallo del proveedor de IA (timeout, sin auth, respuesta no parseable)."""


class ClasificadorIA(Protocol):
    """Puerto. Un adaptador implementa la clasificación puntual y por lote."""

    def clasificar(
        self, ctx: ContextoEmpresa, catalogo: list[BloqueOpcion],
        ejemplos: list[dict] | None = None,
    ) -> SugerenciaBloque: ...

    def clasificar_lote(
        self, empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion]
    ) -> list[SugerenciaBloque]:
        """Clasifica varias empresas. Optimizado para coste: una sola llamada con
        contexto comprimido y salida tersa (menos preciso que `clasificar`)."""
        ...

    def completar(self, system: str, user: str, timeout_s: int | None = None,
                  modelo: str | None = None) -> str:
        """Transporte genérico: devuelve el texto crudo del modelo para un par
        (system, user). Lo usan capacidades que arman su propio prompt+parser
        (p.ej. el onboarding, el asesor). `timeout_s` permite ampliar el límite
        para generaciones largas (planes multi-paso). `modelo` fuerza un modelo
        concreto (None = el por defecto del proveedor). El swap de proveedor es
        transparente."""
        ...

    def investigar(self, system: str, user: str, timeout_s: int | None = None,
                   modelo: str | None = None) -> str:
        """Como `completar` pero CON búsqueda web (PASO 0: contexto cualitativo).
        En dev (Max CLI) usa la tool WebSearch pre-aprobada; en la API usará el
        server-tool `web_search`. Más lento y no determinista que `completar`.
        `timeout_s` permite acortar para chat conversacional vs análisis profundo.
        `modelo` fuerza un modelo concreto (None = el por defecto del proveedor)."""
        ...
