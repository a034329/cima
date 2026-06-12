"""Score de salud del dividendo (V6, mejoras 2026-06).

Implementa el check "cobertura del dividendo" del protocolo de rotaciones de
WG: un yield alto solo es seguro si el free cash flow lo cubre con holgura y
el payout sobre beneficios no está al límite. Datos de los fundamentales
cacheados del feed (los calienta el prefill) — cache-only, no bloquea lecturas.

Niveles:
  ALTA   — cobertura FCF ≥ 1,5× y payout < 70%
  MEDIA  — cubre pero sin holgura (1,1-1,5×) o payout 70-90%
  RIESGO — cobertura < 1,1× o payout > 90% o negativo (pierde dinero y paga)
  SIN_DATOS — el feed no da FCF/payout (no inventar seguridad)
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session


@dataclass
class SaludDividendo:
    isin: str
    nivel: str                       # ALTA | MEDIA | RIESGO | SIN_DATOS
    motivo: str
    fcf_cobertura: float | None      # FCF / dividendo total (veces)
    payout: float | None             # dividendo / beneficios (fracción)


def _score(payout: float | None, cobertura: float | None) -> tuple[str, str]:
    if payout is None and cobertura is None:
        return "SIN_DATOS", "el feed no da FCF ni payout"
    motivos: list[str] = []
    riesgo = False
    medio = False
    if cobertura is not None:
        if cobertura < 1.1:
            riesgo = True
            motivos.append(f"el FCF solo cubre {cobertura:.2f}× el dividendo")
        elif cobertura < 1.5:
            medio = True
            motivos.append(f"cobertura FCF ajustada ({cobertura:.2f}×)")
        else:
            motivos.append(f"FCF cubre {cobertura:.1f}× el dividendo")
    if payout is not None:
        if payout > 0.9 or payout < 0:
            riesgo = True
            motivos.append(
                "paga dividendo perdiendo dinero" if payout < 0
                else f"payout {payout * 100:.0f}% sobre beneficios")
        elif payout > 0.7:
            medio = True
            motivos.append(f"payout {payout * 100:.0f}%")
        else:
            motivos.append(f"payout {payout * 100:.0f}%")
    nivel = "RIESGO" if riesgo else ("MEDIA" if medio else "ALTA")
    return nivel, "; ".join(motivos)


def evaluar(db: Session, cartera_id: str) -> dict[str, SaludDividendo]:
    """{isin: score} de las posiciones CON dividendo (las que no pagan no
    tienen nada que evaluar). Cache-only: usa los fundamentales del prefill."""
    from app.services.precios import fundamentales_por_isin

    out: dict[str, SaludDividendo] = {}
    for isin, f in fundamentales_por_isin(db, cartera_id).items():
        if not f or not f.get("dividend"):
            continue
        payout = f.get("payout")
        cobertura = f.get("fcf_cobertura_div")
        nivel, motivo = _score(payout, cobertura)
        out[isin] = SaludDividendo(
            isin=isin, nivel=nivel, motivo=motivo,
            fcf_cobertura=cobertura, payout=payout,
        )
    return out
