"""Cálculo fiscal de opciones — invoca `calcular_resumen_opciones` de Cuádrate.

Carga las filas `Opcion` de BD, las convierte al dict que el motor espera
(con fecha "DD/MM/YYYY") y devuelve el resumen por contrato + totales con la
clasificación DGT V2172-21 (normal / ejercida / mixta / long-abierta /
short-abierta / roll).

Filtro por ejercicio: por la fecha del trade (`fecha.year == ejercicio`).
`ejercicio=None` → acumulado (todas las opciones en BD). Es una aproximación
al criterio de Cuádrate (que corre año a año leyendo el CSV del año); la
casuística fina de diferimiento entre años queda como refinamiento futuro.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import _ensure_cuadrate_importable
from app.db import models


_MESES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_RE_VENC = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")


def _venc_to_date(venc: str) -> date | None:
    """Convierte un vencimiento '16JAN26' → date(2026, 1, 16). None si no parsea."""
    m = _RE_VENC.match((venc or "").strip().upper())
    if not m:
        return None
    dd, mmm, yy = m.groups()
    mes = _MESES.get(mmm)
    if not mes:
        return None
    try:
        return date(2000 + int(yy), mes, int(dd))
    except ValueError:
        return None


def _inyectar_expiraciones_vencidas(
    ops: list[dict[str, Any]], cutoff: date
) -> list[dict[str, Any]]:
    """Inyecta eventos de expiración sintéticos para contratos con posición
    neta abierta cuyo vencimiento ya pasó (< cutoff).

    Caso real: una opción larga (o corta) que expira sin valor no deja
    registro de cierre en DEGIRO/IBKR. El motor la ve sólo con su apertura
    → la clasifica "long/short abierta" para siempre. Aquí detectamos que
    el vencimiento ya pasó y añadimos una op `expirada=True` que cierra la
    posición neta, de modo que el motor la trate como expirada (la prima
    pagada/cobrada se realiza en el año del vencimiento).
    """
    # Agrupar por contrato (misma clave que el motor, sin broker para sumar
    # neto por contrato — el motor reagrupa con broker después).
    grupos: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"neto": Decimal("0"), "venc": "", "tipo_op": "?",
                 "subyacente": "", "strike": "", "broker": "", "fecha_open": None}
    )
    for op in ops:
        clave = (op["subyacente"], op["tipo_op"], str(op["strike"]),
                 op["vencimiento"], op["broker"])
        g = grupos[clave]
        g["venc"] = op["vencimiento"]
        g["tipo_op"] = op["tipo_op"]
        g["subyacente"] = op["subyacente"]
        g["strike"] = op["strike"]
        g["broker"] = op["broker"]
        # Neto firmado por dirección natural (convención: posición corta = +).
        # CADA op cuenta por su acción, incluidos los cierres reales (IBKR
        # `Code=Ep` deja un buy-to-close compra que aquí resta). Así una corta
        # vendida y luego cerrada da neto 0 → no se infiere expiración (el
        # cierre real ya está). Una larga comprada sin cierre da neto != 0.
        cant = Decimal(str(op["cantidad"]))
        g["neto"] += cant if op["accion"] == "venta" else -cant

    extra: list[dict[str, Any]] = []
    for clave, g in grupos.items():
        neto = g["neto"]
        if neto == 0:
            continue
        venc_date = _venc_to_date(g["venc"])
        if venc_date is None or venc_date >= cutoff:
            continue  # aún no ha vencido (o sin fecha) → sigue abierta de verdad
        # Inyectar expiración: cierra el neto. accion opuesta al signo.
        # neto>0 → era short neto → cierra con 'compra'; neto<0 → long → 'venta'.
        accion_cierre = "compra" if neto > 0 else "venta"
        extra.append({
            "fecha": venc_date.strftime("%d/%m/%Y"),
            "simbolo": f"{g['subyacente']} {g['tipo_op']}{g['strike']} {g['venc']}",
            "isin": "",
            "tipo_op": g["tipo_op"],
            "subyacente": g["subyacente"],
            "strike": g["strike"],
            "vencimiento": g["venc"],
            "accion": accion_cierre,
            "cantidad": abs(neto),
            "prima_unitaria": Decimal("0"),
            "importe_eur": Decimal("0"),
            "gastos_eur": Decimal("0"),
            "expirada": True,
            "ejercida": False,
            "broker": g["broker"],
            "_sintetica": True,
        })
    return ops + extra


def _opcion_a_dict_motor(o: models.Opcion, broker_alias: str) -> dict[str, Any]:
    """Convierte una fila Opcion al dict que ingiere calcular_resumen_opciones.
    La fecha debe ir como 'DD/MM/YYYY' (el motor la parsea con ese formato)."""
    return {
        "fecha": o.fecha.strftime("%d/%m/%Y"),
        "simbolo": o.simbolo,
        "isin": o.isin or "",
        "tipo_op": o.tipo_op,
        "subyacente": o.subyacente,
        "strike": o.strike,
        "vencimiento": o.vencimiento,
        "accion": o.accion,
        "cantidad": Decimal(str(o.cantidad)),
        "prima_unitaria": Decimal(str(o.prima_unitaria)),
        "importe_eur": Decimal(str(o.importe_eur)),
        "gastos_eur": Decimal(str(o.gastos_eur)),
        "expirada": bool(o.expirada),
        "ejercida": bool(o.ejercida),
        "broker": broker_alias,
    }


@dataclass
class OpcionesResultado:
    ejercicio: int                       # 0 = acumulado
    por_contrato: list[Any]              # lista de dicts del motor
    totales: dict[str, Any]
    n_opciones: int
    fecha_calculo: date


def _broker_alias(db: Session, broker_id: str | None) -> str:
    if broker_id is None:
        return "manual"
    b = db.get(models.Broker, broker_id)
    if b is None:
        return "manual"
    return (b.alias or b.broker_tipo).upper()


def calcular_opciones(
    db: Session, cartera_id: str, ejercicio: int | None
) -> OpcionesResultado:
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    # Cargamos TODAS las opciones (sin filtrar por año todavía): la atribución
    # fiscal de una opción es el año de su CIERRE (expiración/ejercicio/
    # buy-to-close), no el del trade de apertura. Una opción vendida en 2024
    # que expira en 2025 tributa en 2025. Filtrar por fecha del trade partiría
    # el contrato y la dejaría fuera del año correcto.
    q = (
        select(models.Opcion)
        .where(models.Opcion.cartera_id == cartera_id)
        .where(models.Opcion.estado == "confirmada")
        .order_by(models.Opcion.fecha)
    )
    opciones_db = list(db.execute(q).scalars())

    alias_cache: dict[str | None, str] = {}
    ops: list[dict[str, Any]] = []
    for o in opciones_db:
        if o.broker_id not in alias_cache:
            alias_cache[o.broker_id] = _broker_alias(db, o.broker_id)
        ops.append(_opcion_a_dict_motor(o, alias_cache[o.broker_id]))

    # Inferir expiraciones sin registro de cierre (opciones largas/cortas que
    # vencieron sin valor — DEGIRO/IBKR no emiten línea de cierre). Mejora
    # sobre el motor de Cuádrate, que las deja como "abiertas" indefinidamente.
    ops = _inyectar_expiraciones_vencidas(ops, cutoff=date.today())

    # Atribución por año de cierre: para un ejercicio concreto, conservamos
    # SÓLO los contratos cuyo evento de cierre (op más reciente del contrato)
    # cae en ese año, pero incluyendo TODAS sus patas (apertura en años
    # previos incluida) para que el motor calcule el P&L completo.
    if ejercicio is not None:
        ops = _filtrar_por_anio_cierre(ops, ejercicio)

    n_reales = sum(1 for op in ops if not op.get("_sintetica"))
    por_contrato, totales = g.calcular_resumen_opciones(ops)

    return OpcionesResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        por_contrato=por_contrato,
        totales=totales,
        n_opciones=n_reales,
        fecha_calculo=date.today(),
    )


def _clave_contrato(op: dict[str, Any]) -> tuple:
    return (op["subyacente"], op["tipo_op"], str(op["strike"]),
            op["vencimiento"], op["broker"])


def _fecha_dt(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _filtrar_por_anio_cierre(
    ops: list[dict[str, Any]], ejercicio: int
) -> list[dict[str, Any]]:
    """Filtra los contratos relevantes para `ejercicio`, distinguiendo:

    - **Contratos cerrados** (neto 0: vendido y cerrado, comprado y cerrado,
      o expiración inferida): se atribuyen a su **año de cierre** (op más
      reciente). Solo aparecen en ese ejercicio (P&L realizado, casilla 1626).

    - **Contratos abiertos** (neto != 0: posición viva, p.ej. una larga
      comprada con vencimiento futuro): son posiciones diferidas. Aparecen
      en el ejercicio desde su **año de apertura en adelante** mientras sigan
      abiertas (una LEAPS comprada en 2025 que vence en 2028 debe verse como
      "abierta" en 2025, 2026, 2027 y 2028 hasta que cierre).

    Incluye todas las patas del contrato para que el motor calcule bien.
    """
    info: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"neto": Decimal("0"), "fechas": []}
    )
    for op in ops:
        clave = _clave_contrato(op)
        d = _fecha_dt(op["fecha"])
        cant = Decimal(str(op["cantidad"]))
        g = info[clave]
        g["neto"] += cant if op["accion"] == "venta" else -cant
        if d is not None:
            g["fechas"].append(d)

    incluir: set[tuple] = set()
    for clave, g in info.items():
        if not g["fechas"]:
            continue
        if g["neto"] == 0:
            # Cerrado → solo en su año de cierre
            if max(g["fechas"]).year == ejercicio:
                incluir.add(clave)
        else:
            # Abierto → desde el año de apertura en adelante
            if min(g["fechas"]).year <= ejercicio:
                incluir.add(clave)

    return [op for op in ops if _clave_contrato(op) in incluir]


# ── Opciones ABIERTAS (vivas) para la vista de Cartera ──────────────────────

# Códigos MEFF de subyacente → ticker resoluble en su divisa real. Sin esto,
# 'SAN' resuelve al ADR de NYSE en USD en vez de SAN.MC en EUR (la del strike).
# Ojo: Enagás MEFF=ENA → Yahoo ENG.MC; Redeia MEFF=REE → RED.MC.
_SUBYACENTE_MAP = {
    "SAN": "SAN.MC", "TEF": "TEF.MC", "REE": "RED.MC", "ENA": "ENG.MC",
    "IBE": "IBE.MC", "REP": "REP.MC", "SAB": "SAB.MC", "ACS": "ACS.MC",
    "BKT": "BKT.MC", "ASLM": "ASML.AS", "NESN": "NESN.SW",
}
# Acciones por contrato (multiplicador estándar de opciones sobre acciones).
_MULTIPLICADOR = Decimal("100")


@dataclass
class OpcionAbierta:
    subyacente: str
    tipo_op: str            # 'C' / 'P'
    strike: str
    vencimiento: str
    contratos: int          # nº de contratos netos abiertos (valor absoluto)
    es_corta: bool          # True = vendida (cobras prima); False = comprada
    prima_neta_eur: Decimal # cobradas − pagadas (corta: + ingreso; larga: − coste)
    dias_a_vencer: int | None
    moneyness: str | None   # 'ITM' / 'OTM' / None (sin precio del subyacente)
    precio_subyacente: Decimal | None = None   # precio actual del subyacente
    divisa_subyacente: str | None = None
    gp_estimada_eur: Decimal | None = None     # estimación por VALOR INTRÍNSECO
    gp_estimada_pct: Decimal | None = None      # sobre la prima neta


def opciones_abiertas(db: Session, cartera_id: str) -> list[OpcionAbierta]:
    """Contratos de opción VIVOS hoy (posición neta ≠ 0 y vencimiento futuro).
    Excluye las vencidas-no-cerradas (worthless-expiry de DEGIRO).
    `n_net_abiertos > 0` = neto vendido (corta).

    Moneyness y G/P salen del precio del SUBYACENTE (no hay feed del precio de la
    opción → no es mark-to-market). La G/P es ESTIMADA por valor intrínseco (sin
    valor temporal): para una corta, prima_neta − intrínseco; para una larga,
    prima_neta + intrínseco. Multiplicador 100 acciones/contrato."""
    from app.services.precios import _precio_y_divisa, _fx_eur, _leer_cache

    op = calcular_opciones(db, cartera_id, None)
    hoy = date.today()
    cache_px: dict[str, tuple[Decimal, str] | None] = {}
    fx_cache = _leer_cache()
    out: list[OpcionAbierta] = []
    for c in op.por_contrato:
        # `n_net_abiertos` viene clipado por Cuádrate (`max(0, vend - comp)`) →
        # cualquier opción COMPRADA neta queda como 0 y desaparecería de
        # "abiertas". Recalculamos sin clipar: positivo = corto (vendido neto),
        # negativo = largo (comprado neto). 0 = cerrado.
        vendidos = int(Decimal(str(c.get("contratos_vendidos") or 0)))
        comprados = int(Decimal(str(c.get("contratos_comprados") or 0)))
        n = vendidos - comprados
        if n == 0:
            continue
        venc = str(c.get("vencimiento", ""))
        fvenc = _venc_to_date(venc)
        dias = (fvenc - hoy).days if fvenc else None
        if dias is None or dias < 0:
            continue   # vencida (o sin fecha) → no es una posición viva
        sub = str(c.get("subyacente", ""))
        es_corta = n > 0
        contratos = abs(n)
        try:
            strike = Decimal(str(c.get("strike", "")).replace(",", "."))
        except Exception:
            strike = None
        # Precio del subyacente en su divisa real (mapea código MEFF si aplica).
        if sub not in cache_px:
            pv = _precio_y_divisa(_SUBYACENTE_MAP.get(sub, sub))
            cache_px[sub] = (Decimal(str(pv[0])), str(pv[1])) if pv else None
        pdiv = cache_px[sub]
        precio_sub = pdiv[0] if pdiv else None
        divisa_sub = pdiv[1] if pdiv else None

        tipo = str(c.get("tipo_op", "")).upper()
        moneyness = None
        intrinseco_nativo = None
        if precio_sub is not None and strike is not None:
            if tipo.startswith("C"):
                moneyness = "ITM" if precio_sub > strike else "OTM"
                intrinseco_nativo = max(Decimal("0"), precio_sub - strike)
            elif tipo.startswith("P"):
                moneyness = "ITM" if precio_sub < strike else "OTM"
                intrinseco_nativo = max(Decimal("0"), strike - precio_sub)

        prima_neta = (Decimal(str(c.get("primas_cobradas") or 0))
                      - Decimal(str(c.get("primas_pagadas") or 0)))

        gp_est = None
        gp_pct = None
        if intrinseco_nativo is not None:
            # Sin FX disponible NO se asume paridad (auditoría D7: un
            # subyacente USD con caché fría salía como EUR ±8-10% sin aviso).
            fx = _fx_eur(divisa_sub or "EUR", fx_cache)
            if fx is None:
                return {}
            intrinseco_eur = intrinseco_nativo * _MULTIPLICADOR * Decimal(contratos) * fx
            # Corta: recompras al intrínseco → prima − intrínseco. Larga: vendes
            # al intrínseco → prima_neta (negativa) + intrínseco.
            gp_est = (prima_neta - intrinseco_eur) if es_corta else (prima_neta + intrinseco_eur)
            gp_est = gp_est.quantize(Decimal("0.01"))
            if prima_neta != 0:
                gp_pct = (gp_est / abs(prima_neta)).quantize(Decimal("0.0001"))

        out.append(OpcionAbierta(
            subyacente=sub, tipo_op=str(c.get("tipo_op", "")),
            strike=str(c.get("strike", "")), vencimiento=venc,
            contratos=contratos, es_corta=es_corta,
            prima_neta_eur=prima_neta.quantize(Decimal("0.01")),
            dias_a_vencer=dias, moneyness=moneyness,
            precio_subyacente=(precio_sub.quantize(Decimal("0.0001")) if precio_sub is not None else None),
            divisa_subyacente=divisa_sub,
            gp_estimada_eur=gp_est, gp_estimada_pct=gp_pct,
        ))
    out.sort(key=lambda o: (o.dias_a_vencer if o.dias_a_vencer is not None else 99999))
    return out
