"""Asesor financiero IA conversacional: ensambla el estado de la cartera +
estrategia + plan + doctrina y responde vía `completar` (rápido, sin web)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.adapters.ia.asesor import system_asesor
from app.config import settings
from app.db import models
from app.services import creditos

_MAX_HIST = 20          # turnos recientes que se envían al modelo
_DETALLE_TOP = 25       # nº de posiciones con detalle completo (CAGR/yield/objetivo/decisión)
_TIPOS_ACCION = {"crear_paso", "ajustar_estimacion"}
_PRIORIDADES = {"CRITICA", "ALTA", "MEDIA", "BAJA"}

# Palabras clave que sugieren que la pregunta NECESITA datos en tiempo real /
# noticias / contexto de mercado → enrutar a `investigar` (con búsqueda web)
# en vez de `completar` (solo el contexto guardado).
_KW_WEB = (
    "hoy", "ahora", "actualmente", "última hora", "ultima hora",
    "noticias", "noticia", "última hora",
    "por qué sube", "por qué baja", "por qué cae", "por qué está",
    "está subiendo", "está bajando", "está cayendo", "está al alza", "está a la baja",
    "qué pasa con", "qué le pasa a", "qué pasó con", "que pasa con", "que le pasa a",
    "cotización", "cotiza", "precio actual", "precio de hoy",
    "earnings hoy", "resultados de hoy", "guidance", "ha publicado",
    "presentó resultados", "presento resultados",
)


def _requiere_web(texto: str) -> bool:
    """Heurística: ¿la pregunta pide datos en vivo / noticias del día?"""
    t = (texto or "").lower()
    return any(k in t for k in _KW_WEB)


@dataclass
class Accion:
    tipo: str
    isin: str
    descripcion: str        # texto para la tarjeta
    params: dict            # lo que el frontend pasa al endpoint conocido


def _validar_accion(a: dict) -> Accion | None:
    if not isinstance(a, dict):
        return None
    tipo = str(a.get("tipo", "")).strip()
    isin = str(a.get("isin", "")).strip()
    if tipo not in _TIPOS_ACCION or not isin:
        return None
    if tipo == "crear_paso":
        dec = str(a.get("decision", "")).strip().upper()
        if dec not in models.DECISIONES_PLAN:
            return None
        prio = str(a.get("prioridad", "MEDIA")).strip().upper()
        prio = prio if prio in _PRIORIDADES else "MEDIA"
        razon = str(a.get("razon") or "").strip() or None
        cap = a.get("capital_objetivo_eur")
        params = {"decision": dec, "prioridad": prio, "capital_objetivo_eur": cap, "razon": razon}
        desc = f"Crear paso {dec} · {isin} (prioridad {prio})" + (f" — {razon}" if razon else "")
        return Accion("crear_paso", isin, desc, params)
    # ajustar_estimacion: puede tocar el tipo de múltiplo, el múltiplo+métrica y/o el dividendo.
    # Todos los campos son opcionales por separado, pero debe cambiar AL MENOS uno.
    razon = str(a.get("razon") or "").strip() or None
    params: dict = {"razon": razon}
    partes: list[str] = []

    tv = str(a.get("tipo_val", "")).strip().upper()
    if tv:
        if tv not in models.TIPOS_VAL:
            return None
        params["tipo_val"] = tv

    mult = _float_o_none(a.get("multiplo_objetivo"))
    met = _float_o_none(a.get("metrica_base_4y"))
    if (mult is None) != (met is None):
        return None                      # múltiplo y métrica van juntos (precio = N×O)
    if mult is not None and met is not None:
        params["multiplo_objetivo"] = mult
        params["metrica_base_4y"] = met
        mult_label, met_label = models.etiquetas_tipo_val(tv or None)
        partes.append(f"{mult_label} {mult:g} × {met_label.split(' ')[0]} {met:g}")
    elif tv:
        partes.append(f"método → {models.etiquetas_tipo_val(tv)[0]}")

    div = _float_o_none(a.get("dividendo_share"))
    if div is not None:
        params["dividendo_share"] = div
        partes.append(f"dividendo/acción {div:g}")

    if not partes:
        return None
    desc = f"Ajustar estimación de {isin}: " + " · ".join(partes) + (f" — {razon}" if razon else "")
    return Accion("ajustar_estimacion", isin, desc, params)


def _float_o_none(v) -> float | None:  # type: ignore[no-untyped-def]
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_acciones(texto: str) -> tuple[str, list[Accion]]:
    """Extrae el bloque JSON de acciones (si lo hay), lo quita del texto mostrado y
    valida cada acción contra la whitelist. Devuelve (texto_limpio, acciones)."""
    s = (texto or "").strip()
    if '"acciones"' not in s:
        return s, []
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    raw = next((f for f in fenced if '"acciones"' in f), None)
    if raw is None:                                  # sin fence: el último objeto {...}
        m = re.search(r"\{.*\}", s, re.DOTALL)
        raw = m.group(0) if m else None
    if raw is None:
        return s, []
    try:
        data = json.loads(raw, strict=False)
    except (ValueError, TypeError):
        return s, []
    acciones = [acc for a in (data.get("acciones") or []) if (acc := _validar_accion(a))]
    # limpiar el texto: quita el fence (o el objeto crudo) con el bloque de acciones
    limpio = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", s, flags=re.DOTALL).strip()
    if limpio == s:
        limpio = s.replace(raw, "").strip()
    return limpio, acciones


def _eur(v) -> str:  # type: ignore[no-untyped-def]
    return "—" if v is None else f"{float(v):,.0f} €".replace(",", ".")


def _num(v) -> str:  # type: ignore[no-untyped-def]
    return "—" if v is None else f"{float(v):.2f}"


def _pct(v, dp=1) -> str:  # type: ignore[no-untyped-def]
    return "—" if v is None else f"{float(v) * 100:.{dp}f}%"


def _contexto(db: Session, cartera_id: str) -> str:
    """Snapshot compacto de cartera + estrategia + plan + régimen para el prompt."""
    from app.services.bloques import calcular_distribucion
    from app.services.dashboard import calcular_dashboard
    from app.services.estimaciones import calcular_estimaciones
    from app.services.onboarding import plan_firmado_actual
    from app.services.plan import listar_pasos, posiciones_con_plan
    from app.services.regimen import estado_regimen

    d = calcular_dashboard(db, cartera_id)
    est = {e.isin: e for e in calcular_estimaciones(db, cartera_id)}
    plan = {p.isin: p for p in posiciones_con_plan(db, cartera_id)}
    dist = calcular_distribucion(db, cartera_id)
    reg = estado_regimen(db, cartera_id)
    pf = plan_firmado_actual(db, cartera_id)

    L: list[str] = []
    L.append(f"Capital invertido: {_eur(d.capital_mercado_eur)} · G/P latente: "
             f"{_eur(d.gp_no_realizada_eur)} ({_pct(d.gp_no_realizada_pct)}) · liquidez {_eur(d.liquidez_eur)}")
    anios = f"{float(d.anios_if):.1f} años" if d.anios_if is not None else "no alcanzable con estos supuestos"
    L.append(f"Progreso IF: {_pct(d.progreso_if_pct, 0)} · estimación {anios} · yield {_pct(d.yield_actual_pct, 2)} "
             f"· CAGR potencial {_pct(d.cagr_anual_pct, 1)}")
    if pf and pf.perfil_json:
        try:
            p = json.loads(pf.perfil_json)
            L.append(f"Perfil firmado: objetivo {p.get('objetivo_if_eur')} € · horizonte {p.get('horizonte_anios')}a "
                     f"· fase {p.get('fase')} · tolerancia {p.get('tolerancia')}")
        except (ValueError, TypeError):
            pass
    L.append(f"Régimen macro: {reg.regimen} (tramos {reg.tramo_min}-{reg.tramo_max} € cada {reg.espaciado})")
    objs = [f"{b.nombre} {_pct(b.peso_actual, 0)}/{_pct(b.peso_objetivo, 0) if b.peso_objetivo is not None else '—'}"
            for b in dist.bloques if b.valor_eur > 0]
    if objs:
        L.append("Bloques (peso actual/objetivo): " + " · ".join(objs))

    # TODAS las posiciones — el asesor DEBE conocer todo lo que tienes (no decir
    # "no está en tu cartera" porque no lo veía). Top con detalle completo; el
    # resto en una línea compacta por posición (nombre + isin + bloque + valor + peso + decisión).
    todos = d.posiciones_peso
    top = todos[:_DETALLE_TOP]
    resto = todos[_DETALLE_TOP:]
    L.append("\nPOSICIONES — top por valor (valor · CAGR4+Div · yield · precio→objetivo · "
             "múltiplo/métrica · anclas: EPS consenso 4Y / PER histórico · decisión):")
    for p in top:
        e = est.get(p.isin)
        pl = plan.get(p.isin)
        cagr = _pct(e.cagr4_div_pct, 1) if e else "—"
        yld = _pct(e.div_yield_pct, 1) if e else "—"
        precio = f"{_num(e.precio_actual)}→{_num(e.precio_objetivo)}" if e else "—"
        modelo = f"{_num(e.multiplo_objetivo)}×{_num(e.metrica_base_4y)}" if e else "—"
        anclas = (f"consenso {_num(e.eps_consenso_4y)} / PER hist {_num(e.per_hist_medio)}"
                  if e else "—")
        dec = pl.decision if pl else "MANTENER"
        L.append(f"  - {p.nombre} ({p.isin}) [{p.categoria_base or '-'}] {_eur(p.valor_eur)} "
                 f"({_pct(p.peso, 0)}) · {cagr} · yield {yld} · {precio} · modelo {modelo} "
                 f"· {anclas} · {dec}")
    if resto:
        L.append("\nRESTO de posiciones (nombre · isin · bloque · valor · peso · CAGR4+Div · decisión):")
        for p in resto:
            e = est.get(p.isin)
            pl = plan.get(p.isin)
            cagr = _pct(e.cagr4_div_pct, 1) if e else "—"
            dec = pl.decision if pl else "MANTENER"
            L.append(f"  - {p.nombre} ({p.isin}) [{p.categoria_base or '-'}] "
                     f"{_eur(p.valor_eur)} ({_pct(p.peso, 0)}) · {cagr} · {dec}")

    from app.services.vigilancia import evaluar as evaluar_vigilancia
    alertas, desde = evaluar_vigilancia(db, cartera_id)
    if alertas:
        L.append(f"\nALERTAS DE VIGILANCIA (movimientos desde {desde or 'la última vez'}):")
        for a in alertas[:10]:
            L.append(f"  - {a.nombre}: {_pct(a.cambio_pct, 1)} [{a.nivel}]")

    from app.services.fiscal_contexto import calcular_contexto
    try:
        fc = calcular_contexto(db, cartera_id)
        L.append("\nSITUACIÓN FISCAL (tenla en cuenta antes de recomendar vender/rotar):")
        L.append("  " + fc.resumen)
    except Exception:  # noqa: BLE001 — sin histórico fiscal aún → omitir, no romper el chat
        pass

    pasos = listar_pasos(db, cartera_id, "PENDIENTE")
    if pasos:
        L.append("\nPRÓXIMOS PASOS (pendientes):")
        for s in pasos[:10]:
            L.append(f"  - {s.decision} {s.isin} (prioridad {s.prioridad})"
                     + (f" — {s.razon}" if s.razon else ""))
    return "\n".join(L)


def historial(db: Session, cartera_id: str) -> list[models.MensajeAsesor]:
    return list(db.execute(
        select(models.MensajeAsesor)
        .where(models.MensajeAsesor.cartera_id == cartera_id)
        .order_by(models.MensajeAsesor.created_at)
    ).scalars())


def responder(
    db: Session, cartera_id: str, texto: str, por_voz: bool = False,
) -> tuple[models.MensajeAsesor, list[Accion]]:
    """Persiste el mensaje del usuario, llama al asesor con el contexto y el historial,
    persiste el texto (sin el bloque de acciones) y devuelve (respuesta, acciones).
    Las acciones (whitelisteadas) solo se extraen en modo owner; son efímeras."""
    db.add(models.MensajeAsesor(cartera_id=cartera_id, rol="user", contenido=texto))
    db.commit()

    mode = getattr(settings.mode, "value", settings.mode)
    web = _requiere_web(texto)
    system = (system_asesor(mode, con_web=web, por_voz=por_voz)
              + "\n\nESTADO ACTUAL DE LA CARTERA:\n" + _contexto(db, cartera_id))
    hist = historial(db, cartera_id)[-_MAX_HIST:]
    rendered = "\n".join(("Usuario" if m.rol == "user" else "Asesor") + f": {m.contenido}" for m in hist)
    user = rendered + "\n\nResponde como Asesor al último mensaje del Usuario."

    ia = get_clasificador()
    if web:
        # La pregunta exige datos en vivo o noticias → IA con búsqueda web (lenta
        # pero precisa). El timeout es el de web (10 min, ya configurado).
        respuesta = ia.investigar(system, user)
    else:
        respuesta = ia.completar(system, user, timeout_s=settings.ia_chat_timeout_s)
    creditos.registrar_uso_ia(db, cartera_id, "asesor", 1)
    if mode == "owner":
        contenido, acciones = parse_acciones(respuesta or "")
    else:
        contenido, acciones = (respuesta or "").strip(), []
    msg = models.MensajeAsesor(cartera_id=cartera_id, rol="assistant", contenido=contenido)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg, acciones


def limpiar(db: Session, cartera_id: str) -> None:
    db.execute(delete(models.MensajeAsesor).where(models.MensajeAsesor.cartera_id == cartera_id))
    db.commit()
