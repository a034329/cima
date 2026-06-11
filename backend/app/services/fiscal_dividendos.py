"""Cálculo de dividendos — espeja la pestaña Dividendos del Excel de Cuádrate.

Reutiliza `calcular_resumen_dividendos` de Cuádrate, que agrupa por ISIN y
calcula: bruto, retención origen, % efectivo, tope CDI, límite CDI EUR,
recuperable (casilla 0588), exceso no recuperable, es_nacional.

Las transacciones DIVIDEND de Cima guardan bruto en `importe_eur` y la
retención en `retencion_eur`. Aquí las descomponemos en los registros
DIV/RET que el motor espera y derivamos el país por ISIN.

Filtro por año: por fecha de pago (`fecha.year == ejercicio`). `ejercicio=None`
→ acumulado.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import _ensure_cuadrate_importable
from app.db import models


@dataclass
class DividendosResultado:
    ejercicio: int                   # 0 = acumulado
    resumen: list[dict[str, Any]]    # por ISIN (shape de calcular_resumen_dividendos)
    bruto_total: Decimal
    ret_origen_total: Decimal
    ret_es_total: Decimal            # retención de pagadores nacionales (popup 0029)
    cdi_recuperable_total: Decimal   # casilla 0588
    exceso_total: Decimal            # retención extranjera no recuperable (coste)
    bruto_ext_con_ret: Decimal       # bruto extranjero con retención > 0 (0588 base)
    fecha_calculo: date


def _broker_alias(db: Session, broker_id: str | None) -> str:
    if broker_id is None:
        return "manual"
    b = db.get(models.Broker, broker_id)
    return (b.alias or b.broker_tipo).upper() if b else "manual"


def calcular_dividendos(
    db: Session, cartera_id: str, ejercicio: int | None
) -> DividendosResultado:
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
        .order_by(models.Transaccion.fecha)
    ).scalars())
    if ejercicio is not None:
        txs = [t for t in txs if t.fecha.year == ejercicio]

    # Descomponer cada DIVIDEND en registros DIV (+ RET) con país por ISIN.
    registros: list[dict[str, Any]] = []
    alias_cache: dict[str | None, str] = {}
    for t in txs:
        pos = t.posicion
        isin = pos.isin
        nombre = pos.nombre or isin
        # País = domicilio del ISIN (criterio de Cuádrate), NO `retencion_pais`.
        # `retencion_pais` indica DÓNDE se retuvo (p.ej. retención a cuenta de
        # un broker español sobre un ETF irlandés), no la nacionalidad del
        # pagador. Usarlo clasificaría JEQP (IE) o un Treasury (US) como
        # nacionales e inflaría la retención ES. `_pais_de_isin` ya resuelve
        # ADRs vía ADR_PAIS_REAL.
        pais = g._pais_de_isin(isin, nombre)
        if t.broker_id not in alias_cache:
            alias_cache[t.broker_id] = _broker_alias(db, t.broker_id)
        broker = alias_cache[t.broker_id]
        fecha_str = t.fecha.strftime("%d/%m/%Y")
        registros.append({
            "isin": isin, "nombre": nombre, "pais": pais, "tipo": "DIV",
            "importe_eur": Decimal(str(t.importe_eur)), "broker": broker,
            "fecha": fecha_str,
        })
        if Decimal(str(t.retencion_eur)) > 0:
            # El RET sí respeta `retencion_pais` cuando es 'ES': es retención
            # IRPF española (TR Sucursal ES al 19% sobre dividendo extranjero)
            # → casilla 0591, NO retención en origen. El motor vendorizado
            # separa 0591/0588 exactamente por RET con pais='ES'
            # (auditoría Cima 2026-06-11, C2: antes la retención ES entraba
            # al cómputo CDI, se capaba al tope y la 0591 quedaba vacía —
            # inconsistente además con fiscal._calcular_rcm_neto, que sí usa
            # retencion_pais). El DIV mantiene el país del emisor.
            pais_ret = "ES" if t.retencion_pais == "ES" else pais
            registros.append({
                "isin": isin, "nombre": nombre, "pais": pais_ret, "tipo": "RET",
                "importe_eur": Decimal(str(t.retencion_eur)), "broker": broker,
                "fecha": fecha_str,
            })

    resumen = g.calcular_resumen_dividendos(registros)

    # Totales (mismos que el informe/Excel de Cuádrate)
    bruto_total = sum((d["bruto"] for d in resumen), Decimal("0"))
    ret_origen_total = sum((d["ret_origen"] for d in resumen), Decimal("0"))
    # Retención ESPAÑOLA (19% → casilla 0591): Cuádrate la reporta ahora en su
    # propio campo `retencion_es`, separada de la retención en origen extranjera
    # (`ret_origen` → CDI 0588). Aplica tanto a pagadores nacionales como a la
    # doble retención de TR Sucursal ES (extranjero con 19% ES sobre el neto).
    ret_es_total = sum(
        (d.get("retencion_es", Decimal("0")) for d in resumen), Decimal("0")
    )
    cdi_recuperable_total = sum(
        (d["recuperable"] for d in resumen if not d["es_nacional"]), Decimal("0")
    )
    exceso_total = sum((d["exceso"] for d in resumen), Decimal("0"))
    bruto_ext_con_ret = sum(
        (d["bruto"] for d in resumen
         if not d["es_nacional"] and d["ret_origen"] > 0),
        Decimal("0"),
    )

    return DividendosResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        resumen=resumen,
        bruto_total=bruto_total,
        ret_origen_total=ret_origen_total,
        ret_es_total=ret_es_total,
        cdi_recuperable_total=cdi_recuperable_total,
        exceso_total=exceso_total,
        bruto_ext_con_ret=bruto_ext_con_ret,
        fecha_calculo=date.today(),
    )


# ── Serie temporal de dividendos (para gráficas de evolución) ───────────────

@dataclass
class PuntoAnual:
    anio: int
    bruto: Decimal
    neto: Decimal        # bruto − retención (origen) cobrada


@dataclass
class PuntoMensual:
    anio: int
    mes: int             # 1-12
    bruto: Decimal
    neto: Decimal


@dataclass
class SerieDividendos:
    anual: list[PuntoAnual]       # un punto por año con dividendos (asc)
    mensual: list[PuntoMensual]   # un punto por (año, mes) con dividendos (asc)


def serie_dividendos(db: Session, cartera_id: str) -> SerieDividendos:
    """Evolución de dividendos cobrados por año y por mes (bruto y neto).
    Bruto = `importe_eur`; neto = bruto − `retencion_eur` (retención en origen).
    Pensado para la gráfica de crecimiento de rentas (historia de IF)."""
    filas = db.execute(
        select(
            models.Transaccion.fecha,
            models.Transaccion.importe_eur,
            models.Transaccion.retencion_eur,
        )
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).all()

    anual: dict[int, list[Decimal]] = {}   # anio -> [bruto, neto]
    mensual: dict[tuple[int, int], list[Decimal]] = {}   # (anio, mes) -> [bruto, neto]
    for fecha, importe, retencion in filas:
        bruto = Decimal(str(importe or 0))
        neto = bruto - Decimal(str(retencion or 0))
        a = anual.setdefault(fecha.year, [Decimal("0"), Decimal("0")])
        a[0] += bruto
        a[1] += neto
        m = mensual.setdefault((fecha.year, fecha.month), [Decimal("0"), Decimal("0")])
        m[0] += bruto
        m[1] += neto

    _c = Decimal("0.01")
    return SerieDividendos(
        anual=[
            PuntoAnual(anio=y, bruto=v[0].quantize(_c), neto=v[1].quantize(_c))
            for y, v in sorted(anual.items())
        ],
        mensual=[
            PuntoMensual(anio=k[0], mes=k[1], bruto=v[0].quantize(_c),
                         neto=v[1].quantize(_c))
            for k, v in sorted(mensual.items())
        ],
    )


# ── Diversificación de la renta (gráficas de concentración) ─────────────────

@dataclass
class TrozoDiv:
    clave: str           # nombre empresa / país / sector
    bruto: Decimal


@dataclass
class DiversificacionDividendos:
    anio: int | None     # None = todo el histórico
    bruto_total: Decimal
    por_empresa: list[TrozoDiv]   # desc por bruto
    por_pais: list[TrozoDiv]
    por_sector: list[TrozoDiv]


def diversificacion_dividendos(
    db: Session, cartera_id: str, anio: int | None = None
) -> DiversificacionDividendos:
    """Reparto del dividendo BRUTO por empresa, país y sector (concentración de
    renta). `anio=None` → todo el histórico. Sector vía feed (best-effort: lo no
    resuelto cae en 'Sin clasificar'). País: retención_pais, si no, prefijo ISIN."""
    from app.services.precios import sector_por_isin

    divs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo == "DIVIDEND")
    ).scalars()

    try:
        sectores = sector_por_isin(db, cartera_id)
    except Exception:
        sectores = {}

    emp: dict[str, list] = {}   # isin -> [nombre, bruto]
    pais: dict[str, Decimal] = {}
    sect: dict[str, Decimal] = {}
    total = Decimal("0")
    for d in divs:
        if anio is not None and d.fecha.year != anio:
            continue
        bruto = Decimal(str(d.importe_eur or 0))
        if bruto <= 0:
            continue
        total += bruto
        isin = d.posicion.isin
        nombre = d.posicion.nombre or isin
        e = emp.setdefault(isin, [nombre, Decimal("0")])
        e[1] += bruto
        p = (d.retencion_pais or (isin[:2] if isin else "??")).upper()
        pais[p] = pais.get(p, Decimal("0")) + bruto
        s = sectores.get(isin) or "Sin clasificar"
        sect[s] = sect.get(s, Decimal("0")) + bruto

    _c = Decimal("0.01")
    return DiversificacionDividendos(
        anio=anio,
        bruto_total=total.quantize(_c),
        por_empresa=sorted(
            (TrozoDiv(v[0], v[1].quantize(_c)) for v in emp.values()),
            key=lambda t: t.bruto, reverse=True),
        por_pais=sorted(
            (TrozoDiv(k, v.quantize(_c)) for k, v in pais.items()),
            key=lambda t: t.bruto, reverse=True),
        por_sector=sorted(
            (TrozoDiv(k, v.quantize(_c)) for k, v in sect.items()),
            key=lambda t: t.bruto, reverse=True),
    )
