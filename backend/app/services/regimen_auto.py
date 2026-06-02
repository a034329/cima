"""Auto-clasificación del régimen macro (doctrina WG, 4 indicadores).

Híbrido: para lo cuantificable (SP500/VIX/petróleo/curva) aplica los umbrales
de la tabla WG directamente; para los matices cualitativos (Fed dovish/hawkish,
tensión geopolítica, ciclo económico real con paro/PIB/probabilidad recesión)
la IA con búsqueda web enriquece la clasificación y aporta fuentes. La
propuesta NO sobrescribe el régimen vigente hasta que el usuario la firme.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.adapters.ia.base import ClasificadorIA
from app.db import models
from app.services import creditos
from app.services import regimen as svc_regimen
from app.services.precios import datos_macro_objetivos


# Umbrales doctrina WG aplicados directamente sobre los datos objetivos.
_BRENT_VERDE = 80.0     # < 80 USD → estabilidad
_BRENT_ROJO = 100.0     # > 100 USD → conflicto activo afectando suministros
_VIX_VERDE = 18.0       # < 18 → mercado tranquilo
_VIX_ROJO = 28.0        # > 28 → rotación defensiva / pánico
_DD_VERDE = -0.05       # caída < 5% desde máximos → VERDE
_DD_ROJO = -0.15        # caída > 15% → ROJA


@dataclass
class IndicadorPropuesta:
    """Propuesta de señal para un indicador. La IA la rellena con razón breve
    (1-2 frases) y fuentes (URLs/medio+fecha). `datos` lleva el snapshot
    numérico que pesó en la decisión (cuando aplica)."""
    senal: str                                 # VERDE | AMARILLA | ROJA
    razon: str
    fuentes: list[str] = field(default_factory=list)
    datos: dict[str, Any] = field(default_factory=dict)


@dataclass
class Propuesta:
    indicadores: dict[str, IndicadorPropuesta]     # ciclo, inflacion, geopolitica, mercado
    regimen: str                                   # VERDE | AMARILLO | ROJO (derivado)
    datos_objetivos: dict[str, Any]                # snapshot del feed yfinance
    proveedor: str                                 # claude_cli | anthropic | mock
    modelo: str
    created_at: str                                # ISO datetime


# ─── Clasificación numérica de respaldo ─────────────────────────────────────

def _peor_color(*senales: str | None) -> str | None:
    """Devuelve el más cauto de un conjunto de señales (ignora None).
    ROJA > AMARILLA > VERDE."""
    orden = {"VERDE": 0, "AMARILLA": 1, "ROJA": 2}
    activas = [s for s in senales if s in orden]
    return max(activas, key=lambda s: orden[s]) if activas else None


def _clasifica_mercado(d: dict | None) -> IndicadorPropuesta | None:
    """Mercado/Sentimiento: 100% cuantificable (SP500 drawdown + VIX). El más
    objetivo de los 4: la tabla WG mira las dos sub-señales y el peor color
    manda (VIX >28 ya es ROJA aunque el drawdown sea pequeño)."""
    if not d:
        return None
    dd = d.get("sp_drawdown")
    vix = d.get("vix")
    if dd is None and vix is None:
        return None
    sen_dd = None
    if dd is not None:
        sen_dd = "ROJA" if dd <= _DD_ROJO else "VERDE" if dd >= _DD_VERDE else "AMARILLA"
    sen_vix = None
    if vix is not None:
        sen_vix = "ROJA" if vix > _VIX_ROJO else "VERDE" if vix < _VIX_VERDE else "AMARILLA"
    senal = _peor_color(sen_dd, sen_vix) or "AMARILLA"
    partes = []
    if dd is not None:
        partes.append(f"S&P {dd*100:.1f}% desde máximos")
    if vix is not None:
        partes.append(f"VIX {vix:.0f}")
    return IndicadorPropuesta(senal=senal, razon=", ".join(partes) + ".",
                              datos={"sp_drawdown": dd, "vix": vix})


def _clasifica_geopolitica(d: dict | None) -> IndicadorPropuesta | None:
    """Geopolítica/M. primas: ancla en el precio del petróleo (Brent). La tabla
    WG mapea Brent <80=VERDE, 80-100=AMARILLA, >100=ROJA. La IA añadirá si hay
    conflicto activo afectando suministros (puede agravar la señal)."""
    if not d or d.get("brent_usd") is None:
        return None
    brent = float(d["brent_usd"])
    if brent < _BRENT_VERDE:
        senal, razon = "VERDE", f"Brent {brent:.0f} USD (<80, estabilidad)"
    elif brent > _BRENT_ROJO:
        senal, razon = "ROJA", f"Brent {brent:.0f} USD (>100, conflicto afectando suministros)"
    else:
        senal, razon = "AMARILLA", f"Brent {brent:.0f} USD (80-100, tensión localizada)"
    return IndicadorPropuesta(senal=senal, razon=razon + ".",
                              datos={"brent_usd": brent, "wti_usd": d.get("wti_usd")})


def _clasifica_ciclo_proxy(d: dict | None) -> IndicadorPropuesta | None:
    """Ciclo económico: SIN paro/PIB en yfinance, usamos la curva 10y-3m como
    proxy de probabilidad de recesión (inversión <0 = antesala histórica). La
    IA la complementa con paro/PIB recientes (web). Es lo más débil de los 4
    auto-clasificables, así que la IA tendrá margen para ajustar."""
    if not d or d.get("yield_curve_spread_pp") is None:
        return None
    spread = float(d["yield_curve_spread_pp"])
    if spread < -0.25:
        senal, razon = "ROJA", f"Curva 10y-3m invertida ({spread:+.2f} pp): señal histórica de recesión"
    elif spread < 0.5:
        senal, razon = "AMARILLA", f"Curva 10y-3m {spread:+.2f} pp (aplanada, sin holgura)"
    else:
        senal, razon = "VERDE", f"Curva 10y-3m {spread:+.2f} pp (positiva, sin señal recesiva)"
    return IndicadorPropuesta(senal=senal, razon=razon + ".",
                              datos={"yield_curve_spread_pp": spread})


def _clasificacion_numerica(datos: dict | None) -> dict[str, IndicadorPropuesta]:
    """Pre-clasificación derivada SOLO de los números. La IA recibe esto en el
    prompt y puede confirmar o matizar; el indicador 'inflacion' queda en
    blanco para que la IA lo investigue (Fed Funds + CPI YoY son juicio web)."""
    out: dict[str, IndicadorPropuesta] = {}
    for k, fn in (
        ("mercado", _clasifica_mercado),
        ("geopolitica", _clasifica_geopolitica),
        ("ciclo", _clasifica_ciclo_proxy),
    ):
        r = fn(datos)
        if r is not None:
            out[k] = r
    return out


# ─── Llamada a la IA para enriquecer y resolver lo cualitativo ───────────────

_SYSTEM_PROMPT = """\
Eres un asistente de análisis macro. Clasificas el régimen macro actual según la doctrina
de Wealth Guardian (4 indicadores en semáforo VERDE/AMARILLA/ROJA).

TABLA DE UMBRALES (no inventar otros criterios):
- Ciclo económico: VERDE = PIB >2% y empleo sólido · AMARILLA = PIB 1-2% o empleo moderándose ·
  ROJA = PIB <1% o recesión, paro subiendo.
- Inflación / Tipos: VERDE = inflación cayendo, Fed dovish · AMARILLA = inflación estable,
  Fed en pausa · ROJA = inflación subiendo, Fed hawkish o sin margen.
- Geopolítica / M. primas: VERDE = estabilidad, petróleo <80 USD · AMARILLA = tensión localizada,
  petróleo 80-100 USD · ROJA = conflicto activo afectando suministros, petróleo >100 USD.
- Mercado / Sentimiento: VERDE = tendencia alcista, VIX <18 · AMARILLA = corrección <15%, VIX 18-28 ·
  ROJA = caída >15% o VIX >28, rotación defensiva.

REGLAS:
1. Para cada indicador, devuelve UNA señal con razón breve (1-2 frases, datos+fecha siempre que
   sea posible) y 1-3 fuentes (URLs o medio+fecha).
2. Recibes una pre-clasificación numérica para mercado/geopolítica/ciclo derivada del feed
   actual. Confirma o matiza, pero NO contradigas un número objetivo sin justificación explícita.
3. Para Inflación/Tipos no hay pre-clasificación: investiga Fed Funds rate actual, CPI/PCE YoY
   reciente, último statement de la Fed (dovish/hawkish), y clasifica según la tabla.
4. Geopolítica: agravar a ROJA si hay conflicto activo afectando suministros aunque el Brent esté
   en rango AMARILLA. Aligerar nunca por debajo del color del Brent.
5. NO recomiendes operaciones. Solo clasificas el régimen.

Devuelve SOLO JSON válido con esta estructura exacta (sin texto antes/después, sin markdown):
{
  "ciclo": {"senal": "VERDE|AMARILLA|ROJA", "razon": "…", "fuentes": ["…"]},
  "inflacion": {"senal": "…", "razon": "…", "fuentes": ["…"]},
  "geopolitica": {"senal": "…", "razon": "…", "fuentes": ["…"]},
  "mercado": {"senal": "…", "razon": "…", "fuentes": ["…"]}
}
"""


def _user_prompt(datos: dict | None, pre: dict[str, IndicadorPropuesta]) -> str:
    lineas = ["Datos macro objetivos del feed (fecha hoy):"]
    if datos:
        if datos.get("sp_drawdown") is not None:
            lineas.append(f"- S&P 500 drawdown vs máximo 52s: {datos['sp_drawdown']*100:.1f}%")
        if datos.get("vix") is not None:
            lineas.append(f"- VIX: {datos['vix']:.1f}")
        if datos.get("brent_usd") is not None:
            lineas.append(f"- Brent: {datos['brent_usd']:.1f} USD")
        if datos.get("wti_usd") is not None:
            lineas.append(f"- WTI: {datos['wti_usd']:.1f} USD")
        if datos.get("yield_curve_spread_pp") is not None:
            lineas.append(f"- Curva 10y-3m: {datos['yield_curve_spread_pp']:+.2f} pp")
    else:
        lineas.append("- (feed no disponible — clasifica los 4 con búsqueda web)")
    if pre:
        lineas.append("")
        lineas.append("Pre-clasificación numérica (úsala como ancla):")
        for k, p in pre.items():
            lineas.append(f"- {k}: {p.senal} — {p.razon}")
    lineas.append("")
    lineas.append("Devuelve el JSON con los 4 indicadores.")
    return "\n".join(lineas)


def _parse_respuesta(raw: str) -> dict[str, IndicadorPropuesta]:
    """Tolerante a markdown/preámbulos: busca el primer bloque JSON."""
    txt = raw.strip()
    # Elimina cercas markdown si la IA las añade pese a la instrucción.
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"):
            txt = txt[4:]
    # Localiza el primer { y el último } para aislar el objeto.
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        raise ValueError(f"Respuesta IA sin JSON parseable: {raw[:200]!r}")
    obj = json.loads(txt[a:b + 1])

    out: dict[str, IndicadorPropuesta] = {}
    for k in svc_regimen.INDICADORES:
        v = obj.get(k) or {}
        senal = str(v.get("senal", "")).upper().strip()
        if senal not in svc_regimen.SENALES:
            raise ValueError(f"Señal inválida para {k}: {senal!r}")
        out[k] = IndicadorPropuesta(
            senal=senal,
            razon=str(v.get("razon", "")).strip(),
            fuentes=[str(f) for f in (v.get("fuentes") or []) if f],
        )
    return out


# ─── Orquestación: proponer + persistir + firmar ────────────────────────────

def proponer(db: Session, cartera_id: str, isin: str = "",
             ia: ClasificadorIA | None = None) -> Propuesta:
    """Genera una propuesta nueva y la persiste como `PropuestaRegimen`. NO toca
    `Cartera.regimen_macro_json` (esto solo cambia al firmar). Sincrónico: la
    llamada IA puede tardar minutos por la búsqueda web — invócalo desde un
    job en segundo plano (`services/jobs.py`). `isin` se ignora (nivel cartera);
    la firma coincide con el framework de jobs."""
    cliente = ia or get_clasificador()
    datos = datos_macro_objetivos()
    pre = _clasificacion_numerica(datos)
    raw = cliente.investigar(_SYSTEM_PROMPT, _user_prompt(datos, pre))
    creditos.registrar_uso_ia(db, cartera_id, "regimen_auto", 1)
    indicadores = _parse_respuesta(raw)
    # Funde el snapshot numérico de la pre-clasificación dentro de cada indicador
    # para no perderlo en el payload guardado.
    for k, p in pre.items():
        if k in indicadores:
            indicadores[k].datos = p.datos
    senales = {k: ip.senal for k, ip in indicadores.items()}
    propuesta = Propuesta(
        indicadores=indicadores,
        regimen=svc_regimen.derivar_regimen(senales),
        datos_objetivos=datos or {},
        proveedor=getattr(cliente, "proveedor", "?"),
        modelo=getattr(cliente, "modelo", "?"),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    _persistir(db, cartera_id, propuesta)
    return propuesta


def _persistir(db: Session, cartera_id: str, p: Propuesta) -> None:
    fila = db.execute(
        select(models.PropuestaRegimen).where(models.PropuestaRegimen.cartera_id == cartera_id)
    ).scalars().first()
    payload = json.dumps({
        "indicadores": {k: asdict(v) for k, v in p.indicadores.items()},
        "regimen": p.regimen,
        "datos_objetivos": p.datos_objetivos,
        "proveedor": p.proveedor,
        "modelo": p.modelo,
        "created_at": p.created_at,
    }, ensure_ascii=False)
    if fila is None:
        db.add(models.PropuestaRegimen(cartera_id=cartera_id, payload_json=payload))
    else:
        fila.payload_json = payload
        fila.created_at = datetime.now(UTC)
    db.commit()


def cargar_propuesta(db: Session, cartera_id: str) -> Propuesta | None:
    fila = db.execute(
        select(models.PropuestaRegimen).where(models.PropuestaRegimen.cartera_id == cartera_id)
    ).scalars().first()
    if fila is None:
        return None
    try:
        obj = json.loads(fila.payload_json)
    except (ValueError, TypeError):
        return None
    inds = {
        k: IndicadorPropuesta(
            senal=v.get("senal", "AMARILLA"),
            razon=v.get("razon", ""),
            fuentes=list(v.get("fuentes") or []),
            datos=dict(v.get("datos") or {}),
        )
        for k, v in (obj.get("indicadores") or {}).items()
    }
    return Propuesta(
        indicadores=inds,
        regimen=obj.get("regimen") or "AMARILLO",
        datos_objetivos=obj.get("datos_objetivos") or {},
        proveedor=obj.get("proveedor", "?"),
        modelo=obj.get("modelo", "?"),
        created_at=obj.get("created_at", ""),
    )


def firmar(db: Session, cartera_id: str) -> svc_regimen.RegimenEstado:
    """Aplica la propuesta vigente al régimen de la cartera. Falla si no hay
    propuesta — la UI debe haber pedido primero `auto-clasificar`."""
    p = cargar_propuesta(db, cartera_id)
    if p is None:
        raise ValueError("No hay propuesta de régimen para firmar. Pide primero auto-clasificar.")
    senales = {k: ip.senal for k, ip in p.indicadores.items()}
    return svc_regimen.guardar_regimen(db, cartera_id, senales)


def descartar_propuesta(db: Session, cartera_id: str) -> None:
    fila = db.execute(
        select(models.PropuestaRegimen).where(models.PropuestaRegimen.cartera_id == cartera_id)
    ).scalars().first()
    if fila is not None:
        db.delete(fila)
        db.commit()
