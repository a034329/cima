"""/one-pager (doctrina WG): estudio inicial de una empresa.

Síntesis en prosa que reúne lo que Cima ya sabe (estimaciones, fundamentales,
encaje de bloque, régimen) + contexto web reciente, y produce una tesis con
fuentes. NO es un hecho: es la lectura de la IA, el usuario verifica. Requiere
proveedor IA con búsqueda web (Max CLI en dev; API después).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.adapters.ia.prompt import FICHAS
from app.config import settings
from app.db import models
from app.services import creditos

_CLASES = ("COYUNTURAL", "GRIS", "ESTRUCTURAL", "")
_DISCLAIMER = ("One-pager generado por IA con búsqueda web; orientativo, NO asesoramiento. "
               "Verifica las fuentes y los datos antes de decidir.")


@dataclass
class OnePager:
    isin: str
    nombre: str
    que_hace: str = ""
    tesis: str = ""
    riesgos: str = ""
    valoracion: str = ""
    encaje: str = ""
    veredicto: str = ""
    clasificacion: str = ""              # COYUNTURAL | GRIS | ESTRUCTURAL | ""
    fuentes: list[str] = field(default_factory=list)
    fecha: str = ""
    proveedor: str = ""
    disclaimer: str | None = None


def _pct(v) -> str:  # type: ignore[no-untyped-def]
    return "—" if v is None else f"{float(v) * 100:.1f}%"


def _num(v) -> str:  # type: ignore[no-untyped-def]
    return "—" if v is None else f"{float(v):.2f}"


def _datos(db: Session, cartera_id: str, isin: str) -> tuple[str, str]:
    """Devuelve (nombre, bloque_quant_text) con el snapshot cuantitativo para el prompt."""
    from app.services.clasificador import construir_contexto
    from app.services.estimaciones import (
        calcular_estimaciones, calcular_estimaciones_seguimiento,
    )
    from app.services.regimen import estado_regimen

    ctx = construir_contexto(db, cartera_id, isin)
    calcs = {c.isin: c for c in calcular_estimaciones(db, cartera_id)}
    calcs.update({c.isin: c for c in calcular_estimaciones_seguimiento(db, cartera_id)})
    e = calcs.get(isin)

    pos = db.execute(select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
                     .where(models.Posicion.isin == isin)).scalars().first()
    seg = db.execute(select(models.Seguimiento).where(models.Seguimiento.cartera_id == cartera_id)
                     .where(models.Seguimiento.isin == isin)).scalars().first()
    bid = (pos.bloque_id if pos else None) or (seg.bloque_id if seg else None)
    bloque = db.get(models.Bloque, bid) if bid else None
    cat = bloque.categoria_base if bloque else None
    bloque_txt = (f"{bloque.nombre} ({cat}); criterios del bloque: {FICHAS[cat].criterios}"
                  if bloque and cat in FICHAS else "sin clasificar")

    reg = estado_regimen(db, cartera_id)
    mult_label, met_label = models.etiquetas_tipo_val(e.tipo_val if e else "PER")
    valora_txt = (
        f"- Valoración del modelo: por {mult_label} (múltiplo {_num(e.multiplo_objetivo)} × "
        f"{met_label} {_num(e.metrica_base_4y)})" if e else "- Valoración del modelo: —"
    )
    lineas = [
        f"- Tipo de activo: {ctx.tipo_activo or '—'} · sector: {ctx.sector or '—'}",
        f"- Precio actual: {_num(e.precio_actual) if e else '—'} {ctx.divisa or ''}"
        f" · precio objetivo (modelo): {_num(e.precio_objetivo) if e else '—'}",
        f"- CAGR4+Div (retorno total estimado): {_pct(ctx.cagr4_div_pct)}"
        f" · yield: {_pct(ctx.yield_pct)} · crecimiento BPA: {_pct(ctx.crecimiento_eps_pct)}",
        valora_txt,
        f"- PER trailing de mercado: {_num(ctx.per)} · beta: {_num(ctx.beta)} · ROE: {_pct(ctx.roe)}",
        f"- Bloque asignado: {bloque_txt}",
        f"- Régimen macro: {reg.regimen} (tramos {reg.tramo_min}-{reg.tramo_max} € cada {reg.espaciado})",
    ]
    return ctx.nombre, "\n".join(lineas)


def build_prompt(nombre: str, snapshot: str) -> tuple[str, str]:
    system = (
        "Eres un analista que redacta el ESTUDIO INICIAL (one-pager) de una empresa para un inversor "
        "particular orientado a la independencia financiera. Te doy un snapshot cuantitativo; BUSCA en "
        "la web contexto reciente (qué hace hoy, últimos resultados, noticias, competencia, riesgos) y "
        "redacta un one-pager honesto y conciso. Si hay un evento reciente, clasifícalo COYUNTURAL/"
        "GRIS/ESTRUCTURAL. CITA las fuentes (URLs).\n"
        "Responde EXCLUSIVAMENTE con JSON, sin texto alrededor:\n"
        '{"que_hace": "<1-2 frases: el negocio>", "tesis": "<tesis alcista, 2-4 frases>", '
        '"riesgos": "<tesis bajista/riesgos, 2-4 frases>", "valoracion": "<lectura de la valoración '
        'usando el MÚLTIPLO del modelo indicado en el snapshot (no asumas PER si el modelo usa otro), '
        'vs el precio objetivo, 1-3 frases>", "encaje": "<encaje en su bloque y en una '
        'estrategia de IF, 1-2 frases>", "veredicto": "<síntesis accionable, 1-2 frases>", '
        '"clasificacion": "COYUNTURAL|GRIS|ESTRUCTURAL|", "fuentes": ["<url>"]}'
    )
    user = f"Empresa: {nombre}\nSnapshot cuantitativo (datos de Cima):\n{snapshot}"
    return system, user


def parse(texto: str, nombre: str, isin: str) -> OnePager:
    s = (texto or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data: dict = {}
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            data = obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            data = {}
    clasif = str(data.get("clasificacion", "")).strip().upper()
    if clasif not in _CLASES:
        clasif = ""
    fus = data.get("fuentes")
    fuentes = [str(u) for u in fus if u] if isinstance(fus, list) else []

    def g(k: str) -> str:
        return str(data.get(k) or "").strip()

    return OnePager(
        isin=isin, nombre=nombre,
        que_hace=g("que_hace"), tesis=g("tesis"), riesgos=g("riesgos"),
        valoracion=g("valoracion"), encaje=g("encaje"),
        # fallback: si no hubo JSON, vuelca el texto crudo en veredicto para no perder el análisis.
        veredicto=g("veredicto") or (s[:600] if not data else ""),
        clasificacion=clasif, fuentes=fuentes,
    )


_TIPO = "one_pager"


def guardado(db: Session, cartera_id: str, isin: str) -> OnePager | None:
    """One-pager persistido (si existe) — sin llamar a la IA."""
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == isin)
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    if row is None:
        return None
    try:
        return OnePager(**json.loads(row.payload_json))
    except (ValueError, TypeError):
        return None


def _persistir(db: Session, cartera_id: str, op: OnePager) -> None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == op.isin)
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    payload = json.dumps(asdict(op), ensure_ascii=False)
    if row is None:
        db.add(models.AnalisisGuardado(cartera_id=cartera_id, isin=op.isin,
                                       tipo=_TIPO, payload_json=payload))
    else:
        row.payload_json = payload
        row.created_at = datetime.now(UTC)
    db.commit()


def generar(db: Session, cartera_id: str, isin: str) -> OnePager:
    """Genera (llamada IA + web) y PERSISTE. El usuario lo regenera explícitamente."""
    nombre, snapshot = _datos(db, cartera_id, isin)
    system, user = build_prompt(nombre, snapshot)
    texto = get_clasificador().investigar(system, user)
    op = parse(texto, nombre, isin)
    creditos.registrar_uso_ia(db, cartera_id, "one_pager", 1)
    op.fecha = date.today().isoformat()
    op.proveedor = settings.ia_provider
    if getattr(settings.mode, "value", settings.mode) == "saas":
        op.disclaimer = _DISCLAIMER
    _persistir(db, cartera_id, op)
    return op
