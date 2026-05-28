"""Constructor de prompt + parser compartidos por TODOS los adaptadores IA.

Es la pieza que hace trivial el cambio CLI(Max) → API key: ambos adaptadores
construyen el mismo prompt aquí y parsean igual; solo cambian el transporte.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.adapters.ia.base import (
    BloqueOpcion,
    ClasificadorError,
    ContextoEmpresa,
    SugerenciaBloque,
)

# ── catálogo de bloques (FUENTE ÚNICA) ──────────────────────────────────────
# Cada categoría tiene una ficha: descripción (el papel), criterios (la compuerta
# objetiva, medible) y no_es (la frontera con el vecino, clave de exclusividad).
# El orden del dict es el de la CASCADA: el primer criterio que cumple gana.
# Lo consumen el prompt de la IA, el servicio (rol de cada bloque) y el seed.

@dataclass(frozen=True)
class Ficha:
    nombre: str
    descripcion: str
    criterios: str
    no_es: str
    es_base: bool
    orden: int            # orden de visualización/seed (≠ orden de cascada del dict)
    en_estrategia: bool = True   # ¿cuenta para el objetivo de IF? (Colchón → False)


FICHAS: dict[str, Ficha] = {
    "indice": Ficha(
        nombre="Índice",
        descripcion="Exposición amplia de mercado a bajo coste; la base pasiva.",
        criterios="ETF de renta variable AMPLIO y diversificado (índice de "
                  "mercado global o regional, no temático ni sectorial).",
        no_es="ETF temático o sectorial (tech, IA, robótica, un solo país nicho) "
              "→ Satélite. Acción individual → su bloque de estrategia.",
        es_base=False, orden=7,
    ),
    "renta_fija": Ficha(
        nombre="Renta Fija",
        descripcion="Bonos, letras y monetario PRODUCTIVO que buscas por su retorno.",
        criterios="Bono, letra del Tesoro, ETF de deuda o monetario elegido por "
                  "su rendimiento (parte de la estrategia, no el colchón).",
        no_es="Si es tu colchón de paz mental → Colchón. Renta variable → su bloque.",
        es_base=False, orden=8,
    ),
    "satelite": Ficha(
        nombre="Satélite",
        descripcion="Apuestas asimétricas de convicción; posición pequeña por diseño.",
        criterios="Cripto, ETF temático/sectorial, o acción SIN dividendo con "
                  "tesis especulativa (pre-beneficios, turnaround o volatilidad "
                  "muy alta).",
        no_es="Acción rentable y CONSOLIDADA sin dividendo (Amazon, Alphabet) → "
              "Compounder, NO satélite. Si paga dividendo estable → Estable.",
        es_base=True, orden=5,
    ),
    "aggressive": Ficha(
        nombre="High Yield",
        descripcion="Rentas altas para flujo de caja (BDC, REIT, covered-call, MLP).",
        criterios="Yield > 6%. Un yield muy alto en mercado eficiente es señal de "
                  "RIESGO, no de oportunidad: comprueba la cobertura del dividendo.",
        no_es="Yield < 6% → baja a Dividend Growth o Estable según crecimiento. "
              "Cripto o especulativo sin dividendo → Satélite.",
        es_base=True, orden=4,
    ),
    "income": Ficha(
        nombre="Dividend Growth",
        descripcion="El dividendo que sube cada año; tus 'subidas de sueldo' (B).",
        criterios="Yield 1,5-5% Y crecimiento del dividendo > 8% anual Y payout "
                  "30-60%. Calidad con BPA creciente.",
        no_es="Yield > 6% → High Yield. Dividendo plano + baja volatilidad → "
              "Estable. Yield < 1,5% con gran crecimiento → Compounder.",
        es_base=True, orden=2,
    ),
    "defensivo": Ficha(
        nombre="Estable",
        descripcion="Preservación y calma; el 'Seguro de Vida' (A), lo que te deja dormir.",
        criterios="Yield 3-6%, beta < 0,9, sector defensivo (consumo básico, "
                  "utilities, salud), dividendo estable de crecimiento modesto.",
        no_es="Si el dividendo crece a doble dígito → Dividend Growth. Si yield "
              "> 6% → High Yield. Si beta alta o cíclica → no es estable.",
        es_base=True, orden=3,
    ),
    "growth": Ficha(
        nombre="Compounders",
        descripcion="Máximo crecimiento del capital; la calidad que compone (C).",
        criterios="Yield < 2-3%, payout < 30%, ROIC alto (> 15%), crecimiento de "
                  "ingresos/BPA > 10%. Reinvierte en lugar de repartir.",
        no_es="Si reparte yield > 6% → High Yield. Si el dividendo crece a doble "
              "dígito con yield 1,5-5% → Dividend Growth.",
        es_base=True, orden=1,
    ),
    "colchon": Ficha(
        nombre="Colchón",
        descripcion="NO es capital de inversión: paz mental. Fuera del objetivo IF (F).",
        criterios="DESIGNACIÓN MANUAL del usuario (monetario EUR, ETF de mínima "
                  "volatilidad EUR). NUNCA la asignes tú.",
        no_es="Cualquier activo elegido por su retorno → va a su bloque de "
              "estrategia, no aquí.",
        es_base=True, orden=6, en_estrategia=False,
    ),
    "cripto": Ficha(
        nombre="Cripto",
        descripcion="Criptoactivos como clase propia (BTC/ETH como reserva de valor "
                    "o apuesta de convicción).",
        criterios="Criptomonedas y tokens. Alta volatilidad; tamaño contenido.",
        no_es="Si es una apuesta especulativa más entre varias → Satélite. "
              "ETF o acción → su bloque por fundamentales.",
        es_base=False, orden=9,
    ),
    "materias_primas": Ficha(
        nombre="Materias primas",
        descripcion="Oro y materias primas: cobertura de inflación y diversificador "
                    "de régimen (estilo cartera permanente).",
        criterios="Oro, plata, energía, ETF de materias primas, o mineras de oro "
                  "como proxy.",
        no_es="Acción operativa normal → su bloque por fundamentales. Bono ligado "
              "a inflación → Renta Fija.",
        es_base=False, orden=10,
    ),
}

# Rol WG derivado (fuente única para el servicio y el chequeo de pertenencia del
# parser): {codigo: "Nombre: descripción"}.
ROLES_CATEGORIA: dict[str, str] = {
    cod: f"{f.nombre}: {f.descripcion}" for cod, f in FICHAS.items()
}

# La cascada que ve la IA (orden del dict = prioridad; primer match gana).
_CASCADA_TXT = "\n".join(
    f"{i}. {f.nombre} [{cod}]\n"
    f"   · Criterios: {f.criterios}\n"
    f"   · No es: {f.no_es}"
    for i, (cod, f) in enumerate(FICHAS.items(), start=1)
)

_CODIGOS_VALIDOS = "|".join(FICHAS)

_SYSTEM = (
    "Eres un analista de cartera senior que clasifica UN valor dentro de la "
    "estrategia Wealth Guardian. Trabajas como una CASCADA ORDENADA: recorre las "
    "categorías en orden y asigna la PRIMERA cuyos criterios se cumplen (así los "
    "bloques son excluyentes). Usa el campo 'No es' para descartar la frontera.\n\n"
    f"{_CASCADA_TXT}\n\n"
    "Reglas de juicio:\n"
    "- Juzga el NEGOCIO (moat, durabilidad, calidad del modelo), no la forma de "
    "la gráfica: una cotización 'perfecta' suele significar CARA, no segura.\n"
    "- Señales (no reglas; pésalas con sector y moat): beta < 0,9 apunta a Estable "
    "(baja volatilidad); ROE alto + yield bajo + crecimiento apunta a Compounder.\n"
    "- 'colchon' es designación MANUAL del usuario: NUNCA la sugieras tú.\n"
    "- Si tu veredicto objetivo contradice un sesgo emocional típico (p.ej. una "
    "marca de calidad que 'se siente' defensiva pero es un compounder), DILO en "
    "el razonamiento — discrepar con argumentos es tu valor, no obedecer.\n"
    "- Usa los datos aportados y tu conocimiento de la empresa. No inventes cifras.\n\n"
    "Responde EXCLUSIVAMENTE con un objeto JSON, sin texto alrededor, con esta "
    "forma exacta:\n"
    f'{{"categoria_base": "<una de: {_CODIGOS_VALIDOS}>", '
    '"bloque_id": "<id del catálogo o null>", '
    '"razonamiento": "<2-3 frases; incluye el rebate si procede>", '
    '"confianza": <número 0..1>, '
    '"distribucion": [{"categoria": "<codigo>", "prob": <0..1>}]}'
)


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _fmt_num(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def _fmt_ejemplos(ejemplos: list[dict]) -> str:
    """Few-shot del sesgo del usuario: sus overrides previos (sugerida ≠ elegida).
    Contexto, NO regla — si el criterio objetivo difiere, la IA debe decirlo."""
    filas = []
    for e in ejemplos:
        quien = e.get("nombre") or e.get("isin")
        sector = f" ({e['sector']})" if e.get("sector") else ""
        motivo = f". Motivo: {e['razon']}" if e.get("razon") else ""
        filas.append(
            f"- {quien}{sector}: la IA sugirió '{e.get('categoria_sugerida')}', "
            f"el usuario lo puso en '{e.get('categoria_elegida')}'{motivo}"
        )
    return (
        "DECISIONES PREVIAS DE ESTE USUARIO (su sesgo personal; tenlo en cuenta "
        "como CONTEXTO, no como regla. Si tu criterio objetivo difiere con "
        "claridad, clasifícalo bien y explícalo en el razonamiento):\n"
        + "\n".join(filas)
    )


def build_mensajes(
    ctx: ContextoEmpresa, catalogo: list[BloqueOpcion],
    ejemplos: list[dict] | None = None,
) -> tuple[str, str]:
    """Devuelve (system, user). El system es estable (cacheable); el user lleva
    el contexto concreto de la empresa + el catálogo + (opcional) el few-shot de
    overrides del usuario."""
    emp = (
        f"EMPRESA A CLASIFICAR\n"
        f"- Nombre: {ctx.nombre}\n"
        f"- ISIN: {ctx.isin}\n"
        f"- Tipo de activo: {ctx.tipo_activo or '—'}\n"
        f"- Sector: {ctx.sector or '—'}\n"
        f"- Industria: {ctx.industria or '—'}\n"
        f"- Divisa: {ctx.divisa or '—'}\n"
        f"- Dividend yield: {_fmt_pct(ctx.yield_pct)}\n"
        f"- Dividendo/acción: {_fmt_num(ctx.dividendo_share)}\n"
        f"- PER: {_fmt_num(ctx.per)}\n"
        f"- Beta (volatilidad vs mercado): {_fmt_num(ctx.beta)}\n"
        f"- ROE (proxy de calidad/ROIC): {_fmt_pct(ctx.roe)}\n"
        f"- Crecimiento BPA estimado (CAGR): {_fmt_pct(ctx.crecimiento_eps_pct)}\n"
        f"- Retorno total anual estimado (CAGR4+Div): {_fmt_pct(ctx.cagr4_div_pct)}\n"
    )
    bloques = "BLOQUES DE LA CARTERA (elige bloque_id de aquí o null):\n" + (
        "\n".join(
            f'- id="{b.id}" · "{b.nombre}" · categoria={b.categoria_base}' for b in catalogo
        )
        or "(la cartera aún no tiene bloques creados)"
    )
    partes = [emp, bloques]
    if ejemplos:
        partes.append(_fmt_ejemplos(ejemplos))
    return _SYSTEM, "\n\n".join(partes)


_SYSTEM_LOTE = (
    "Eres un analista de cartera que clasifica varios valores dentro de la "
    "estrategia Wealth Guardian. Categorías (cascada: primer criterio que cumple "
    "gana; 'colchon' es manual, no la asignes):\n"
    + "\n".join(f"- {cod}: {f.nombre} — {f.criterios}" for cod, f in FICHAS.items())
    + "\n\nClasifica CADA empresa de la lista en su categoría. Responde "
    "EXCLUSIVAMENTE con un array JSON, sin texto alrededor, una entrada por "
    "empresa, con esta forma exacta (sin razonamiento largo, sé conciso):\n"
    f'[{{"isin": "<isin>", "categoria_base": "<{_CODIGOS_VALIDOS}>", '
    '"confianza": <0..1>}]'
)


def build_mensajes_lote(
    empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion]
) -> tuple[str, str]:
    """(system, user) para clasificar EN LOTE. Contexto comprimido (una línea por
    empresa) y salida tersa → menos tokens, menos preciso que `build_mensajes`."""
    filas = "\n".join(
        f"- {e.isin} | {e.nombre} | sector={e.sector or '—'} | "
        f"yield={_fmt_pct(e.yield_pct)} | beta={_fmt_num(e.beta)} | ROE={_fmt_pct(e.roe)} | "
        f"crecBPA={_fmt_pct(e.crecimiento_eps_pct)} | CAGR4+Div={_fmt_pct(e.cagr4_div_pct)}"
        for e in empresas
    )
    bloques = "BLOQUES DE LA CARTERA (categorías disponibles):\n" + (
        "\n".join(f"- {b.categoria_base}: \"{b.nombre}\"" for b in catalogo)
        or "(la cartera aún no tiene bloques creados)"
    )
    return _SYSTEM_LOTE, f"EMPRESAS A CLASIFICAR:\n{filas}\n\n{bloques}"


def _extraer_array(texto: str) -> list:
    """Extrae el primer array JSON del texto (tolerante a fences/prosa)."""
    s = texto.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    else:
        m = re.search(r"\[.*\]", s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        data = json.loads(s, strict=False)
        if isinstance(data, list):
            return data
    except (ValueError, TypeError):
        pass
    # Fallback: recuperar objeto a objeto por regex (objetos planos, sin anidar).
    objetos = [_campos_por_regex(o) for o in re.findall(r"\{[^{}]*\}", s)]
    objetos = [o for o in objetos if o.get("categoria_base") and o.get("isin")]
    if objetos:
        return objetos
    raise ClasificadorError(f"Respuesta IA (lote) no es JSON: {texto[:200]!r}")


def parse_respuesta_lote(
    texto: str, empresas: list[ContextoEmpresa], catalogo: list[BloqueOpcion],
    modelo: str, proveedor: str,
) -> list[SugerenciaBloque]:
    """Parsea el array de clasificaciones y lo mapea a las empresas de entrada
    por ISIN. Empresas sin entrada válida en la respuesta se omiten."""
    por_isin = {str(d.get("isin")): d for d in _extraer_array(texto) if isinstance(d, dict)}
    out: list[SugerenciaBloque] = []
    for e in empresas:
        d = por_isin.get(e.isin)
        if not d:
            continue
        cat = str(d.get("categoria_base", "")).strip().lower()
        if cat not in ROLES_CATEGORIA:
            continue
        bloque_id = next((b.id for b in catalogo if b.categoria_base == cat), None)
        try:
            conf = max(0.0, min(1.0, float(d.get("confianza", 0.5))))
        except (ValueError, TypeError):
            conf = 0.5
        out.append(SugerenciaBloque(
            categoria_base=cat, bloque_id=bloque_id,
            razonamiento=str(d.get("razonamiento", "")).strip()
            or "(autoclasificación en lote — menos preciso)",
            confianza=conf, modelo=modelo, proveedor=proveedor, isin=e.isin,
        ))
    return out


def _campos_por_regex(texto: str) -> dict:
    """Recupera los campos clave por regex cuando el JSON viene malformado (causa
    típica: comillas sin escapar en el razonamiento de modelos verbosos). La
    categoría y la confianza son lo que importa para clasificar; el razonamiento
    se recupera best-effort y puede quedar truncado."""
    d: dict = {}
    cat = re.search(r'"categoria_base"\s*:\s*"([a-zA-Z_]+)"', texto)
    if cat:
        d["categoria_base"] = cat.group(1)
    conf = re.search(r'"confianza"\s*:\s*([0-9.]+)', texto)
    if conf:
        d["confianza"] = conf.group(1)
    isin = re.search(r'"isin"\s*:\s*"([^"]+)"', texto)
    if isin:
        d["isin"] = isin.group(1)
    raz = re.search(r'"razonamiento"\s*:\s*"(.+?)"\s*[,}]', texto, re.DOTALL)
    if raz:
        d["razonamiento"] = raz.group(1)
    return d


def _extraer_json(texto: str) -> dict:
    """Extrae el primer objeto JSON del texto (tolerante a ```json``` o prosa).
    `strict=False` admite saltos de línea en strings; si aun así falla, recupera
    los campos por regex (no romper la clasificación por un JSON imperfecto)."""
    s = texto.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    else:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        return json.loads(s, strict=False)
    except (ValueError, TypeError):
        d = _campos_por_regex(texto)
        if d.get("categoria_base"):
            return d
        raise ClasificadorError(f"Respuesta IA no es JSON válido: {texto[:200]!r}")


def _parse_distribucion(d: dict) -> list[dict] | None:
    """Distribución de probabilidad opcional [{categoria, prob}]. Filtra entradas
    con categoría desconocida o prob no numérica; devuelve None si no hay nada útil."""
    raw = d.get("distribucion")
    if not isinstance(raw, list):
        return None
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        c = str(item.get("categoria", "")).strip().lower()
        if c not in ROLES_CATEGORIA:
            continue
        try:
            p = max(0.0, min(1.0, float(item.get("prob", 0))))
        except (ValueError, TypeError):
            continue
        out.append({"categoria": c, "prob": p})
    return out or None


def parse_respuesta(
    texto: str, catalogo: list[BloqueOpcion], modelo: str, proveedor: str
) -> SugerenciaBloque:
    """Parsea la respuesta del modelo a SugerenciaBloque. Valida la categoría y,
    si el modelo no dio bloque_id, mapea al primer bloque de esa categoría."""
    d = _extraer_json(texto)
    cat = str(d.get("categoria_base", "")).strip().lower()
    if cat not in ROLES_CATEGORIA:
        raise ClasificadorError(f"categoria_base inválida en respuesta IA: {cat!r}")

    bloque_id = d.get("bloque_id")
    ids_validos = {b.id for b in catalogo}
    if bloque_id not in ids_validos:           # None, vacío o id inventado
        bloque_id = next((b.id for b in catalogo if b.categoria_base == cat), None)

    try:
        conf = max(0.0, min(1.0, float(d.get("confianza", 0.5))))
    except (ValueError, TypeError):
        conf = 0.5

    return SugerenciaBloque(
        categoria_base=cat,
        bloque_id=bloque_id,
        razonamiento=str(d.get("razonamiento", "")).strip() or "(sin razonamiento)",
        confianza=conf,
        modelo=modelo,
        proveedor=proveedor,
        distribucion=_parse_distribucion(d),
    )
