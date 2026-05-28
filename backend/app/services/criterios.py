"""Criterios objetivos medibles por bloque (chequeo de encaje del candidato).

El texto humano de cada bloque vive en FICHAS[cat].criterios (prompt.py). Aquí
están solo los UMBRALES que SÍ podemos comprobar con los datos que tenemos
(yield, beta, ROE, crecimiento del BPA). Lo que no medimos — payout, cobertura
del dividendo, ROIC exacto, deuda, moat — NO se convierte en semáforo verde/rojo:
queda como juicio cualitativo. Sin falsa precisión.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.adapters.ia.base import ContextoEmpresa


@dataclass(frozen=True)
class Criterio:
    etiqueta: str
    campo: str                       # atributo de ContextoEmpresa a leer
    minimo: float | None = None
    maximo: float | None = None
    formato: str = "pct"             # "pct" (×100 + %) | "num"


# Solo categorías con métricas comprobables hoy. Las demás (indice, renta_fija,
# satelite, cripto, materias_primas, colchon) no tienen umbrales → solo el texto
# cualitativo de su ficha.
CRITERIOS_MEDIBLES: dict[str, list[Criterio]] = {
    "growth": [
        Criterio("Yield", "yield_pct", maximo=0.03),
        Criterio("Crecimiento BPA", "crecimiento_eps_pct", minimo=0.10),
        Criterio("ROE", "roe", minimo=0.15),
    ],
    "income": [
        Criterio("Yield", "yield_pct", minimo=0.015, maximo=0.05),
        Criterio("Crecimiento BPA (proxy de calidad)", "crecimiento_eps_pct", minimo=0.08),
    ],
    "defensivo": [
        Criterio("Yield", "yield_pct", minimo=0.03, maximo=0.06),
        Criterio("Beta", "beta", maximo=0.9, formato="num"),
    ],
    "aggressive": [
        Criterio("Yield", "yield_pct", minimo=0.06),
    ],
}


@dataclass
class CriterioCheck:
    etiqueta: str
    valor: float | None
    valor_txt: str
    objetivo_txt: str
    cumple: bool | None              # None = dato no disponible (no cuenta)


def _fmt(v: float | None, formato: str) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if formato == "pct" else f"{v:.2f}"


def _objetivo_txt(c: Criterio) -> str:
    if c.minimo is not None and c.maximo is not None:
        return f"{_fmt(c.minimo, c.formato)}–{_fmt(c.maximo, c.formato)}"
    if c.minimo is not None:
        return f"≥ {_fmt(c.minimo, c.formato)}"
    if c.maximo is not None:
        return f"≤ {_fmt(c.maximo, c.formato)}"
    return ""


def evaluar_criterios(ctx: ContextoEmpresa, categoria: str) -> list[CriterioCheck]:
    """Chequea las métricas de `ctx` contra los umbrales medibles de `categoria`.
    Un campo None → `cumple=None` (no se puntúa, no se finge precisión)."""
    checks: list[CriterioCheck] = []
    for c in CRITERIOS_MEDIBLES.get(categoria, []):
        v = getattr(ctx, c.campo, None)
        if v is None:
            cumple: bool | None = None
        else:
            cumple = (c.minimo is None or v >= c.minimo) and (c.maximo is None or v <= c.maximo)
        checks.append(CriterioCheck(
            etiqueta=c.etiqueta, valor=v, valor_txt=_fmt(v, c.formato),
            objetivo_txt=_objetivo_txt(c), cumple=cumple,
        ))
    return checks
