"""Adaptador de producción: API de Anthropic con API key.

Stub estructurado. El cuerpo de `clasificar` queda por implementar a propósito
(posponemos el pago de API): la transición es rellenar este método usando el
MISMO `build_mensajes`/`parse_respuesta` que el resto de adaptadores. Al
activarlo, invocar la skill `claude-api` para prompt caching y `max_tokens`.
"""
from __future__ import annotations

from app.adapters.ia.base import (
    BloqueOpcion,
    ClasificadorError,
    ContextoEmpresa,
    SugerenciaBloque,
)
from app.adapters.ia.prompt import build_mensajes, parse_respuesta  # noqa: F401
from app.config import settings

_PROVEEDOR = "anthropic"


class AnthropicClasificador:
    """Implementa el puerto ClasificadorIA vía SDK de Anthropic."""

    def __init__(self, api_key: str | None = None, modelo: str | None = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self.modelo = modelo or settings.anthropic_default_model

    def clasificar(
        self, ctx: ContextoEmpresa, catalogo: list[BloqueOpcion],
        ejemplos: list[dict] | None = None,
    ) -> SugerenciaBloque:
        if not self.api_key:
            raise ClasificadorError(
                "CIMA_ANTHROPIC_API_KEY vacío. Configura la key o usa "
                "CIMA_IA_PROVIDER=claude_cli (Max)."
            )
        # TODO(api): implementar con el SDK reutilizando build_mensajes/parse_respuesta:
        #   client = anthropic.Anthropic(api_key=self.api_key)
        #   system, user = build_mensajes(ctx, catalogo)
        #   msg = client.messages.create(
        #       model=self.modelo, max_tokens=512,
        #       system=[{"type": "text", "text": system,
        #                "cache_control": {"type": "ephemeral"}}],   # prompt caching
        #       messages=[{"role": "user", "content": user}])
        #   return parse_respuesta(msg.content[0].text, catalogo, self.modelo, _PROVEEDOR)
        raise NotImplementedError(
            "Adaptador Anthropic pendiente — ver TODO(api). De momento usa el "
            "proveedor claude_cli (Claude Max)."
        )

    def clasificar_lote(
        self, empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion]
    ) -> list[SugerenciaBloque]:
        # TODO(api): una sola llamada con build_mensajes_lote/parse_respuesta_lote.
        raise NotImplementedError(
            "Adaptador Anthropic pendiente — ver TODO(api). De momento usa el "
            "proveedor claude_cli (Claude Max)."
        )

    def completar(self, system: str, user: str, timeout_s: int | None = None,
                  modelo: str | None = None) -> str:
        # TODO(api): client.messages.create(...) con prompt caching.
        raise NotImplementedError(
            "Adaptador Anthropic pendiente — ver TODO(api). De momento usa el "
            "proveedor claude_cli (Claude Max)."
        )

    def investigar(self, system: str, user: str, timeout_s: int | None = None,  # noqa: ARG002
                   modelo: str | None = None) -> str:
        # TODO(api): client.messages.create(..., tools=[{"type": "web_search_..."}]).
        raise NotImplementedError(
            "Búsqueda web vía API pendiente — usará el server-tool `web_search`. "
            "De momento usa claude_cli (Claude Max) con WebSearch."
        )
