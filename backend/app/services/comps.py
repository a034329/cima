"""/comps (doctrina WG): comparables del sector antes de abrir/reforzar una posición.

La IA arma una tabla de pares del mismo sector/modelo con sus múltiplos clave +
una lectura de caro/barato vs pares. ANCLADO en los datos reales del objetivo
(de Cima) y con búsqueda web para los pares. NO es un hecho: el usuario verifica.
Cierra el ciclo hueco de bloque → elegir candidato → watchlist. Requiere IA con web.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.config import settings
from app.db import models
from app.services import creditos

_TIPO = "comps"
_MAX_PEERS = 8
_DISCLAIMER = ("Comparables generados por IA con búsqueda web; orientativos y NO asesoramiento. "
               "Los múltiplos de los pares son estimaciones — verifica antes de decidir.")


@dataclass
class Peer:
    nombre: str
    ticker: str
    per: float | None
    ev_ebitda: float | None
    p_fcf: float | None
    yield_pct: float | None          # fracción (0.03 = 3%)
    crecimiento_pct: float | None    # crecimiento de ingresos/BPA, fracción
    roic_pct: float | None           # fracción
    es_objetivo: bool = False


@dataclass
class Comps:
    isin: str
    nombre: str
    sector: str
    peers: list = field(default_factory=list)   # list[Peer]
    lectura: str = ""                            # caro/barato vs pares
    fuentes: list[str] = field(default_factory=list)
    fecha: str = ""
    proveedor: str = ""
    disclaimer: str | None = None


def _f(v) -> float | None:  # type: ignore[no-untyped-def]
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _datos(db: Session, cartera_id: str, isin: str):  # type: ignore[no-untyped-def]
    """(nombre, sector, bloque de anclas reales del objetivo) para el prompt."""
    from app.services.clasificador import construir_contexto
    ctx = construir_contexto(db, cartera_id, isin)
    anclas = {
        "per": _f(ctx.per), "yield_pct": _f(ctx.yield_pct),
        "crecimiento_eps_pct": _f(ctx.crecimiento_eps_pct), "roe": _f(ctx.roe),
        "cagr4_div_pct": _f(ctx.cagr4_div_pct),
    }
    return ctx.nombre, (ctx.sector or ""), ctx.tipo_activo, anclas


def build_prompt(nombre: str, sector: str, tipo_activo: str, anclas: dict) -> tuple[str, str]:
    system = (
        "Eres un analista que prepara una tabla de COMPARABLES (comps) del sector para decidir si una "
        "empresa está cara o barata frente a sus pares, para un inversor a largo plazo. BUSCA en la web "
        "4-6 competidores del MISMO sector y modelo de negocio y sus múltiplos actuales. INCLUYE también "
        "a la propia empresa objetivo en la lista (marcada con es_objetivo=true), usando como ancla los "
        "datos que te doy. Usa los múltiplos relevantes para el sector (deja null los que no apliquen). "
        "Da una LECTURA honesta: ¿el objetivo cotiza con prima o descuento vs la media de pares, y por "
        "qué (calidad, crecimiento, riesgo)? CITA fuentes (URLs).\n"
        "yield, crecimiento y roic en FRACCIÓN (0.03 = 3%). No inventes: si no encuentras un dato, null.\n"
        "Responde EXCLUSIVAMENTE con JSON, sin texto alrededor:\n"
        '{"sector": "<sector>", "peers": [{"nombre": "<empresa>", "ticker": "<TICK>", "per": <num|null>, '
        '"ev_ebitda": <num|null>, "p_fcf": <num|null>, "yield_pct": <num|null>, "crecimiento_pct": '
        '<num|null>, "roic_pct": <num|null>, "es_objetivo": <bool>}], '
        '"lectura": "<2-4 frases: prima/descuento vs pares y por qué>", "fuentes": ["<url>"]}'
    )
    user = (
        f"Empresa objetivo: {nombre} · sector declarado: {sector or '—'} · tipo: {tipo_activo}\n"
        f"Anclas reales del objetivo (de Cima): PER {anclas.get('per')} · yield {anclas.get('yield_pct')} "
        f"· crecimiento BPA {anclas.get('crecimiento_eps_pct')} · ROE {anclas.get('roe')} "
        f"· CAGR4+Div {anclas.get('cagr4_div_pct')}\n"
        "Encuentra sus comparables y arma la tabla."
    )
    return system, user


def parse(texto: str, nombre: str, isin: str) -> Comps:
    s = (texto or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data: dict = {}
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            data = obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            data = {}

    peers: list[Peer] = []
    for p in (data.get("peers") or [])[:_MAX_PEERS]:
        if not isinstance(p, dict):
            continue
        nom = str(p.get("nombre") or "").strip()
        if not nom:
            continue
        peers.append(Peer(
            nombre=nom, ticker=str(p.get("ticker") or "").strip(),
            per=_f(p.get("per")), ev_ebitda=_f(p.get("ev_ebitda")), p_fcf=_f(p.get("p_fcf")),
            yield_pct=_f(p.get("yield_pct")), crecimiento_pct=_f(p.get("crecimiento_pct")),
            roic_pct=_f(p.get("roic_pct")), es_objetivo=bool(p.get("es_objetivo")),
        ))
    fus = data.get("fuentes")
    fuentes = [str(u) for u in fus if u] if isinstance(fus, list) else []
    return Comps(
        isin=isin, nombre=nombre, sector=str(data.get("sector") or "").strip(),
        peers=peers, lectura=str(data.get("lectura") or "").strip(), fuentes=fuentes,
    )


def guardado(db: Session, cartera_id: str, isin: str) -> Comps | None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == isin)
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    if row is None:
        return None
    try:
        d = json.loads(row.payload_json)
        d["peers"] = [Peer(**p) for p in d.get("peers", [])]
        return Comps(**d)
    except (ValueError, TypeError):
        return None


def _persistir(db: Session, cartera_id: str, c: Comps) -> None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == c.isin)
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    payload = json.dumps(asdict(c), ensure_ascii=False)
    if row is None:
        db.add(models.AnalisisGuardado(cartera_id=cartera_id, isin=c.isin,
                                       tipo=_TIPO, payload_json=payload))
    else:
        row.payload_json = payload
        row.created_at = datetime.now(UTC)
    db.commit()


def generar(db: Session, cartera_id: str, isin: str) -> Comps:
    """Genera (IA + web) y PERSISTE. El usuario lo regenera explícitamente."""
    nombre, sector, tipo_activo, anclas = _datos(db, cartera_id, isin)
    system, user = build_prompt(nombre, sector, tipo_activo, anclas)
    texto = get_clasificador().investigar(system, user)
    c = parse(texto, nombre, isin)
    creditos.registrar_uso_ia(db, cartera_id, "comps", 1)
    c.fecha = date.today().isoformat()
    c.proveedor = settings.ia_provider
    if getattr(settings.mode, "value", settings.mode) == "saas":
        c.disclaimer = _DISCLAIMER
    _persistir(db, cartera_id, c)
    return c
