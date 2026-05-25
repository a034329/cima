"""Adaptador dev: clasifica vía el CLI de Claude Code en modo headless.

Tira de la suscripción Claude Max ya autenticada en el entorno (OAuth en
`~/.claude`), sin consumir API key. Aísla el contexto:
  - `--system-prompt`  reemplaza el system por defecto por el nuestro.
  - `--tools ""`        sin herramientas → no escribe FS ni navega (sandbox).
  - cwd = dir temporal  → no auto-descubre ningún CLAUDE.md del repo.
  - `--no-session-persistence` → no deja rastro de sesión.

NO usar `--bare`: fuerza ANTHROPIC_API_KEY e ignora el OAuth de Max.
"""
from __future__ import annotations

import json
import subprocess
import tempfile

from app.adapters.ia.base import (
    BloqueOpcion,
    ClasificadorError,
    ContextoEmpresa,
    SugerenciaBloque,
)
from app.adapters.ia.prompt import (
    build_mensajes,
    build_mensajes_lote,
    parse_respuesta,
    parse_respuesta_lote,
)
from app.config import settings

_PROVEEDOR = "claude_cli"


class ClaudeCliClasificador:
    """Implementa el puerto ClasificadorIA mediante `claude -p`."""

    def __init__(self, cli_path: str | None = None, modelo: str | None = None,
                 timeout_s: int | None = None) -> None:
        self.cli_path = cli_path or settings.ia_cli_path
        self.modelo = modelo or settings.anthropic_default_model
        self.timeout_s = timeout_s or settings.ia_timeout_s

    def clasificar(
        self, ctx: ContextoEmpresa, catalogo: list[BloqueOpcion],
        ejemplos: list[dict] | None = None,
    ) -> SugerenciaBloque:
        system, user = build_mensajes(ctx, catalogo, ejemplos)
        return parse_respuesta(self._run(system, user), catalogo, self.modelo, _PROVEEDOR)

    def clasificar_lote(
        self, empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion]
    ) -> list[SugerenciaBloque]:
        """Trocea el lote en grupos de `ia_lote_chunk` (una llamada por grupo) para
        acotar el tiempo/output. Usa `ia_lote_model` (opus) — ver experimento."""
        modelo = settings.ia_lote_model or self.modelo
        out: list[SugerenciaBloque] = []
        chunk = max(1, settings.ia_lote_chunk)
        for i in range(0, len(empresas), chunk):
            grupo = empresas[i:i + chunk]
            system, user = build_mensajes_lote(grupo, catalogo)
            # Resiliencia: si un grupo devuelve JSON inválido o falla, se omite
            # (esas empresas quedan sin sugerencia) en vez de tumbar todo el lote.
            try:
                out.extend(parse_respuesta_lote(
                    self._run(system, user, modelo), grupo, catalogo, modelo, _PROVEEDOR
                ))
            except ClasificadorError:
                continue
        return out

    def _run(self, system: str, user: str, modelo: str | None = None) -> str:
        """Una llamada headless a `claude -p`; devuelve el texto del resultado.
        Arranque ligero: sin MCP (`--strict-mcp-config` sin configs) y solo
        settings de usuario, para minimizar latencia de cold start."""
        cmd = [
            self.cli_path, "-p",
            "--system-prompt", system,
            "--output-format", "json",
            "--model", modelo or self.modelo,
            "--tools", "",                  # sin herramientas: sandbox por diseño
            "--strict-mcp-config",          # ignora servidores MCP del entorno
            "--setting-sources", "user",    # ignora hooks/settings de proyecto/local
            "--permission-mode", "default",
            "--no-session-persistence",
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="cima-ia-") as cwd:
                proc = subprocess.run(
                    cmd, input=user, capture_output=True, text=True,
                    timeout=self.timeout_s, cwd=cwd,
                )
        except FileNotFoundError as e:
            raise ClasificadorError(
                f"CLI de Claude no encontrado en {self.cli_path!r}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise ClasificadorError(f"Timeout del CLI tras {self.timeout_s}s") from e

        if proc.returncode != 0:
            raise ClasificadorError(
                f"CLI devolvió código {proc.returncode}: {proc.stderr.strip()[:300]}"
            )

        # `--output-format json` envuelve el resultado: {type, is_error, result, ...}
        try:
            envoltura = json.loads(proc.stdout)
        except ValueError as e:
            raise ClasificadorError(
                f"Salida del CLI no es JSON: {proc.stdout[:200]!r}"
            ) from e
        if envoltura.get("is_error") or "result" not in envoltura:
            raise ClasificadorError(f"CLI reportó error: {str(envoltura)[:300]}")

        return envoltura["result"]
