"""Onboarding IA (1.5): el usuario diseña su estrategia con la IA y la firma.

Wizard en pasos discretos: perfil → la IA PROPONE un reparto de bloques con
objetivos % + razonamiento → el usuario ajusta → firma. La firma aplica los
`peso_objetivo` a los bloques y guarda un `PlanFirmado` (contrato de Ulises que
la fricción referencia). La IA propone PESOS por bloque, nunca valores concretos.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.adapters.ia.prompt import FICHAS
from app.config import settings
from app.db import models

_DISCLAIMER = (
    "Propuesta orientativa generada por IA, NO asesoramiento de inversión. Ajústala "
    "a tu criterio: la decisión y la firma son tuyas."
)


@dataclass
class PropuestaBloque:
    categoria_base: str
    peso_objetivo: float          # fracción 0..1
    razon: str


@dataclass
class Viabilidad:
    capital_actual_eur: float        # invertido en estrategia hoy (base)
    aportaciones_eur: float          # aportación mensual × 12 × horizonte
    cagr_requerido_pct: float | None # retorno anual aprox. necesario (fracción); None si no calculable
    viable: bool
    veredicto: str


@dataclass
class PropuestaEstrategia:
    bloques: list[PropuestaBloque] = field(default_factory=list)
    resumen: str = ""
    disclaimer: str | None = None
    viabilidad: Viabilidad | None = None


_UMBRAL_VIABLE = 0.15            # CAGR anual por encima del cual el objetivo es poco realista


def _capital_actual(db: Session, cartera_id: str) -> Decimal:
    """Capital invertido HOY en bloques de estrategia, a VALOR DE MERCADO (la
    misma base que usa el dashboard para los años a IF). Antes usaba el coste
    FIFO, lo que infraba el punto de partida y disparaba el retorno requerido."""
    from app.services.dashboard import capital_en_estrategia_eur

    return capital_en_estrategia_eur(db, cartera_id)


def _viabilidad(perfil: dict, capital_actual: Decimal) -> Viabilidad | None:
    objetivo = perfil.get("objetivo_if_eur")
    horizonte = perfil.get("horizonte_anios")
    aport_mes = perfil.get("aportacion_mensual_eur") or 0
    if not objetivo or not horizonte or float(horizonte) <= 0:
        return None
    from app.services import proyeccion

    n = float(horizonte)              # admite fracciones (p.ej. 2,5 años)
    aport_anual = Decimal(str(aport_mes)) * 12
    aportes = float(aport_anual) * n
    # Retorno requerido = inverso de la proyección del dashboard (capital a
    # mercado + aportación mensual capitalizada). Consistente por construcción.
    cagr = proyeccion.retorno_requerido(capital_actual, aport_anual, Decimal(str(objetivo)), n)
    if cagr is None:
        ver = ("Poco realista: ni con un retorno extraordinario llegas en este plazo. "
               "Alarga el horizonte o sube la aportación.")
        return Viabilidad(float(capital_actual), aportes, None, False, ver)
    viable = cagr <= _UMBRAL_VIABLE
    if cagr <= 0.07:
        ver = "Holgado: el objetivo es alcanzable con una cartera equilibrada."
    elif cagr <= 0.12:
        ver = "Exigente pero posible con una cartera de crecimiento."
    elif cagr <= _UMBRAL_VIABLE:
        ver = "Muy exigente: solo con riesgo alto y sin garantías."
    else:
        ver = (f"Poco realista: necesitarías ~{cagr * 100:.0f}% anual, que ninguna cartera "
               "diversificada da con seguridad. Alarga el horizonte o sube la aportación.")
    return Viabilidad(float(capital_actual), aportes, cagr, viable, ver)


def _catalogo(db: Session, cartera_id: str) -> list[models.Bloque]:
    return list(db.execute(
        select(models.Bloque)
        .where(models.Bloque.cartera_id == cartera_id)
        .order_by(models.Bloque.orden)
    ).scalars())


def build_prompt(perfil: dict, dist_bloques: list, viab: Viabilidad | None) -> tuple[str, str]:
    reales = [b for b in dist_bloques if b.categoria_base != "sin_clasificar"]
    cats = "\n".join(
        f"- {b.categoria_base}: {b.nombre}"
        f"{'' if b.en_estrategia else ' (FUERA de la estrategia IF — no le asignes objetivo)'}"
        f" — {FICHAS[b.categoria_base].descripcion if b.categoria_base in FICHAS else ''}"
        for b in reales
    )
    # Cartera ACTUAL por bloque (lo que el usuario ya tiene importado) → la IA
    # propone objetivos teniendo en cuenta el punto de partida y el hueco.
    con_valor = [b for b in dist_bloques if float(b.valor_eur) > 0]
    if con_valor:
        actual = "TU CARTERA ACTUAL POR BLOQUE:\n" + "\n".join(
            f"- {b.nombre}: {float(b.peso_actual) * 100:.0f}% ({float(b.valor_eur):.0f} €)"
            for b in con_valor
        )
    else:
        actual = "TU CARTERA ACTUAL: sin posiciones todavía (cartera nueva)."
    system = (
        "Eres un asesor que propone una ASIGNACIÓN por bloques (pesos objetivo %) "
        "para una estrategia hacia la independencia financiera (IF), según el perfil. "
        "Propones SOLO el reparto por bloque, NUNCA valores concretos.\n"
        "El reparto lo determinan (1) el RETORNO ANUAL REQUERIDO para alcanzar el "
        "objetivo y (2) la tolerancia al riesgo. NO uses el horizonte como atajo de "
        "prudencia: un horizonte corto NO implica ser conservador.\n"
        "- Si el retorno requerido es alto, hace falta más Compounders/Dividend Growth "
        "(crecimiento); ser conservador GARANTIZA no llegar.\n"
        "- Si el retorno requerido es bajo o ya se alcanza con aportaciones, prioriza "
        "preservar (Estable/Renta Fija).\n"
        "- Si el retorno requerido es POCO REALISTA (lo verás en el perfil), dilo "
        "claramente en el resumen (alargar horizonte o subir aportación) en vez de "
        "proponer una cartera condenada a fallar.\n"
        "Los pesos de los bloques EN estrategia suman ~1.0 (no asignes objetivo a los "
        "marcados FUERA).\n\n"
        "Responde EXCLUSIVAMENTE con un objeto JSON, sin texto alrededor:\n"
        '{"bloques": [{"categoria_base": "<codigo>", "peso_objetivo": <0..1>, '
        '"razon": "<1 frase>"}], "resumen": "<2-3 frases>"}'
    )
    viab_txt = ""
    if viab is not None:
        req = (f"{viab.cagr_requerido_pct * 100:.1f}%" if viab.cagr_requerido_pct is not None
               else "—")
        viab_txt = (
            f"\n- Capital ya invertido en estrategia: {viab.capital_actual_eur:.0f} €\n"
            f"- Aportaciones previstas en el horizonte: {viab.aportaciones_eur:.0f} €\n"
            f"- RETORNO ANUAL REQUERIDO (aprox) para el objetivo: {req}"
            f"{' — POCO REALISTA' if not viab.viable else ''}\n"
        )
    user = (
        f"PERFIL DEL USUARIO:\n"
        f"- Objetivo IF: {perfil.get('objetivo_if_eur', '—')} €\n"
        f"- Horizonte: {perfil.get('horizonte_anios', '—')} años\n"
        f"- Aportación mensual: {perfil.get('aportacion_mensual_eur', '—')} €\n"
        f"- Tolerancia al riesgo: {perfil.get('tolerancia', '—')}\n"
        f"- Fase: {perfil.get('fase', '—')}{viab_txt}\n"
        f"{actual}\n\n"
        f"BLOQUES DISPONIBLES (propón peso por categoria_base):\n{cats}"
    )
    return system, user


def parse_propuesta(texto: str, categorias_validas: set[str]) -> PropuestaEstrategia:
    s = texto.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data = {}
    if m:
        try:
            data = json.loads(m.group(0), strict=False)
        except (ValueError, TypeError):
            data = {}
    out: list[PropuestaBloque] = []
    for b in data.get("bloques", []) if isinstance(data, dict) else []:
        if not isinstance(b, dict):
            continue
        cat = str(b.get("categoria_base", "")).strip().lower()
        if cat not in categorias_validas:
            continue
        try:
            peso = max(0.0, min(1.0, float(b.get("peso_objetivo", 0))))
        except (ValueError, TypeError):
            continue
        out.append(PropuestaBloque(cat, peso, str(b.get("razon", "")).strip()))
    return PropuestaEstrategia(bloques=out, resumen=str(data.get("resumen", "")).strip())


def proponer_estrategia(db: Session, cartera_id: str, perfil: dict) -> PropuestaEstrategia:
    from app.services.bloques import calcular_distribucion

    dist = calcular_distribucion(db, cartera_id)
    cats = {b.categoria_base for b in dist.bloques
            if b.en_estrategia and b.categoria_base != "sin_clasificar"}
    viab = _viabilidad(perfil, _capital_actual(db, cartera_id))
    system, user = build_prompt(perfil, dist.bloques, viab)
    texto = get_clasificador().completar(system, user, timeout_s=settings.ia_chat_timeout_s)
    prop = parse_propuesta(texto, cats)
    prop.viabilidad = viab
    if getattr(settings.mode, "value", settings.mode) == "saas":
        prop.disclaimer = _DISCLAIMER
    return prop


def firmar_plan(
    db: Session, cartera_id: str, perfil: dict, objetivos: dict[str, float]
) -> models.PlanFirmado:
    """Aplica los pesos a los bloques + actualiza el objetivo IF/aportación de la
    cartera + guarda el PlanFirmado versionado."""
    from app.services.bloques import editar_bloque

    bloques = _catalogo(db, cartera_id)
    por_categoria = {b.categoria_base: b for b in bloques}
    for cat, peso in objetivos.items():
        b = por_categoria.get(cat)
        if b is None:
            continue
        editar_bloque(db, cartera_id, b.id,
                      peso_objetivo=Decimal(str(peso)), set_peso=True)

    cartera = db.get(models.Cartera, cartera_id)
    if cartera is not None:
        if perfil.get("objetivo_if_eur") is not None:
            cartera.objetivo_if_eur = Decimal(str(perfil["objetivo_if_eur"]))
        if perfil.get("aportacion_mensual_eur") is not None:
            cartera.aportacion_mensual_eur = Decimal(str(perfil["aportacion_mensual_eur"]))

    ultima = db.execute(
        select(models.PlanFirmado.version)
        .where(models.PlanFirmado.cartera_id == cartera_id)
        .order_by(models.PlanFirmado.version.desc())
    ).scalars().first()
    plan = models.PlanFirmado(
        cartera_id=cartera_id, version=(ultima or 0) + 1,
        perfil_json=json.dumps(perfil, ensure_ascii=False, default=str),
        objetivos_json=json.dumps(objetivos, ensure_ascii=False, default=str),
        resumen=perfil.get("resumen"),
    )
    db.add(plan)
    db.commit()
    return plan


def plan_firmado_actual(db: Session, cartera_id: str) -> models.PlanFirmado | None:
    return db.execute(
        select(models.PlanFirmado)
        .where(models.PlanFirmado.cartera_id == cartera_id)
        .order_by(models.PlanFirmado.version.desc())
    ).scalars().first()
