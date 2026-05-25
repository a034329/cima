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
