"""Hoja de ruta (1.7): traduce la estrategia firmada (objetivos por bloque) en
pasos concretos del plan. HÍBRIDO: el déficit € por bloque es DETERMINISTA
(`calcular_distribucion`); la IA solo ORDENA, razona y aplica anti-churn/15-15
sobre instrumentos que el usuario ya tiene o sigue. Se dispara tras firmar (job
en segundo plano) y el usuario aprueba cada paso (→ `crear_paso`).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, UTC
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.ia import get_clasificador
from app.config import settings
from app.db import models
from app.services import creditos

_TIPO = "hoja_ruta"
_MAX_PASOS = 12
_PRIORIDADES = {"CRITICA", "ALTA", "MEDIA", "BAJA"}
_DISCLAIMER = ("Hoja de ruta generada por IA, orientativa y NO asesoramiento. "
               "Tú apruebas cada paso; ajusta el importe y el ritmo a tu criterio.")


@dataclass
class GapBloque:
    categoria_base: str
    nombre: str
    peso_actual: float
    peso_objetivo: float
    deficit_eur: float          # >0 = por debajo del objetivo (falta invertir); <0 = exceso
    n_posiciones: int


@dataclass
class PasoPropuesto:
    isin: str
    nombre: str
    decision: str
    prioridad: str
    capital_objetivo_eur: float | None
    razon: str
    en_cartera: bool            # False = candidato del watchlist


@dataclass
class HojaRuta:
    capital_eur: float
    liquidez_eur: float
    deficit: list = field(default_factory=list)     # list[GapBloque]
    pasos: list = field(default_factory=list)       # list[PasoPropuesto]
    huecos: list = field(default_factory=list)      # bloques con déficit y sin instrumento nombrable
    resumen: str = ""
    fecha: str = ""
    proveedor: str = ""
    disclaimer: str | None = None


def analizar_deficit(db: Session, cartera_id: str) -> tuple[list[GapBloque], Decimal]:
    """Déficit € por bloque = (objetivo − actual) × capital EN ESTRATEGIA.
    DETERMINISTA.

    La base es el capital dentro de la estrategia (excluye Colchón y bloques
    fuera de estrategia), igual que hace plan.hueco_asignacion. Con la base
    total (auditoría Cima 2026-06-11, A7) los objetivos —que suman 100% sobre
    la estrategia— generaban Σ déficits ≈ tamaño del colchón: pasos COMPRAR
    sin financiación posible salvo vender el colchón (regla absoluta: jamás)."""
    from app.services.bloques import calcular_distribucion
    d = calcular_distribucion(db, cartera_id)
    total_estrategia = sum(
        (b.valor_eur for b in d.bloques if b.en_estrategia), Decimal("0")
    )
    gaps: list[GapBloque] = []
    for b in d.bloques:
        if b.peso_objetivo is None or not b.en_estrategia:
            continue
        peso_actual = (
            b.valor_eur / total_estrategia if total_estrategia > 0 else Decimal("0")
        )
        deficit = (b.peso_objetivo - peso_actual) * total_estrategia
        gaps.append(GapBloque(
            categoria_base=b.categoria_base, nombre=b.nombre,
            peso_actual=float(peso_actual), peso_objetivo=float(b.peso_objetivo),
            deficit_eur=float(deficit), n_posiciones=b.n_posiciones,
        ))
    gaps.sort(key=lambda g: g.deficit_eur, reverse=True)
    return gaps, total_estrategia


def _contexto(db: Session, cartera_id: str):  # type: ignore[no-untyped-def]
    """Arma el prompt + el conjunto de ISINs válidos (posiciones ∪ watchlist)."""
    from app.services.dashboard import calcular_dashboard
    from app.services.estimaciones import calcular_estimaciones
    from app.services.onboarding import plan_firmado_actual
    from app.services.regimen import estado_regimen

    gaps, total = analizar_deficit(db, cartera_id)
    d = calcular_dashboard(db, cartera_id)
    est = {e.isin: e for e in calcular_estimaciones(db, cartera_id)}
    reg = estado_regimen(db, cartera_id)

    nombres: dict[str, str] = {}
    en_cartera: set[str] = set()
    isines: set[str] = set()

    fase = "acumulacion"
    pf = plan_firmado_actual(db, cartera_id)
    if pf and pf.perfil_json:
        try:
            fase = json.loads(pf.perfil_json).get("fase") or fase
        except (ValueError, TypeError):
            pass

    L = [
        f"Capital invertido: {float(total):.0f} € · liquidez disponible: {float(d.liquidez_eur):.0f} €",
        f"Fase: {fase} · Régimen macro: {reg.regimen} → tramo de compra "
        f"{reg.tramo_min}-{reg.tramo_max} € cada {reg.espaciado}",
        "\nDÉFICIT POR BLOQUE (objetivo − actual; >0 = falta invertir):",
    ]
    for g in gaps:
        sentido = "falta" if g.deficit_eur > 0 else "exceso"
        L.append(f"  - {g.nombre} [{g.categoria_base}]: actual {g.peso_actual * 100:.0f}% / objetivo "
                 f"{g.peso_objetivo * 100:.0f}% → {sentido} {abs(g.deficit_eur):.0f} € "
                 f"· {g.n_posiciones} posición(es)")

    L.append("\nTUS POSICIONES (nombre (isin) [bloque] valor · CAGR4+Div · peso):")
    for p in d.posiciones_peso:
        e = est.get(p.isin)
        cagr = f"{float(e.cagr4_div_pct) * 100:.0f}%" if e and e.cagr4_div_pct is not None else "—"
        nombres[p.isin] = p.nombre
        en_cartera.add(p.isin)
        isines.add(p.isin)
        L.append(f"  - {p.nombre} ({p.isin}) [{p.categoria_base or '-'}] {float(p.valor_eur):.0f} € "
                 f"· {cagr} · {float(p.peso) * 100:.0f}%")

    segs = db.execute(select(models.Seguimiento)
                      .where(models.Seguimiento.cartera_id == cartera_id)).scalars().all()
    if segs:
        L.append("\nWATCHLIST (candidatos a compra; nombre (isin)):")
        for s in segs:
            nombres.setdefault(s.isin, s.nombre or s.ticker or s.isin)
            isines.add(s.isin)
            L.append(f"  - {s.nombre or s.ticker or s.isin} ({s.isin})")

    try:
        from app.services.fiscal_contexto import calcular_contexto
        fc = calcular_contexto(db, cartera_id)
        L.append("\nSITUACIÓN FISCAL (condiciona los pasos VENDER/RECORTAR):")
        L.append("  " + fc.resumen)
    except Exception:  # noqa: BLE001 — sin histórico fiscal → omitir
        pass

    return gaps, total, float(d.liquidez_eur), "\n".join(L), nombres, en_cartera, isines


def build_prompt(contexto: str) -> tuple[str, str]:
    system = (
        "Eres el Analista de Cartera del usuario (estrategia Wealth Guardian, objetivo: Independencia "
        "Financiera). Te doy el DÉFICIT por bloque (YA calculado) y sus posiciones/watchlist. Propón una "
        "HOJA DE RUTA: una secuencia ORDENADA y priorizada de pasos concretos para cerrar el déficit.\n"
        "REGLAS:\n"
        "  - Respeta la FASE: en acumulación prioriza Compounders/Dividend Growth y evita rentas altas.\n"
        "  - El importe de cada compra debe caber en el TRAMO del régimen y en la liquidez disponible.\n"
        "  - ANTI-CHURN: no propongas vender/rotar algo con CAGR4+Div > 10% por una diferencia < 2% con "
        "la alternativa.\n"
        "  - 15/15: en el bloque estable, si CAGR4+Div ≥ 15% es compra prioritaria.\n"
        "  - Cada paso debe ser sobre un ISIN DE LA LISTA (posición o watchlist). NO inventes ISINs.\n"
        "  - Si un bloque tiene déficit pero NO hay instrumento en la lista para cubrirlo, NO crees un "
        "paso: dilo en 'resumen' (hay que elegir un valor en la watchlist/análisis).\n"
        "Decisiones válidas: COMPRAR, REFORZAR, RECORTAR, VENDER, MONITORIZAR, ESPERAR. "
        "Prioridad: CRITICA|ALTA|MEDIA|BAJA.\n"
        "Responde EXCLUSIVAMENTE con JSON:\n"
        '{"resumen":"<2-4 frases: la estrategia de la ruta y los huecos sin instrumento>",'
        '"pasos":[{"isin":"<de la lista>","decision":"COMPRAR|REFORZAR|...","prioridad":"ALTA|...",'
        '"capital_objetivo_eur":<num o null>,"razon":"<por qué, anclado en déficit/régimen/CAGR>"}]}'
    )
    return system, contexto


def _parse_json(texto: str) -> dict:
    m = re.search(r"\{.*\}", (texto or "").strip(), re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0), strict=False)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _validar_paso(p: dict, nombres: dict, isines: set[str], en_cartera: set[str]) -> PasoPropuesto | None:
    if not isinstance(p, dict):
        return None
    isin = str(p.get("isin", "")).strip()
    if isin not in isines:                       # no inventar ISINs fuera de cartera/watchlist
        return None
    dec = str(p.get("decision", "")).strip().upper()
    if dec not in models.DECISIONES_PLAN:
        return None
    prio = str(p.get("prioridad", "MEDIA")).strip().upper()
    prio = prio if prio in _PRIORIDADES else "MEDIA"
    try:
        cap = float(p["capital_objetivo_eur"]) if p.get("capital_objetivo_eur") is not None else None
    except (TypeError, ValueError):
        cap = None
    return PasoPropuesto(
        isin=isin, nombre=nombres.get(isin, isin), decision=dec, prioridad=prio,
        capital_objetivo_eur=cap, razon=str(p.get("razon") or "").strip(),
        en_cartera=isin in en_cartera,
    )


def guardado(db: Session, cartera_id: str) -> HojaRuta | None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == "")
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    if row is None:
        return None
    try:
        d = json.loads(row.payload_json)
        d["deficit"] = [GapBloque(**g) for g in d.get("deficit", [])]
        d["pasos"] = [PasoPropuesto(**p) for p in d.get("pasos", [])]
        return HojaRuta(**d)
    except (ValueError, TypeError):
        return None


def _persistir(db: Session, cartera_id: str, hr: HojaRuta) -> None:
    row = db.execute(
        select(models.AnalisisGuardado)
        .where(models.AnalisisGuardado.cartera_id == cartera_id)
        .where(models.AnalisisGuardado.isin == "")
        .where(models.AnalisisGuardado.tipo == _TIPO)
    ).scalars().first()
    payload = json.dumps(asdict(hr), ensure_ascii=False)
    if row is None:
        db.add(models.AnalisisGuardado(cartera_id=cartera_id, isin="",
                                       tipo=_TIPO, payload_json=payload))
    else:
        row.payload_json = payload
        row.created_at = datetime.now(UTC)
    db.commit()


def proponer(db: Session, cartera_id: str, isin: str = "") -> HojaRuta:
    """Déficit determinista + IA que ordena/razona los pasos (sin web). Persiste.
    `isin` se ignora (nivel cartera) — la firma coincide con el framework de jobs."""
    gaps, total, liquidez, ctx, nombres, en_cartera, isines = _contexto(db, cartera_id)
    system, user = build_prompt(ctx)
    texto = get_clasificador().completar(system, user, timeout_s=settings.ia_chat_timeout_s)
    creditos.registrar_uso_ia(db, cartera_id, "hoja_ruta", 1)
    data = _parse_json(texto)

    pasos: list[PasoPropuesto] = []
    for p in (data.get("pasos") or [])[:_MAX_PASOS]:
        pp = _validar_paso(p, nombres, isines, en_cartera)
        if pp:
            pasos.append(pp)
    huecos = [g.nombre for g in gaps if g.deficit_eur > 0 and g.n_posiciones == 0]

    hr = HojaRuta(
        capital_eur=float(total), liquidez_eur=liquidez,
        deficit=gaps, pasos=pasos, huecos=huecos,
        resumen=str(data.get("resumen") or "").strip(),
        fecha=date.today().isoformat(), proveedor=settings.ia_provider,
        disclaimer=(_DISCLAIMER if getattr(settings.mode, "value", settings.mode) == "saas" else None),
    )
    _persistir(db, cartera_id, hr)
    return hr
