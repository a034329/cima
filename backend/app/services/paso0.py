"""PASO 0 (doctrina WG): contexto web reciente + clasificación COYUNTURAL/ESTRUCTURAL.

Busca noticias de los últimos meses y aplica el Test de Oportunidad Coyuntural
(5 preguntas) para decidir si un problema es temporal (oportunidad) o estructural
(deterioro de tesis). NO es un hecho: es la lectura de la IA CON FUENTES; el usuario
verifica. Requiere un proveedor IA con búsqueda web (Max CLI en dev; API después).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.config import settings
from app.services import creditos

_CLASES = ("COYUNTURAL", "GRIS", "ESTRUCTURAL")
_DISCLAIMER = ("Contexto vía búsqueda web; análisis orientativo de IA, NO asesoramiento. "
               "Verifica las fuentes: puede contener errores o estar desactualizado.")


@dataclass
class AnalisisContexto:
    isin: str
    nombre: str
    resumen: str
    clasificacion: str                       # COYUNTURAL | GRIS | ESTRUCTURAL | SIN_DATOS
    preguntas: list[dict] = field(default_factory=list)   # [{pregunta, respuesta, senal}]
    riesgo_principal: str = ""
    fuentes: list[str] = field(default_factory=list)
    fecha: str = ""
    proveedor: str = ""
    disclaimer: str | None = None


def build_prompt(nombre: str, sector: str | None) -> tuple[str, str]:
    system = (
        "Eres un analista que investiga el CONTEXTO CUALITATIVO de una empresa para decidir si un "
        "problema es COYUNTURAL (temporal, reversible → oportunidad) o ESTRUCTURAL (deterioro "
        "permanente de la tesis → salir). Busca en la web NOTICIAS de los últimos 3-6 meses: "
        "resultados, guidance, legal/regulatorio, cambios de management, problemas sectoriales, "
        "pérdida de cuota o deterioro del moat.\n"
        "Aplica el TEST DE OPORTUNIDAD COYUNTURAL (5 preguntas): 1) ¿Sigue generando caja? "
        "2) ¿Afecta a toda la industria (headwind sectorial) o solo a esta empresa? 3) ¿Horizonte "
        "temporal claro (evento puntual) o abierto? 4) ¿Management creíble (sin ocultación/fraude)? "
        "5) ¿Mismo negocio en 3-4 años? Cada respuesta favorable suma: 4-5 → COYUNTURAL, 2-3 → GRIS, "
        "0-1 → ESTRUCTURAL.\n"
        "REGLA DEL MOAT INTANGIBLE: si el moat central es intangible (confianza, reputación, "
        "seguridad, credibilidad fiduciaria) y el evento lo ataca directamente → ESTRUCTURAL por "
        "defecto. CITA las fuentes (URLs). Si no hay noticias relevantes, dilo y clasifica por defecto.\n"
        "Responde EXCLUSIVAMENTE con JSON, sin texto alrededor:\n"
        '{"resumen": "<2-3 frases>", "clasificacion": "COYUNTURAL|GRIS|ESTRUCTURAL", '
        '"preguntas": [{"pregunta": "...", "respuesta": "...", "senal": "coyuntural|estructural|mixta"}], '
        '"riesgo_principal": "<1 frase>", "fuentes": ["<url>"]}'
    )
    user = (f"Empresa: {nombre}" + (f" (sector: {sector})" if sector else "")
            + ". Investiga su contexto reciente y clasifícalo.")
    return system, user


def parse(texto: str) -> dict:
    """Extrae el JSON tolerando prosa/fences alrededor; si falla, deja el texto como resumen."""
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
        clasif = "SIN_DATOS"
    pregs = data.get("preguntas")
    preguntas = [p for p in pregs if isinstance(p, dict)] if isinstance(pregs, list) else []
    fus = data.get("fuentes")
    fuentes = [str(u) for u in fus if u] if isinstance(fus, list) else []
    resumen = str(data.get("resumen") or "").strip() or (s[:400] if not data else "")
    return {"resumen": resumen, "clasificacion": clasif, "preguntas": preguntas,
            "riesgo_principal": str(data.get("riesgo_principal") or "").strip(), "fuentes": fuentes}


def analizar_contexto(db: Session, cartera_id: str, isin: str) -> AnalisisContexto:
    """Busca contexto web de `isin` y lo clasifica coyuntural/estructural. Consume una
    llamada IA con búsqueda (lenta); registra crédito."""
    from app.services.clasificador import construir_contexto

    ctx = construir_contexto(db, cartera_id, isin)   # nombre + sector (posición o seguimiento)
    system, user = build_prompt(ctx.nombre, ctx.sector)
    texto = get_clasificador().investigar(system, user)
    d = parse(texto)
    creditos.registrar_uso_ia(db, cartera_id, "contexto", 1)
    saas = getattr(settings.mode, "value", settings.mode) == "saas"
    return AnalisisContexto(
        isin=isin, nombre=ctx.nombre, resumen=d["resumen"], clasificacion=d["clasificacion"],
        preguntas=d["preguntas"], riesgo_principal=d["riesgo_principal"], fuentes=d["fuentes"],
        fecha=date.today().isoformat(), proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if saas else None),
    )
