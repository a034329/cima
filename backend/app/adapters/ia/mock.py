"""Adaptador mock: regla determinista offline para tests y desarrollo sin Max."""
from __future__ import annotations

from app.adapters.ia.base import (
    BloqueOpcion,
    ContextoEmpresa,
    SugerenciaBloque,
)


def _categoria_heuristica(ctx: ContextoEmpresa) -> tuple[str, str]:
    """Regla simple sobre yield y crecimiento. Devuelve (categoria, razón)."""
    y = ctx.yield_pct or 0.0
    g = ctx.crecimiento_eps_pct or 0.0
    if y >= 0.06:
        return "aggressive", f"Yield alto ({y * 100:.1f}%) → rentas/acelerador."
    if y >= 0.03:
        return "income", f"Yield medio ({y * 100:.1f}%) con dividendo establecido."
    if g >= 0.10:
        return "growth", f"Crecimiento del BPA elevado ({g * 100:.1f}%) y yield bajo."
    return "defensivo", "Yield y crecimiento moderados → perfil estable."


class MockClasificador:
    """Implementa el puerto ClasificadorIA sin red."""

    def clasificar(
        self, ctx: ContextoEmpresa, catalogo: list[BloqueOpcion],
        ejemplos: list[dict] | None = None,
    ) -> SugerenciaBloque:
        cat, razon = _categoria_heuristica(ctx)
        bloque_id = next((b.id for b in catalogo if b.categoria_base == cat), None)
        return SugerenciaBloque(
            categoria_base=cat,
            bloque_id=bloque_id,
            razonamiento=razon,
            confianza=0.6,
            modelo="mock",
            proveedor="mock",
            isin=ctx.isin,
        )

    def clasificar_lote(
        self, empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion]
    ) -> list[SugerenciaBloque]:
        return [self.clasificar(e, catalogo) for e in empresas]

    def completar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        """Propuesta de onboarding canónica (offline): reparto de acumulación."""
        return (
            '{"bloques": ['
            '{"categoria_base": "growth", "peso_objetivo": 0.40, "razon": "Núcleo de '
            'crecimiento para componer hasta la IF."},'
            '{"categoria_base": "income", "peso_objetivo": 0.20, "razon": "Dividendos '
            'crecientes que suben cada año."},'
            '{"categoria_base": "defensivo", "peso_objetivo": 0.20, "razon": "Estabilidad '
            'y baja volatilidad."},'
            '{"categoria_base": "aggressive", "peso_objetivo": 0.20, "razon": "Rentas '
            'altas para flujo de caja."}'
            '], "resumen": "Cartera de acumulación equilibrada hacia el crecimiento."}'
        )

    def investigar(self, system: str, user: str) -> str:
        """PASO 0 canónico (offline): contexto + clasificación coyuntural."""
        return (
            '{"resumen": "Sin noticias estructurales recientes; ruido de mercado de corto plazo.",'
            ' "clasificacion": "COYUNTURAL",'
            ' "preguntas": ['
            '{"pregunta": "¿Sigue generando caja?", "respuesta": "Sí", "senal": "coyuntural"},'
            '{"pregunta": "¿Afecta a toda la industria?", "respuesta": "Headwind sectorial", "senal": "coyuntural"},'
            '{"pregunta": "¿Horizonte temporal claro?", "respuesta": "Evento puntual", "senal": "coyuntural"},'
            '{"pregunta": "¿Management creíble?", "respuesta": "Sí", "senal": "coyuntural"},'
            '{"pregunta": "¿Mismo negocio en 3-4 años?", "respuesta": "Sí", "senal": "coyuntural"}'
            '], "riesgo_principal": "Compresión de márgenes temporal.",'
            ' "fuentes": ["https://example.com/noticia"]}'
        )
