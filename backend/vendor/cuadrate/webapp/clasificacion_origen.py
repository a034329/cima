"""
clasificacion_origen — Clasifica valores por origen fiscal a partir del ISIN.

Devuelve una de estas etiquetas, alineadas con la taxonomía que Hacienda usa
para distinguir tratamientos fiscales y casillas de Renta Web:

  · "España"   — emisor residente en territorio español (ISIN ES…)
  · "UE-EEE"   — emisor residente en otro estado de la UE-27 o del EEE
                 (Islandia, Liechtenstein, Noruega)
  · "Resto"    — todo lo demás (US, GB tras Brexit, CH, JP, HK, …)

El ISIN lleva el país de emisión en los dos primeros caracteres (ISO 3166-1
alpha-2). Para retail con DeGiro/IBKR/Trade Republic esto cubre el ~99 % de
casos. Las excepciones son ADRs: una empresa española puede emitir un
American Depositary Receipt con ISIN US…, pero fiscalmente sigue siendo
española. Para esos casos hay un fichero `overrides_origen.json` con las
correcciones manuales.
"""
from __future__ import annotations

import json
import os

# UE-27 + EEE (Islandia, Liechtenstein, Noruega). España va aparte porque es
# su propia categoría. Reino Unido salió del EEE en 2020 → no está aquí.
_EEA_PREFIXES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "SE",     # UE-27 (sin ES)
    "IS", "LI", "NO",                       # EEE no-UE
})

LABEL_ESPANA  = "España"
LABEL_UE_EEE  = "UE-EEE"
LABEL_RESTO   = "Resto"
LABEL_DESC    = "Sin ISIN"

_OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "overrides_origen.json")
_overrides_cache: dict[str, str] | None = None


def _load_overrides() -> dict[str, str]:
    """Carga el JSON de overrides (lazy + cacheado).

    Estructura del JSON:
      { "US05964H1059": "España",  // Banco Santander ADR
        ...
      }
    """
    global _overrides_cache
    if _overrides_cache is not None:
        return _overrides_cache
    if not os.path.exists(_OVERRIDES_PATH):
        _overrides_cache = {}
        return _overrides_cache
    try:
        with open(_OVERRIDES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _overrides_cache = {k.upper(): v for k, v in data.items()
                            if isinstance(k, str) and isinstance(v, str)}
    except (json.JSONDecodeError, OSError):
        _overrides_cache = {}
    return _overrides_cache


def clasificar_isin(isin: str | None) -> str:
    """Devuelve la etiqueta de origen para un ISIN.

    >>> clasificar_isin("ES0113900J37")  # Banco Santander
    'España'
    >>> clasificar_isin("DE0007164600")  # SAP
    'UE-EEE'
    >>> clasificar_isin("US0378331005")  # Apple
    'Resto'
    >>> clasificar_isin("US05964H1059")  # Santander ADR (override)
    'España'
    >>> clasificar_isin("")
    'Sin ISIN'
    """
    if not isin or len(isin) < 2:
        return LABEL_DESC
    isin_up = isin.strip().upper()
    overrides = _load_overrides()
    if isin_up in overrides:
        return overrides[isin_up]
    prefix = isin_up[:2]
    if prefix == "ES":
        return LABEL_ESPANA
    if prefix in _EEA_PREFIXES:
        return LABEL_UE_EEE
    return LABEL_RESTO


def reload_overrides() -> None:
    """Invalida el caché. Útil en tests o tras editar el JSON en caliente."""
    global _overrides_cache
    _overrides_cache = None
