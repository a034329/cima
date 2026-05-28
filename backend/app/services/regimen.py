"""Régimen macro: el modelo da el destino, el macro da el RITMO (doctrina WG).

4 indicadores (semáforo VERDE/AMARILLA/ROJA) → régimen VERDE/AMARILLO/ROJO por
mayoría → tamaño de tramo + espaciado para el DCA. Los indicadores los fija el
usuario (no auto-fetch): es su flujo en Wealth Guardian, fiable y sin red.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models

INDICADORES = ("ciclo", "inflacion", "geopolitica", "mercado")
SENALES = ("VERDE", "AMARILLA", "ROJA")
_DEFECTO = "AMARILLA"

# Régimen → (tramo_min €, tramo_max €, espaciado). Tabla de la doctrina WG.
CALIBRACION: dict[str, tuple[int, int, str]] = {
    "VERDE":    (1000, 2000, "2-3 semanas"),
    "AMARILLO": (500, 1000, "3-4 semanas"),
    "ROJO":     (300, 500, "4-6 semanas"),
}

# Señal de cada indicador → régimen del mismo color (para el recuento y el empate).
_SENAL_A_REGIMEN = {"VERDE": "VERDE", "AMARILLA": "AMARILLO", "ROJA": "ROJO"}
_CAUTELA = {"VERDE": 0, "AMARILLO": 1, "ROJO": 2}   # mayor = más cauto

# Regla del −14%: tramo ESCALADO por régimen cuando hay corrección sistémica y el
# ciclo NO está en recesión. Tabla de la doctrina WG.
CALIBRACION_14: dict[str, tuple[int, int]] = {
    "VERDE": (1500, 2500), "AMARILLO": (1000, 1500), "ROJO": (500, 1000),
}
_DD_MIN, _DD_MAX = -0.20, -0.10     # corrección sistémica: caída entre 10% y 20%
_VIX_PANICO = 35.0                  # > 35 → pánico, no escalar
_VIX_ESCALAR = 28.0                 # escalar solo con VIX < 28 (estabilización)


@dataclass
class RegimenEstado:
    indicadores: dict[str, str]      # {ciclo, inflacion, geopolitica, mercado: SENAL}
    regimen: str                     # VERDE | AMARILLO | ROJO
    tramo_min: int
    tramo_max: int
    espaciado: str
    actualizado: str | None          # ISO date o None si nunca se fijó


@dataclass
class VentanaCorreccion:
    sp_drawdown: float | None        # fracción negativa (−0,13 = −13%)
    vix: float | None
    activa: bool                     # ¿se permite escalar el tramo?
    escalado_min: int | None
    escalado_max: int | None
    nota: str                        # explicación (activa, en espera o bloqueada)


def evaluar_correccion(estado: RegimenEstado, mercado: dict | None) -> VentanaCorreccion:
    """Regla del −14%: ¿la caída del S&P es una corrección donde cargar (escalar el
    tramo) o el principio de un bear market (no tocar)? La clave es el CICLO
    económico. NO automatiza la clasificación COYUNTURAL por empresa (juicio del
    usuario): cuando la ventana está activa, OFRECE el tramo escalado con la salvedad."""
    if mercado is None:
        return VentanaCorreccion(None, None, False, None, None,
                                 "Datos de mercado no disponibles ahora mismo.")
    dd = mercado.get("sp_drawdown")
    vix = mercado.get("vix")
    ciclo = estado.indicadores.get("ciclo")

    if ciclo == "ROJA":
        return VentanaCorreccion(dd, vix, False, None, None,
            "Ciclo económico en ROJA → posible bear market (−34% a −43%). NO escalar: "
            "el −14% puede ser solo el principio.")
    if dd is None or dd > _DD_MAX:
        return VentanaCorreccion(dd, vix, False, None, None,
            "S&P sin corrección sistémica (caída < 10% desde máximos). Tramo normal.")
    if dd < _DD_MIN:
        return VentanaCorreccion(dd, vix, False, None, None,
            f"S&P {dd * 100:.0f}% (> 20%): zona de peligro. No escalar sin confirmar que no hay recesión.")
    if vix is not None and vix >= _VIX_PANICO:
        return VentanaCorreccion(dd, vix, False, None, None,
            f"VIX {vix:.0f} > 35: pánico activo. Espera estabilización (VIX < 28) antes de escalar.")
    if vix is not None and vix >= _VIX_ESCALAR:
        return VentanaCorreccion(dd, vix, False, None, None,
            f"VIX {vix:.0f}: aún alto. Espera a VIX < 28 para escalar.")
    emin, emax = CALIBRACION_14[estado.regimen]
    extra = "" if vix is not None else " (VIX no disponible)"
    return VentanaCorreccion(dd, vix, True, emin, emax,
        f"Ventana −14% activa (S&P {dd * 100:.0f}%{extra}). Puedes escalar a {emin}–{emax} € "
        "en nombres COYUNTURALES cuyo CAGR no haya empeorado — verifica tú esa clasificación.")


def derivar_regimen(indicadores: dict[str, str]) -> str:
    """Régimen = mayoría de los 4 indicadores. Empate → el más cauto
    (ROJO > AMARILLO > VERDE), porque ante duda macro se reduce exposición."""
    conteo: dict[str, int] = {}
    for ind in INDICADORES:
        reg = _SENAL_A_REGIMEN.get(indicadores.get(ind, _DEFECTO), "AMARILLO")
        conteo[reg] = conteo.get(reg, 0) + 1
    # max por (nº de votos, cautela) → desempata hacia el más cauto
    return max(conteo, key=lambda r: (conteo[r], _CAUTELA[r]))


def _normaliza(indicadores: dict[str, str] | None) -> dict[str, str]:
    ind = indicadores or {}
    return {k: (ind.get(k) if ind.get(k) in SENALES else _DEFECTO) for k in INDICADORES}


def _estado(indicadores: dict[str, str], actualizado: str | None) -> RegimenEstado:
    norm = _normaliza(indicadores)
    reg = derivar_regimen(norm)
    tmin, tmax, esp = CALIBRACION[reg]
    return RegimenEstado(norm, reg, tmin, tmax, esp, actualizado)


def estado_regimen(db: Session, cartera_id: str) -> RegimenEstado:
    """Régimen vigente de la cartera. Sin definir → todo AMARILLA (neutral)."""
    c = db.get(models.Cartera, cartera_id)
    data = {}
    if c is not None and c.regimen_macro_json:
        try:
            data = json.loads(c.regimen_macro_json)
        except (ValueError, TypeError):
            data = {}
    return _estado(data, data.get("actualizado") if isinstance(data, dict) else None)


def guardar_regimen(db: Session, cartera_id: str, indicadores: dict[str, str]) -> RegimenEstado:
    """Persiste los 4 indicadores (validados) + la fecha. Devuelve el estado nuevo."""
    norm = _normaliza(indicadores)
    payload = {**norm, "actualizado": date.today().isoformat()}
    c = db.get(models.Cartera, cartera_id)
    if c is not None:
        c.regimen_macro_json = json.dumps(payload, ensure_ascii=False)
        db.commit()
    return _estado(norm, payload["actualizado"])


def tramos_para(deficit_eur: Decimal | float | None, estado: RegimenEstado) -> tuple[int, int] | None:
    """Rango de nº de tramos para cubrir el déficit con el tamaño del régimen:
    (déficit/tramo_max .. déficit/tramo_min). None si no hay déficit positivo."""
    if deficit_eur is None:
        return None
    d = float(deficit_eur)
    if d <= 0:
        return None
    import math
    n_min = max(1, math.ceil(d / estado.tramo_max))
    n_max = max(n_min, math.ceil(d / estado.tramo_min))
    return n_min, n_max
