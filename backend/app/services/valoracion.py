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

# ── Guardias (bug BAM 5-jun-2026: IA confundió FRE total $5.63B con FRE/acc) ─
# La métrica base SIEMPRE debe ser POR ACCIÓN; si excede el 50% del precio es
# casi seguro un agregado mal etiquetado y el precio objetivo se infla por el
# número de acciones (~1.638B en el caso BAM).
_RATIO_METRICA_PRECIO_MAX = 0.5
# CAGR implícito por bloque (umbral de aviso). Por encima del bloqueo absoluto,
# el escenario se marca como `bloqueado` y la UI no permite aplicarlo.
_UMBRAL_CAGR_BLOQUEO = 0.35
_UMBRAL_CAGR_ALERTA: dict[str, float] = {
    "growth":     0.30,
    "income":     0.20,
    "defensivo":  0.20,
    "aggressive": 0.25,
}
# Default para bloques sin clasificar o desconocidos (incluye 'colchon', etc.).
_UMBRAL_CAGR_DEFAULT = 0.30


@dataclass
class Escenario:
    nombre: str                  # conservador | base | optimista
    multiplo: float
    metrica_base_4y: float       # EPS proyectado a 4 años — SIEMPRE por acción
    precio_objetivo: float
    cagr4_pct: float | None
    razon: str
    # Guardias post-cálculo (bug BAM). Permiten al frontend bloquear "Aplicar"
    # si el escenario es sospechoso de error dimensional / CAGR irreal.
    alertas: list = field(default_factory=list)
    bloqueado: bool = False
    # Desglose paso a paso del cálculo para la UI ("Cómo se calcula").
    # Cada item: {"etiqueta": "FRE/acc 4Y", "valor": "3.54", "calc": "1.92 × (1.17)⁴"}.
    desglose: list = field(default_factory=list)


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
        # ── Guardia crítica (bug BAM 5-jun-2026): per-share, NUNCA agregado ──
        f"REGLA CRÍTICA — POR ACCIÓN: «metrica_4y» SIEMPRE debe estar expresada POR ACCIÓN "
        f"(en {met_label}), NUNCA como agregado total (no $B totales, no FRE total "
        f"de la compañía). Si encuentras la guía como total, DIVIDE por las "
        f"acciones en circulación antes de devolverla. Como check rápido: "
        f"metrica_4y debe ser bastante menor que el precio actual de la acción. "
        f"Si metrica_4y > 50% del precio actual, casi seguro estás confundiendo "
        f"agregado con per-share — revísalo.\n"
        # ── Reglas de integridad analítica (anti-fallos conversación LVMH→Hermès 2026-06-09) ──
        # Mismo patrón que el asesor: confusión TTM/Forward, divisas, fuentes
        # discrepantes y falta de auto-check. Aquí son más críticas porque la
        # salida se TRADUCE en un precio objetivo que va al modelo del usuario.
        f"REGLA — SERIE DEL MÚLTIPLO (TTM vs Forward): el «multiplo» objetivo y el "
        f"«metrica_4y» deben ser de SERIES COMPATIBLES. Para una proyección 4Y "
        f"usa el múltiplo de la SERIE FORWARD (no TTM): forward {mult_label} de "
        f"la empresa o de comparables del sector. Si citas la banda histórica en "
        f"la `razon`, debe ser de la MISMA serie (no 'PER histórico 45-65x TTM' "
        f"cuando tu múltiplo objetivo es 30x forward — no son comparables). En la "
        f"razón, di qué SERIE estás usando.\n"
        f"REGLA — DIVISA: el «metrica_4y» debe estar en la MISMA divisa que el "
        f"precio actual de la acción que ves en las anclas. Si el EPS/FCF/FRE/NAV "
        f"que encuentras viene en USD pero la acción cotiza en EUR/GBp/etc. "
        f"(ADRs, multi-listed, listados europeos de US shares), CONVIERTE antes "
        f"de devolver. Si no estás seguro de la divisa de la fuente, dilo en la "
        f"razón.\n"
        f"REGLA — CONTRASTE DE FUENTES: si la fuente que usas (consenso de "
        f"analistas, modelo DCF como SimplyWallSt, target del propio emisor) "
        f"DIFIERE más de un 10% del precio objetivo implícito por tu multiplo × "
        f"metrica_4y, MENCIÓNALO en la razón. No elijas una fuente sin más; "
        f"una discrepancia del 30%+ es zona de incertidumbre y el escenario "
        f"base debe ser conservador.\n"
        f"REGLA — AUTO-CHECK DE COHERENCIA: antes de devolver, verifica para el "
        f"escenario BASE que (multiplo × metrica_4y) implica un CAGR razonable "
        f"desde el precio actual. Si CAGR > 30% anual para 4 años en un negocio "
        f"maduro, sospecha de error dimensional o de múltiplo inflado — revisa.\n"
        "Los escenarios DEBEN derivarse de la TESIS del negocio, no de números al azar: el OPTIMISTA "
        "refleja que los drivers de crecimiento de la tesis se cumplen; el CONSERVADOR refleja que se "
        "materializan los riesgos; el BASE es el caso central. "
        + anclaje + " Puedes buscar en la web. No calcules el precio objetivo (lo hace el sistema).\n"
        "Responde EXCLUSIVAMENTE con JSON:\n"
        '{"escenarios": [{"nombre": "conservador|base|optimista", "multiplo": <num>, '
        '"metrica_4y": <num por acción, misma divisa que el precio>, '
        '"razon": "<múltiplo justificado + crecimiento implícito + serie usada + divisa si no es la del precio + discrepancias de fuentes si las hay, 2-3 frases>"}]}'
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


def _validar_escenario(esc: Escenario, precio_actual: float | None,
                       categoria_bloque: str | None) -> None:
    """Aplica las guardias del bug BAM (5-jun-2026): dimensional + CAGR.
    Modifica esc.alertas y esc.bloqueado in-place."""
    # Guardia 1 — DIMENSIONAL: la métrica base debe ser POR ACCIÓN.
    # Si supera el 50% del precio actual, casi seguro es un agregado mal
    # etiquetado por la IA (ej. FRE total $5.63B en lugar de $3.54/acc).
    if precio_actual and precio_actual > 0:
        ratio = esc.metrica_base_4y / precio_actual
        if ratio > _RATIO_METRICA_PRECIO_MAX:
            esc.alertas.append(
                f"⚠️ La métrica base ({esc.metrica_base_4y:.2f}) supera el "
                f"{_RATIO_METRICA_PRECIO_MAX * 100:.0f}% del precio actual "
                f"({precio_actual:.2f}). Probable confusión entre VALOR TOTAL "
                f"(agregado, $B) y VALOR POR ACCIÓN. Verifica dividiéndolo "
                f"entre las acciones en circulación antes de aplicar."
            )
            esc.bloqueado = True

    # Guardia 2 — CAGR implícito. Bloqueo absoluto y alerta por bloque.
    if esc.cagr4_pct is not None:
        if esc.cagr4_pct > _UMBRAL_CAGR_BLOQUEO:
            esc.alertas.append(
                f"⚠️ CAGR implícito {esc.cagr4_pct * 100:.1f}% > "
                f"{_UMBRAL_CAGR_BLOQUEO * 100:.0f}%. Es muy difícil de defender "
                f"para 4 años; confirma explícitamente antes de aplicar."
            )
            esc.bloqueado = True
        else:
            umbral = _UMBRAL_CAGR_ALERTA.get(
                (categoria_bloque or "").lower(), _UMBRAL_CAGR_DEFAULT,
            )
            if esc.cagr4_pct > umbral:
                bloque_nombre = categoria_bloque or "sin clasificar"
                esc.alertas.append(
                    f"CAGR implícito {esc.cagr4_pct * 100:.1f}% supera el "
                    f"umbral del bloque «{bloque_nombre}» "
                    f"({umbral * 100:.0f}%). Revisa la coherencia de la tesis."
                )


def _desglose(esc: Escenario, tipo_val: str, metrica_actual: float | None,
              precio_actual: float | None) -> list[dict]:
    """Pasos del cálculo para mostrar en la UI ("Cómo se calcula")."""
    _, met_label = models.etiquetas_tipo_val(tipo_val)
    pasos: list[dict] = [
        {"etiqueta": "Método", "valor": tipo_val, "calc": ""},
    ]
    if metrica_actual is not None:
        pasos.append({
            "etiqueta": f"{met_label} hoy",
            "valor": f"{metrica_actual:.2f}",
            "calc": "(modelo actual de Cima)",
        })
    crecimiento = None
    if metrica_actual and metrica_actual > 0 and esc.metrica_base_4y > 0:
        crecimiento = (esc.metrica_base_4y / metrica_actual) ** (1 / 4) - 1
    if crecimiento is not None:
        pasos.append({
            "etiqueta": f"CAGR aplicado a {met_label}",
            "valor": f"{crecimiento * 100:.1f}%",
            "calc": "",
        })
    pasos.append({
        "etiqueta": f"{met_label} en 4Y",
        "valor": f"{esc.metrica_base_4y:.2f}",
        "calc": (
            f"{metrica_actual:.2f} × (1+{crecimiento:.3f})⁴"
            if crecimiento is not None and metrica_actual else ""
        ),
    })
    pasos.append({
        "etiqueta": "Múltiplo objetivo",
        "valor": f"{esc.multiplo:.1f}×",
        "calc": "",
    })
    pasos.append({
        "etiqueta": "Precio objetivo",
        "valor": f"{esc.precio_objetivo:.2f}",
        "calc": f"{esc.multiplo:.1f} × {esc.metrica_base_4y:.2f}",
    })
    if esc.cagr4_pct is not None and precio_actual:
        pasos.append({
            "etiqueta": "CAGR implícito (precio)",
            "valor": f"{esc.cagr4_pct * 100:.1f}%",
            "calc": f"({esc.precio_objetivo:.2f} / {precio_actual:.2f})^(1/4) − 1",
        })
    return pasos


def _categoria_bloque_de_isin(db: Session, cartera_id: str, isin: str
                              ) -> str | None:
    """Categoría base del bloque al que pertenece el ISIN (posición o
    watchlist). None si el ISIN no está clasificado todavía."""
    pos = db.execute(
        select(models.Posicion)
        .where(models.Posicion.cartera_id == cartera_id)
        .where(models.Posicion.isin == isin)
    ).scalars().first()
    bloque_id = pos.bloque_id if pos and pos.bloque_id else None
    if bloque_id is None:
        seg = db.execute(
            select(models.Seguimiento)
            .where(models.Seguimiento.cartera_id == cartera_id)
            .where(models.Seguimiento.isin == isin)
        ).scalars().first()
        bloque_id = seg.bloque_id if seg and seg.bloque_id else None
    if bloque_id is None:
        return None
    b = db.get(models.Bloque, bloque_id)
    return b.categoria_base if b else None


def _escenarios(data: dict, precio_actual: float | None,
                metrica_actual: float | None = None,
                tipo_val: str = "PER",
                categoria_bloque: str | None = None) -> list[Escenario]:
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
        esc = Escenario(
            nombre=str(it.get("nombre") or "—").strip().lower(),
            multiplo=mult, metrica_base_4y=eps, precio_objetivo=precio_obj,
            cagr4_pct=cagr, razon=str(it.get("razon") or "").strip(),
        )
        _validar_escenario(esc, precio_actual, categoria_bloque)
        esc.desglose = _desglose(esc, tipo_val, metrica_actual, precio_actual)
        out.append(esc)
    return out


def parse(texto: str, precio_actual: float | None,
          metrica_actual: float | None = None, tipo_val: str = "PER",
          categoria_bloque: str | None = None) -> list[Escenario]:
    s = (texto or "").strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    data: dict = {}
    if m:
        try:
            obj = json.loads(m.group(0), strict=False)
            data = obj if isinstance(obj, dict) else {}
        except (ValueError, TypeError):
            data = {}
    return _escenarios(data, precio_actual, metrica_actual, tipo_val, categoria_bloque)


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
    metrica_actual = _f(e.metrica_base_4y)
    anclas = _anclas(e)
    categoria_bloque = _categoria_bloque_de_isin(db, cartera_id, isin)
    # Liga los escenarios a la TESIS del one-pager si ya se generó (si no, la IA investiga).
    from app.services.one_pager import guardado as op_guardado
    op = op_guardado(db, cartera_id, isin)
    tesis = {"tesis": op.tesis, "riesgos": op.riesgos, "valoracion": op.valoracion} if op else None
    system, user = build_prompt(e.nombre, anclas, tipo_val, tesis)
    escenarios = parse(
        get_clasificador().investigar(system, user),
        precio_actual, metrica_actual, tipo_val, categoria_bloque,
    )
    creditos.registrar_uso_ia(db, cartera_id, "valoracion", 1)
    v = Valoracion(
        isin=isin, nombre=e.nombre, tipo_val=tipo_val, precio_actual=precio_actual,
        anclas=anclas, escenarios=escenarios, fecha=date.today().isoformat(),
        proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if getattr(settings.mode, "value", settings.mode) == "saas" else None),
    )
    _persistir(db, cartera_id, v)
    return v
