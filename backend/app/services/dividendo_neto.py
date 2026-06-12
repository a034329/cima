"""Tipo efectivo del dividendo para la métrica CAGR4+Div NETA.

Decisión de doctrina (Angel, 2026-06-11): la componente Div de la métrica
maestra usa el dividendo neto de lo que se paga DE VERDAD cada año:

    tipo_efectivo = tipo_ahorro (suelo, 19% configurable)
                  + exceso de la retención de ORIGEN sobre el tope del CDI

La retención española (0591) y la retención de origen hasta el tope del
convenio (0588) son CRÉDITOS en la propia declaración — no se descuentan
otra vez. El exceso (DE 26,375%→15%, CH 35%→15%, BE 30%→15%…) solo se
recupera reclamando al fisco extranjero (funcionalidad futura): mientras
tanto es coste real y resta retorno.

Ejemplos (suelo 19%): US/UK/ES → 19% · DE → 30,4% · CH → 39% · BE → 34%.

Tablas: topes CDI (`DTA_SOURCE_MAX`) y retenciones estatutarias reales
(`TR_SOURCE_WHT_RATE`) del vendor de Cuádrate — verificadas contra BOE/
fuentes primarias en la auditoría 2026-06-11 (incl. CDI Japón 5%). Para
países sin retención estatutaria conocida se asume retención == tope CDI
(exceso 0): sin dato no se inventa coste.

La componente de CRECIMIENTO del dividendo (g_div) refleja el check "yield
a medio plazo" del protocolo de rotaciones: un 4% creciendo al 15% supera
en el año 4 a un 12% plano.

    factor_horizonte(g) = media[(1+g)^t, t=1..4]
    Div_horizonte = yield_actual × (1 − tipo_efectivo) × factor_horizonte
"""
from __future__ import annotations

from decimal import Decimal

from app.config import settings

_CERO = Decimal("0")


def _tablas_vendor() -> tuple[dict, dict]:
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    return g.DTA_SOURCE_MAX, g.TR_SOURCE_WHT_RATE


def exceso_no_recuperable_pct(pais: str | None) -> Decimal:
    """Puntos de retención de origen por ENCIMA del tope CDI (fracción).
    0 si el país no retiene, si la estatutaria ≤ tope, o si no hay dato."""
    if not pais:
        return _CERO
    topes, estatutarias = _tablas_vendor()
    est = estatutarias.get(pais.upper())
    if est is None:
        return _CERO   # sin dato de estatutaria → no inventar coste
    tope = topes.get(pais.upper(), est)
    exceso = Decimal(str(est)) - Decimal(str(tope))
    return exceso if exceso > 0 else _CERO


def tipo_efectivo_dividendo(pais: str | None) -> Decimal:
    """Fracción del dividendo bruto que se pierde en impuestos no
    acreditables: suelo del ahorro + exceso de origen sobre el tope CDI."""
    suelo = Decimal(str(settings.tipo_ahorro_dividendo))
    return suelo + exceso_no_recuperable_pct(pais)


def factor_horizonte_div(g_div: Decimal | None, anios: int = 4) -> Decimal:
    """Media de (1+g)^t para t=1..anios — el multiplicador del yield actual
    que representa el yield medio cobrado durante el horizonte."""
    if g_div is None or g_div == 0:
        return Decimal("1")
    total = _CERO
    base = Decimal("1") + g_div
    acum = Decimal("1")
    for _ in range(anios):
        acum *= base
        total += acum
    return total / Decimal(anios)


def pais_de_isin(isin: str | None, nombre: str | None = None) -> str | None:
    """País del emisor según el vendor (resuelve ADRs vía ADR_PAIS_REAL)."""
    if not isin:
        return None
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]
    return g._pais_de_isin(isin, nombre or "")


def tasa_origen_observada(db, cartera_id: str, ventana_anios: int = 3) -> dict[str, Decimal]:  # type: ignore[no-untyped-def]
    """Tasa de retención de ORIGEN realmente aplicada por el broker, observada
    en los dividendos cobrados (ret/bruto ponderado por importe, últimos N
    años). Claves: ISIN y `"pais:XX"` (agregado del país como respaldo).

    Motivo (caso Francia, 2026-06-12): la estatutaria del vendor para FR es
    12,8% (tipo de persona física, lo que aplica TR) pero DeGiro/IBKR retienen
    el 25% general. La tabla legal dice cuánto PUEDES reclamar; lo observado
    dice cuánto te quitan de verdad — y eso es lo que debe usar el modelo.
    Se excluye la retención española (crédito 0591, no es de origen)."""
    from datetime import date

    from sqlalchemy import select

    from app.db import models

    desde = date.today().year - ventana_anios
    bruto_isin: dict[str, Decimal] = {}
    ret_isin: dict[str, Decimal] = {}
    pais_isin: dict[str, str] = {}
    txs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).scalars()
    for t in txs:
        if t.fecha.year < desde or t.retencion_pais == "ES":
            continue
        isin = t.posicion.isin if t.posicion else None
        bruto = Decimal(str(t.importe_eur or 0))
        if not isin or bruto <= 0:
            continue
        bruto_isin[isin] = bruto_isin.get(isin, _CERO) + bruto
        ret_isin[isin] = ret_isin.get(isin, _CERO) + Decimal(str(t.retencion_eur or 0))
        pais_isin.setdefault(isin, (pais_de_isin(isin, t.posicion.nombre) or "").upper())

    seis = Decimal("0.000001")   # los Out de la API validan max 6 decimales
    out: dict[str, Decimal] = {}
    bruto_pais: dict[str, Decimal] = {}
    ret_pais: dict[str, Decimal] = {}
    for isin, bruto in bruto_isin.items():
        out[isin] = (ret_isin[isin] / bruto).quantize(seis)
        pais = pais_isin.get(isin)
        if pais:
            bruto_pais[pais] = bruto_pais.get(pais, _CERO) + bruto
            ret_pais[pais] = ret_pais.get(pais, _CERO) + ret_isin[isin]
    for pais, bruto in bruto_pais.items():
        out[f"pais:{pais}"] = (ret_pais[pais] / bruto).quantize(seis)
    return out


def exceso_observado_pct(
    pais: str | None, isin: str | None,
    observadas: dict[str, Decimal] | None,
) -> Decimal:
    """Exceso sobre el tope CDI con la tasa OBSERVADA del propio usuario
    (ISIN → país → estatutaria como respaldo). Es la versión calibrada de
    `exceso_no_recuperable_pct`: misma semántica, mejor dato."""
    if not pais:
        return _CERO
    topes, _ = _tablas_vendor()
    tope = topes.get(pais.upper())
    if tope is None:
        return exceso_no_recuperable_pct(pais)
    tasa = None
    if observadas:
        if isin and isin in observadas:
            tasa = observadas[isin]
        elif f"pais:{pais.upper()}" in observadas:
            tasa = observadas[f"pais:{pais.upper()}"]
    if tasa is None:
        return exceso_no_recuperable_pct(pais)
    exceso = (tasa - Decimal(str(tope))).quantize(Decimal("0.000001"))
    return exceso if exceso > 0 else _CERO
