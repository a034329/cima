"""Valoración asistida: la IA propone escenarios (múltiplo + EPS 4Y) ANCLADOS en
datos reales (consenso de analistas, PER histórico) y el usuario los traslada a la
hoja de Estimaciones. La IA propone; el usuario edita y aplica. Soporta cualquier
TIPO_VAL (PER, P/FCF, P/BV, P/FRE): para no-PER la IA investiga el múltiplo sectorial.
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

_TIPO = "valoracion"
_DISCLAIMER = ("Escenarios de valoración generados por IA, orientativos y NO asesoramiento. "
               "Tú fijas los inputs finales; verifica contra el consenso y el histórico.")


@dataclass
class Escenario:
    nombre: str                  # conservador | base | optimista
    multiplo: float
    metrica_base_4y: float       # EPS proyectado a 4 años
    precio_objetivo: float
    cagr4_pct: float | None
    razon: str


@dataclass
class Valoracion:
    isin: str
    nombre: str
    tipo_val: str
    precio_actual: float | None
    anclas: dict = field(default_factory=dict)
    escenarios: list = field(default_factory=list)
    fecha: str = ""
    proveedor: str = ""
    disclaimer: str | None = None


def _f(v) -> float | None:  # type: ignore[no-untyped-def]
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _calc(db: Session, cartera_id: str, isin: str):  # type: ignore[no-untyped-def]
    from app.services.estimaciones import (
        calcular_estimaciones, calcular_estimaciones_seguimiento,
    )
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}
    calcs.update({c.isin: c for c in calcular_estimaciones_seguimiento(db, cartera_id)})
    return calcs.get(isin)


def _anclas(e) -> dict:  # type: ignore[no-untyped-def]
    return {
        "eps_actual": _f(e.eps_actual),
        "eps_consenso_4y": _f(getattr(e, "eps_consenso_4y", None)),
        "anio_consenso_4y": getattr(e, "anio_consenso_4y", None),
        "num_analistas_eps": getattr(e, "num_analistas_eps", None),
        "per_hist_medio": _f(getattr(e, "per_hist_medio", None)),
        "per_hist_mediano": _f(getattr(e, "per_hist_mediano", None)),
        "multiplo_actual": _f(e.multiplo_objetivo),
        "metrica_actual": _f(e.metrica_base_4y),
        "precio_obj_consenso": _f(getattr(e, "precio_obj_consenso", None)),
        "precio_actual": _f(e.precio_actual),
    }


def build_prompt(nombre: str, anclas: dict, tipo_val: str = "PER",
                 tesis: dict | None = None) -> tuple[str, str]:
    mult_label, met_label = models.etiquetas_tipo_val(tipo_val)
    es_per = (tipo_val or "PER") == "PER"
    anclaje = (
        "ANCLA el escenario BASE en el consenso de analistas y el múltiplo en el PER histórico de la "
        "empresa; no inventes sin base. En la razón, di cómo se compara tu EPS 4Y con el consenso "
        "(por encima/en línea/por debajo y por qué)."
        if es_per else
        f"Este negocio NO se valora por beneficios (PER): usa el múltiplo propio de su tipo "
        f"({mult_label}). INVESTIGA en la web el {mult_label} típico de comparables del sector y un "
        f"rango razonable, y PROYECTA el {met_label} a 4 años a partir de la trayectoria del negocio. "
        f"NO uses EPS ni PER. En la razón, justifica el múltiplo con comparables y el crecimiento "
        f"implícito de la métrica."
    )
    system = (
        f"Eres un analista que propone una valoración por {mult_label} en 3 escenarios (conservador, "
        f"base, optimista) para un inversor a largo plazo. Para cada escenario da un MÚLTIPLO "
        f"({mult_label} objetivo) y la métrica base proyectada a 4 años ({met_label}, campo "
        "metrica_4y).\n"
        "Los escenarios DEBEN derivarse de la TESIS del negocio, no de números al azar: el OPTIMISTA "
        "refleja que los drivers de crecimiento de la tesis se cumplen; el CONSERVADOR refleja que se "
        "materializan los riesgos; el BASE es el caso central. "
        + anclaje + " Puedes buscar en la web. No calcules el precio objetivo (lo hace el sistema).\n"
        "Responde EXCLUSIVAMENTE con JSON:\n"
        '{"escenarios": [{"nombre": "conservador|base|optimista", "multiplo": <num>, '
        '"metrica_4y": <num>, "razon": "<múltiplo justificado + crecimiento implícito, 1-2 frases>"}]}'
    )
    a = anclas
    bloques = [f"Empresa: {nombre}. Se valora por {mult_label} (métrica base: {met_label}). "
               "Anclas reales (de Cima):"]
    if es_per:
        bloques += [
            f"- EPS actual: {a.get('eps_actual')}",
            f"- EPS consenso 4Y: {a.get('eps_consenso_4y')} (año {a.get('anio_consenso_4y')}, "
            f"{a.get('num_analistas_eps')} analistas)",
            f"- PER histórico medio: {a.get('per_hist_medio')} · mediano: {a.get('per_hist_mediano')}",
            f"- Precio objetivo consenso: {a.get('precio_obj_consenso')}",
        ]
    bloques.append(f"- Múltiplo/métrica actuales en el modelo: {a.get('multiplo_actual')} / "
                   f"{a.get('metrica_actual')}")
    bloques.append(f"- Precio actual: {a.get('precio_actual')}")
    if tesis:
        bloques += [
            "\nTESIS de tu one-pager (deriva los escenarios de aquí):",
            f"- Tesis alcista: {tesis.get('tesis') or '—'}",
            f"- Riesgos: {tesis.get('riesgos') or '—'}",
            f"- Lectura de valoración: {tesis.get('valoracion') or '—'}",
        ]
    else:
        bloques.append("\nNo hay one-pager guardado: investiga brevemente los drivers de crecimiento y "
                       "los riesgos del negocio antes de proyectar.")
    bloques.append("\nPropón los 3 escenarios derivados de la tesis y anclados en los datos.")
    return system, "\n".join(bloques)


def _escenarios(data: dict, precio_actual: float | None) -> list[Escenario]:
    out: list[Escenario] = []
    items = data.get("escenarios") if isinstance(data, dict) else None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        mult = _f(it.get("multiplo"))
        met_raw = next((it[k] for k in ("metrica_4y", "eps_4y", "metrica_base_4y") if k in it), None)
        eps = _f(met_raw)
        if mult is None or eps is None:
            continue
        precio_obj = mult * eps                              # el sistema calcula N×O
        cagr = None
        if precio_actual and precio_actual > 0 and precio_obj > 0:
            cagr = (precio_obj / precio_actual) ** (1 / 4) - 1
        out.append(Escenario(
            nombre=str(it.get("nombre") or "—").strip().lower(),
            multiplo=mult, metrica_base_4y=eps, precio_objetivo=precio_obj,
            cagr4_pct=cagr, razon=str(it.get("razon") or "").strip(),
        ))
    return out


def parse(texto: str, precio_actual: float | None) -> list[Escenario]:
    s = (texto or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data: dict = {}
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            data = obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            data = {}
    return _escenarios(data, precio_actual)


def guardado(db: Session, cartera_id: str, isin: str) -> Valoracion | None:
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
        d["escenarios"] = [Escenario(**e) for e in d.get("escenarios", [])]
        return Valoracion(**d)
    except (ValueError, TypeError):
        return None


def _persistir(db: Session, cartera_id: str, v: Valoracion) -> None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == v.isin)
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    payload = json.dumps(asdict(v), ensure_ascii=False)
    if row is None:
        db.add(models.AnalisisGuardado(cartera_id=cartera_id, isin=v.isin,
                                       tipo=_TIPO, payload_json=payload))
    else:
        row.payload_json = payload
        row.created_at = datetime.now(UTC)
    db.commit()


def proponer(db: Session, cartera_id: str, isin: str) -> Valoracion:
    """Propone escenarios anclados según el tipo_val de la estimación (PER, P/FCF,
    P/BV o P/FRE) + persiste. Para no-PER, la IA investiga el múltiplo sectorial."""
    e = _calc(db, cartera_id, isin)
    if e is None:
        raise ValueError(f"Sin estimación para {isin}")
    tipo_val = e.tipo_val or "PER"
    precio_actual = _f(e.precio_actual)
    anclas = _anclas(e)
    # Liga los escenarios a la TESIS del one-pager si ya se generó (si no, la IA investiga).
    from app.services.one_pager import guardado as op_guardado
    op = op_guardado(db, cartera_id, isin)
    tesis = {"tesis": op.tesis, "riesgos": op.riesgos, "valoracion": op.valoracion} if op else None
    system, user = build_prompt(e.nombre, anclas, tipo_val, tesis)
    escenarios = parse(get_clasificador().investigar(system, user), precio_actual)
    creditos.registrar_uso_ia(db, cartera_id, "valoracion", 1)
    v = Valoracion(
        isin=isin, nombre=e.nombre, tipo_val=tipo_val, precio_actual=precio_actual,
        anclas=anclas, escenarios=escenarios, fecha=date.today().isoformat(),
        proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if getattr(settings.mode, "value", settings.mode) == "saas" else None),
    )
    _persistir(db, cartera_id, v)
    return v
