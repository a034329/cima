"""PASO 0 (doctrina WG): contexto web reciente + clasificación COYUNTURAL/ESTRUCTURAL.

Busca noticias de los últimos meses y aplica el Test de Oportunidad Coyuntural
(5 preguntas) para decidir si un problema es temporal (oportunidad) o estructural
(deterioro de tesis). NO es un hecho: es la lectura de la IA CON FUENTES; el usuario
verifica. Requiere un proveedor IA con búsqueda web (Max CLI en dev; API después).

PASO 0B: segunda búsqueda dirigida a la CAUSA RAÍZ cuando el PASO 0 detecta
disparadores (ESTRUCTURAL/GRIS o riesgos geopolítico/legal/reputacional/reembolsos).
La doctrina WG la marca como OBLIGATORIA ante esos disparadores; aquí la activamos
en modo HÍBRIDO: el backend marca `requiere_0b` y el usuario confirma la 2ª llamada.
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
_PROFUNDIDADES = ("LIGERA", "MEDIA", "GRAVE")
_DISCLAIMER = ("Contexto vía búsqueda web; análisis orientativo de IA, NO asesoramiento. "
               "Verifica las fuentes: puede contener errores o estar desactualizado.")

# Disparadores PASO 0B (doctrina WG): palabras que evidencian causa raíz no
# trivial y requieren 2ª búsqueda dirigida — geopolítica/legal/reputacional/
# reembolsos/fraude. Listado conservador: priorizamos falsos positivos (una
# 2ª llamada IA de más) sobre perder un disparador real.
_KEYWORDS_0B = (
    "geopolític", "geopolitic", "sanción", "sancion", "sanciones",
    "embargo", "guerra", "conflicto", "ataque", "atentado",
    "demanda", "litigio", "pleito", "juicio", "regulator", "regulación",
    "regulacion", "investigación", "investigacion", "multa", "ilegal",
    "fraude", "manipulación", "manipulacion", "scandal", "escándalo",
    "escandalo", "irregularidad", "ocultación", "ocultacion",
    "reembols", "redempt", "rescate", "liquidez", "bank run",
    "reputacion", "reputación", "boicot", "ciberataque", "hackeo", "brecha",
)


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
    requiere_0b: bool = False                # disparador PASO 0B activo
    motivo_0b: str = ""                      # por qué se recomienda profundizar


@dataclass
class AnalisisCausaRaiz:
    """Resultado del PASO 0B: causa exacta + profundidad + horizonte."""
    isin: str
    nombre: str
    causa_exacta: str                        # 2-4 frases: qué pasó, cuándo, cómo
    profundidad: str                         # LIGERA | MEDIA | GRAVE | SIN_DATOS
    horizonte_resolucion: str                # ej: "1-3 meses" | "abierto" | "evento puntual"
    segmentos_afectados: list[dict] = field(default_factory=list)  # [{nombre, peso_pct, impacto}]
    evidencias: list[str] = field(default_factory=list)            # hitos concretos con datos
    conclusion: str = ""                     # mantiene / refuerza / cambia clasificación previa
    nueva_clasificacion: str = ""            # COYUNTURAL | GRIS | ESTRUCTURAL | "" (mantiene)
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


def detectar_disparador_0b(clasificacion: str, resumen: str, riesgo_principal: str
                            ) -> tuple[bool, str]:
    """Decide si el resultado del PASO 0 requiere una 2ª búsqueda dirigida.

    Disparadores (doctrina WG, marcada como OBLIGATORIA):
      1. Clasificación ESTRUCTURAL o GRIS — no podemos decidir con la 1ª pasada.
      2. Aparecen keywords de causa raíz no trivial (geopolítica/legal/reputacional/
         reembolsos/fraude) en resumen o riesgo_principal.

    Devuelve (requiere, motivo_legible).
    """
    clasif = (clasificacion or "").upper()
    if clasif in ("ESTRUCTURAL", "GRIS"):
        return True, f"Clasificación {clasif}: profundizar para confirmar causa raíz y horizonte."
    blob = f"{resumen} {riesgo_principal}".lower()
    encontradas = [k for k in _KEYWORDS_0B if k in blob]
    if encontradas:
        # Devolvemos hasta 3 keywords concretas para que el usuario sepa por qué.
        muestra = ", ".join(sorted(set(encontradas))[:3])
        return True, f"Detectado riesgo cualificado ({muestra}): conviene investigar causa raíz."
    return False, ""


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
    requiere, motivo = detectar_disparador_0b(
        d["clasificacion"], d["resumen"], d["riesgo_principal"],
    )
    return AnalisisContexto(
        isin=isin, nombre=ctx.nombre, resumen=d["resumen"], clasificacion=d["clasificacion"],
        preguntas=d["preguntas"], riesgo_principal=d["riesgo_principal"], fuentes=d["fuentes"],
        fecha=date.today().isoformat(), proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if saas else None),
        requiere_0b=requiere, motivo_0b=motivo,
    )


# ── PASO 0B: 2ª búsqueda dirigida a la causa raíz ───────────────────────────


def build_prompt_0b(nombre: str, sector: str | None,
                    contexto_previo: dict | None) -> tuple[str, str]:
    """Prompt de la 2ª búsqueda. Recibe el resumen + clasificación del PASO 0
    para que la IA NO repita la 1ª pasada y vaya directa a la causa exacta.

    `contexto_previo` (opcional): {resumen, clasificacion, riesgo_principal}.
    Si está vacío, la IA tendrá que descubrir el evento de partida también — más
    caro y menos preciso; preferible llamar siempre con el contexto del 0.
    """
    system = (
        "Eres un analista que ha leído un resumen inicial sobre una empresa y necesita "
        "INVESTIGAR LA CAUSA RAÍZ EXACTA del problema mencionado. Tu objetivo es entender "
        "QUÉ pasó (no que 'hay tensiones'), CUÁNDO, CÓMO de profundo es y QUÉ SEGMENTOS de "
        "negocio afecta. Busca en la web evidencias concretas (fechas, importes, % de ingresos, "
        "decisiones judiciales, comunicados oficiales, datos de reembolsos, sentencias).\n"
        "DISECCIÓN DEL NEGOCIO (PASO 0A de la doctrina WG): identifica el SEGMENTO específico "
        "afectado y su PESO sobre el negocio total. Pesos: >50% sistémico; 20-50% material; "
        "5-20% contenible; <5% probable pánico de mercado. Si los segmentos NO afectados crecen, "
        "refuerza coyuntural; si se deterioran, sistémico.\n"
        "PROFUNDIDAD: LIGERA (evento puntual con horizonte claro <6 meses, sin contagio), "
        "MEDIA (resolución 6-18 meses o contagio acotado), GRAVE (horizonte abierto, contagio "
        "estructural, moat intangible atacado directamente). Si el moat central es intangible "
        "(reputación, confianza, seguridad, credibilidad fiduciaria) y el evento lo ataca → "
        "GRAVE por defecto salvo evidencia positiva sólida.\n"
        "CONCLUSIÓN: di si MANTIENES, REFUERZAS o CAMBIAS la clasificación inicial; si la cambias, "
        "indica la nueva (COYUNTURAL/GRIS/ESTRUCTURAL). CITA fuentes (URLs).\n"
        "Responde EXCLUSIVAMENTE con JSON, sin texto alrededor:\n"
        '{"causa_exacta": "<2-4 frases con qué pasó/cuándo/cómo>", '
        '"profundidad": "LIGERA|MEDIA|GRAVE", '
        '"horizonte_resolucion": "<ej: 1-3 meses | 6-12 meses | abierto>", '
        '"segmentos_afectados": [{"nombre": "...", "peso_pct": <0-100>, "impacto": "<descripción breve>"}], '
        '"evidencias": ["<hito con dato/fecha concreta>"], '
        '"conclusion": "MANTIENE|REFUERZA|CAMBIA — <1 frase>", '
        '"nueva_clasificacion": "COYUNTURAL|GRIS|ESTRUCTURAL|", '
        '"fuentes": ["<url>"]}'
    )
    ctx_str = ""
    if contexto_previo:
        ctx_str = (
            "\nContexto inicial del PASO 0 que YA conoces (no lo repitas — investiga más a fondo):\n"
            f"- Resumen: {contexto_previo.get('resumen', '')}\n"
            f"- Clasificación inicial: {contexto_previo.get('clasificacion', '')}\n"
            f"- Riesgo principal: {contexto_previo.get('riesgo_principal', '')}\n"
        )
    user = (
        f"Empresa: {nombre}" + (f" (sector: {sector})" if sector else "") +
        ".\nInvestiga la CAUSA RAÍZ exacta del problema y diséctala como pide el sistema." +
        ctx_str
    )
    return system, user


def parse_0b(texto: str) -> dict:
    """Extrae el JSON del PASO 0B tolerando prosa/fences. Si falla, deja causa
    exacta vacía y profundidad SIN_DATOS."""
    s = (texto or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data: dict = {}
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            data = obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            data = {}
    prof = str(data.get("profundidad", "")).strip().upper()
    if prof not in _PROFUNDIDADES:
        prof = "SIN_DATOS"
    nueva = str(data.get("nueva_clasificacion", "")).strip().upper()
    if nueva not in _CLASES:
        nueva = ""    # vacío = mantiene la clasificación previa
    segs = data.get("segmentos_afectados")
    segmentos = [s for s in segs if isinstance(s, dict)] if isinstance(segs, list) else []
    evs = data.get("evidencias")
    evidencias = [str(e) for e in evs if e] if isinstance(evs, list) else []
    fus = data.get("fuentes")
    fuentes = [str(u) for u in fus if u] if isinstance(fus, list) else []
    causa = str(data.get("causa_exacta") or "").strip() or (s[:400] if not data else "")
    return {
        "causa_exacta": causa,
        "profundidad": prof,
        "horizonte_resolucion": str(data.get("horizonte_resolucion") or "").strip(),
        "segmentos_afectados": segmentos,
        "evidencias": evidencias,
        "conclusion": str(data.get("conclusion") or "").strip(),
        "nueva_clasificacion": nueva,
        "fuentes": fuentes,
    }


def analizar_causa_raiz(
    db: Session, cartera_id: str, isin: str,
    contexto_previo: dict | None = None,
) -> AnalisisCausaRaiz:
    """PASO 0B: 2ª búsqueda dirigida a la causa raíz exacta.

    `contexto_previo`: dict con {resumen, clasificacion, riesgo_principal} del
    PASO 0; si no se proporciona, la IA arranca a frío (más caro). En el flujo
    híbrido el frontend pasa el contexto que ya tiene del 0.
    """
    from app.services.clasificador import construir_contexto

    ctx = construir_contexto(db, cartera_id, isin)
    system, user = build_prompt_0b(ctx.nombre, ctx.sector, contexto_previo)
    texto = get_clasificador().investigar(system, user)
    d = parse_0b(texto)
    creditos.registrar_uso_ia(db, cartera_id, "contexto_0b", 1)
    saas = getattr(settings.mode, "value", settings.mode) == "saas"
    return AnalisisCausaRaiz(
        isin=isin, nombre=ctx.nombre,
        causa_exacta=d["causa_exacta"], profundidad=d["profundidad"],
        horizonte_resolucion=d["horizonte_resolucion"],
        segmentos_afectados=d["segmentos_afectados"], evidencias=d["evidencias"],
        conclusion=d["conclusion"], nueva_clasificacion=d["nueva_clasificacion"],
        fuentes=d["fuentes"],
        fecha=date.today().isoformat(), proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if saas else None),
    )
