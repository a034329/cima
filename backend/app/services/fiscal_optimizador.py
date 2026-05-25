"""Optimizador fiscal de cierre de año (tax-loss harvesting).

Reúne lo que ya tributa (G/P patrimonial realizada YTD + RCM) y las palancas
para reducir base antes del 31-dic:
  - Pérdidas latentes por posición (precio actual − PM real) → candidatas a
    realizar para compensar plusvalías, marcando el bloqueo de la regla 2M.
  - Bolsas de pérdidas de años anteriores (arrastre 4 años) ya disponibles.
  - Pérdidas diferidas 2M latentes (afloran al vender el lote recomprado).

Precios vía `precios.obtener_precios_eur` (OpenFIGI + yfinance, best-effort, con
override manual). Acepta `precios` inyectados para tests offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models
from app.services.fifo import estado_posicion
from app.services.fiscal import calcular_fiscal


_DIAS_2M = 62  # ventana regla 2 meses (aprox. 2 meses naturales)


@dataclass
class LatentePosicion:
    isin: str
    nombre: str
    cantidad: Decimal
    pm_real_eur: Decimal
    precio_actual_eur: Decimal | None
    valor_actual_eur: Decimal | None
    gp_latente_eur: Decimal | None
    es_perdida: bool
    bloqueo_2m: bool
    precio_manual: bool
    sin_precio: bool


@dataclass
class OptimizadorResultado:
    ejercicio: int
    fecha_calculo: date
    gp_realizada_ytd: Decimal            # patrimonial realizada (acciones, FIFO)
    rcm_ytd: Decimal
    bolsas_pendientes: Decimal           # pérdidas de años ANTERIORES disponibles
    perdida_a_arrastrar_anio: Decimal    # pérdida de ESTE año que se arrastra (4 años)
    diferidas_2m: Decimal                # pérdidas diferidas latentes (informativo)
    perdida_latente_cosechable: Decimal  # Σ pérdidas latentes sin bloqueo 2M (negativo)
    ganancia_latente_total: Decimal
    compensable_ahora: Decimal           # min(realizado+, |pérdida cosechable|)
    latentes: list[LatentePosicion] = field(default_factory=list)
    no_resueltos: list[str] = field(default_factory=list)


def calcular_optimizador(
    db: Session, cartera_id: str, ejercicio: int,
    precios: dict[str, Decimal] | None = None,
    no_resueltos: list[str] | None = None,
) -> OptimizadorResultado:
    if precios is None:
        from app.services.precios import obtener_precios_eur
        precios, no_resueltos = obtener_precios_eur(db, cartera_id)
    no_resueltos = no_resueltos or []

    # Realizado YTD + compensación + bolsas + diferidas 2M.
    f = calcular_fiscal(db, cartera_id, ejercicio)
    gp_realizada = Decimal(str(f.gp_bruto))
    rcm = Decimal(str(f.rcm_neto))
    comp = f.resultado_compensacion
    # Bolsas de AÑOS ANTERIORES = carryforward con origen anterior al ejercicio.
    # OJO: `perdidas_actualizadas` incluye también la pérdida nueva de ESTE año
    # (origen == ejercicio); no es "años anteriores" y la separamos.
    bolsas = sum(
        (Decimal(str(p.pendiente_eur)) for p in comp.perdidas_actualizadas
         if p.ejercicio_origen < ejercicio),
        Decimal("0"),
    )
    perdida_a_arrastrar = Decimal(str(comp.nuevo_saldo_negativo))
    f_acum = calcular_fiscal(db, cartera_id, None)
    diferidas = sum(
        (Decimal(str(p.importe_eur)) for p in f_acum.perdidas_diferidas_latentes),
        Decimal("0"),
    )

    # Compras del mismo ISIN en los últimos ~2 meses → bloquean la pérdida (2M).
    hoy = date.today()
    desde = hoy - timedelta(days=_DIAS_2M)
    compras_recientes = {
        t.posicion.isin
        for t in db.execute(
            select(models.Transaccion)
            .where(models.Transaccion.cartera_id == cartera_id)
            .where(models.Transaccion.estado == "confirmada")
            .where(models.Transaccion.tipo == "BUY")
            .where(models.Transaccion.fecha >= desde)
        ).scalars()
    }

    manual_isins = {
        p.isin for p in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars() if p.precio_manual_eur is not None
    }

    latentes: list[LatentePosicion] = []
    perdida_cosechable = Decimal("0")
    ganancia_latente = Decimal("0")
    for pos in db.execute(
        select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
    ).scalars():
        est = estado_posicion(db, pos.id)
        cant = est["cantidad"]
        if cant <= 0:
            continue
        pm = Decimal(str(est["pm_real_eur"]))
        px = precios.get(pos.isin)
        if px is None:
            latentes.append(LatentePosicion(
                isin=pos.isin, nombre=pos.nombre or pos.isin, cantidad=cant,
                pm_real_eur=pm, precio_actual_eur=None, valor_actual_eur=None,
                gp_latente_eur=None, es_perdida=False, bloqueo_2m=False,
                precio_manual=pos.isin in manual_isins, sin_precio=True,
            ))
            continue
        valor = px * cant
        gp = (px - pm) * cant
        es_perdida = gp < 0
        bloqueo = pos.isin in compras_recientes
        if es_perdida and not bloqueo:
            perdida_cosechable += gp
        if gp > 0:
            ganancia_latente += gp
        latentes.append(LatentePosicion(
            isin=pos.isin, nombre=pos.nombre or pos.isin, cantidad=cant,
            pm_real_eur=pm, precio_actual_eur=px, valor_actual_eur=valor,
            gp_latente_eur=gp, es_perdida=es_perdida, bloqueo_2m=bloqueo,
            precio_manual=pos.isin in manual_isins, sin_precio=False,
        ))

    latentes.sort(key=lambda x: (x.gp_latente_eur if x.gp_latente_eur is not None else Decimal("0")))

    compensable = min(
        gp_realizada if gp_realizada > 0 else Decimal("0"),
        -perdida_cosechable,
    ) if perdida_cosechable < 0 else Decimal("0")

    return OptimizadorResultado(
        ejercicio=ejercicio,
        fecha_calculo=hoy,
        gp_realizada_ytd=gp_realizada,
        rcm_ytd=rcm,
        bolsas_pendientes=bolsas,
        perdida_a_arrastrar_anio=perdida_a_arrastrar,
        diferidas_2m=diferidas,
        perdida_latente_cosechable=perdida_cosechable,
        ganancia_latente_total=ganancia_latente,
        compensable_ahora=compensable,
        latentes=latentes,
        no_resueltos=no_resueltos,
    )
