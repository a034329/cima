"""Informe mensual de la cartera (V3, mejoras 2026-06).

El cierre de mes que el usuario haría a mano: qué entró y salió, qué se
cobró, qué se realizó y cómo queda la marcha hacia la IF. Todo deriva de
datos ya existentes (transacciones confirmadas, matches FIFO, aportaciones
y la proyección IF del dashboard) — sin estado nuevo.

No hay histórico de precios, así que el informe NO inventa una "evolución
del valor de mercado del mes": cuenta flujos reales y deja la foto de
capital/IF a fecha de hoy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models

_C2 = Decimal("0.01")


@dataclass
class MovimientoDestacado:
    fecha: date
    tipo: str
    nombre: str
    importe_eur: Decimal


@dataclass
class VentaRealizada:
    nombre: str
    isin: str
    gp_eur: Decimal


@dataclass
class InformeMensual:
    anio: int
    mes: int
    # Flujos del mes
    compras_eur: Decimal
    n_compras: int
    ventas_eur: Decimal
    n_ventas: int
    gastos_eur: Decimal
    aportaciones_eur: Decimal
    dividendos_bruto_eur: Decimal
    dividendos_retencion_eur: Decimal      # todas las retenciones (origen + ES)
    dividendos_neto_eur: Decimal
    intereses_eur: Decimal
    gp_realizada_eur: Decimal              # G/P FIFO de ventas del mes
    # Valor de mercado a CIERRE del mes (histórico, ADR-004)
    valor_mercado_eur: Decimal | None
    valor_mercado_var_pct: Decimal | None      # variación % vs cierre del mes anterior
    valor_mercado_completo: bool               # False si faltó el cierre de algún valor
    # Foto a hoy (no histórica)
    capital_estrategia_eur: Decimal | None
    objetivo_if_eur: Decimal | None
    progreso_if_pct: Decimal | None
    anios_if: Decimal | None
    destacados: list[MovimientoDestacado] = field(default_factory=list)
    ventas_detalle: list[VentaRealizada] = field(default_factory=list)


def _q2(x: Decimal) -> Decimal:
    return x.quantize(_C2, ROUND_HALF_UP)


def calcular_informe(db: Session, cartera_id: str, anio: int, mes: int) -> InformeMensual:
    from app.services import proyeccion
    from app.services.fiscal import calcular_fiscal
    from app.services.impacto_if import parametros_proyeccion_if

    txs = [t for t in db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
    ).scalars() if t.fecha.year == anio and t.fecha.month == mes]

    cero = Decimal("0")
    compras = sum((Decimal(str(t.importe_eur)) for t in txs if t.tipo == "BUY"), cero)
    n_compras = sum(1 for t in txs if t.tipo == "BUY")
    ventas = sum((Decimal(str(t.importe_eur)) for t in txs if t.tipo == "SELL"), cero)
    n_ventas = sum(1 for t in txs if t.tipo == "SELL")
    gastos = sum((Decimal(str(t.gastos_eur or 0)) for t in txs), cero)
    div_bruto = sum((Decimal(str(t.importe_eur)) for t in txs if t.tipo == "DIVIDEND"), cero)
    div_ret = sum((Decimal(str(t.retencion_eur or 0)) for t in txs if t.tipo == "DIVIDEND"), cero)
    intereses = sum((Decimal(str(t.importe_eur)) for t in txs if t.tipo == "INTEREST"), cero)

    aportaciones = sum((Decimal(str(a.importe_eur)) for a in db.execute(
        select(models.Aportacion).where(models.Aportacion.cartera_id == cartera_id)
    ).scalars() if a.fecha.year == anio and a.fecha.month == mes), cero)

    # G/P realizada del mes: matches FIFO con venta dentro del mes. El motor es
    # multi-año; pedimos el ejercicio y filtramos por mes de venta.
    gp_mes = cero
    ventas_detalle: dict[str, VentaRealizada] = {}
    try:
        f = calcular_fiscal(db, cartera_id, anio)
        for m in f.matches:
            if m.fecha_venta.year == anio and m.fecha_venta.month == mes:
                gp = Decimal(str(m.ganancia_perdida))
                gp_mes += gp
                v = ventas_detalle.setdefault(
                    m.isin, VentaRealizada(nombre=m.nombre, isin=m.isin, gp_eur=cero))
                v.gp_eur += gp
    except Exception:
        pass   # sin transacciones de venta o motor sin datos: informe sin G/P

    # Valor de mercado a CIERRE del mes y variación % vs el mes anterior (ADR-004).
    valor_mercado = var_pct = None
    completo_mercado = True
    try:
        from app.services import historico
        ym = f"{anio:04d}-{mes:02d}"
        valor_mes, completo_mercado = historico.valor_cartera_mes(db, cartera_id, ym)
        py, pm = (anio, mes - 1) if mes > 1 else (anio - 1, 12)
        valor_prev, _ = historico.valor_cartera_mes(db, cartera_id, f"{py:04d}-{pm:02d}")
        if valor_mes is not None:
            valor_mercado = _q2(valor_mes)
            if valor_prev and valor_prev > 0:
                var_pct = ((valor_mes / valor_prev) - 1).quantize(
                    Decimal("0.0001"), ROUND_HALF_UP)
    except Exception:
        completo_mercado = True

    # Foto IF a hoy (misma proyección del dashboard, vía impacto_if).
    capital = progreso = anios = objetivo_if = None
    try:
        cap, aport, objetivo, retorno = parametros_proyeccion_if(db, cartera_id)
        capital = _q2(cap)
        objetivo_if = _q2(objetivo)
        if objetivo > 0:
            progreso = (cap / objetivo).quantize(Decimal("0.0001"), ROUND_HALF_UP)
        anios = proyeccion.anios_hasta(cap, aport, objetivo, float(retorno))
    except Exception:
        pass

    destacados = sorted(
        (MovimientoDestacado(
            fecha=t.fecha, tipo=t.tipo,
            nombre=(t.posicion.nombre if t.posicion else None) or t.isin or "—",
            importe_eur=_q2(Decimal(str(t.importe_eur))),
        ) for t in txs if t.tipo in ("BUY", "SELL", "DIVIDEND")),
        key=lambda m: -abs(m.importe_eur),
    )[:8]

    return InformeMensual(
        anio=anio, mes=mes,
        compras_eur=_q2(compras), n_compras=n_compras,
        ventas_eur=_q2(ventas), n_ventas=n_ventas,
        gastos_eur=_q2(gastos),
        aportaciones_eur=_q2(aportaciones),
        dividendos_bruto_eur=_q2(div_bruto),
        dividendos_retencion_eur=_q2(div_ret),
        dividendos_neto_eur=_q2(div_bruto - div_ret),
        intereses_eur=_q2(intereses),
        gp_realizada_eur=_q2(gp_mes),
        valor_mercado_eur=valor_mercado,
        valor_mercado_var_pct=var_pct,
        valor_mercado_completo=completo_mercado,
        capital_estrategia_eur=capital,
        objetivo_if_eur=objetivo_if,
        progreso_if_pct=progreso,
        anios_if=anios,
        destacados=destacados,
        ventas_detalle=sorted(
            (_vq(v) for v in ventas_detalle.values()),
            key=lambda v: -abs(v.gp_eur),
        ),
    )


def _vq(v: VentaRealizada) -> VentaRealizada:
    v.gp_eur = _q2(v.gp_eur)
    return v
