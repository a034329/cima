"""Liquidez calculada de cash flows.

liquidez = Σ efectos de caja de todo lo que Cima conoce:
  - Aportaciones (depósitos +, retiradas −)
  - BUY  → −(importe + gastos + tasas)
  - SELL → +(importe − gastos − tasas)
  - DIVIDEND / INTEREST → +(importe − retención)
  - Opción venta  → +(importe − gastos)   (prima cobrada)
  - Opción compra → −(importe + gastos)   (prima pagada)
  - STAKING_REWARD / CORPORATE_SPLIT → 0 (no es caja)

Validación: se compara la liquidez calculada por broker contra el saldo
reportado por el broker (DEGIRO: última fila Saldo del cuenta; IBKR: Ending
Cash del Cash Report), capturado al importar y guardado en `Broker`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


def _cash_transaccion(t: models.Transaccion) -> Decimal:
    imp = Decimal(str(t.importe_eur))
    gastos = Decimal(str(t.gastos_eur)) + Decimal(str(t.tasas_externas_eur))
    ret = Decimal(str(t.retencion_eur))
    if t.tipo == "BUY":
        return -(imp + gastos)
    if t.tipo == "SELL":
        return imp - gastos
    if t.tipo in ("DIVIDEND", "INTEREST"):
        return imp - ret
    return Decimal("0")   # STAKING_REWARD, CORPORATE_*: no afectan caja


def _cash_opcion(o: models.Opcion) -> Decimal:
    imp = Decimal(str(o.importe_eur))
    gastos = Decimal(str(o.gastos_eur))
    if o.accion == "venta":
        return imp - gastos        # prima cobrada
    return -(imp + gastos)          # prima pagada


@dataclass
class LiquidezBroker:
    broker_id: str | None
    alias: str
    calculada: Decimal
    reportada: Decimal | None        # saldo del extracto (None si no reporta)
    diferencia: Decimal | None       # calculada − reportada


@dataclass
class LiquidezResultado:
    total_calculada: Decimal
    total_reportada: Decimal | None
    # Mejor estimación disponible: saldo reportado donde exista, calculada si no.
    # (La validación mostró que el cash-flow de DEGIRO no es fiable por no
    # capturar depósitos; el saldo reportado sí lo es.)
    total_disponible: Decimal
    por_broker: list[LiquidezBroker] = field(default_factory=list)


def calcular_liquidez(db: Session, cartera_id: str) -> LiquidezResultado:
    brokers = {
        b.id: b for b in db.execute(
            select(models.Broker)
        ).scalars()
    }

    calc: dict[str | None, Decimal] = {}

    def add(bid: str | None, amount: Decimal) -> None:
        calc[bid] = calc.get(bid, Decimal("0")) + amount

    for t in db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
    ).scalars():
        add(t.broker_id, _cash_transaccion(t))

    for o in db.execute(
        select(models.Opcion)
        .where(models.Opcion.cartera_id == cartera_id)
        .where(models.Opcion.estado == "confirmada")
    ).scalars():
        add(o.broker_id, _cash_opcion(o))

    for a in db.execute(
        select(models.Aportacion).where(models.Aportacion.cartera_id == cartera_id)
    ).scalars():
        add(a.broker_id, Decimal(str(a.importe_eur)))

    por_broker: list[LiquidezBroker] = []
    total_calc = Decimal("0")
    total_rep: Decimal | None = None
    total_disp = Decimal("0")
    for bid, monto in calc.items():
        b = brokers.get(bid) if bid else None
        alias = (b.alias or b.broker_tipo).upper() if b else "Manual / sin broker"
        reportada = None
        if b is not None and b.saldo_reportado_eur is not None:
            reportada = Decimal(str(b.saldo_reportado_eur))
            total_rep = (total_rep or Decimal("0")) + reportada
        dif = (monto - reportada) if reportada is not None else None
        por_broker.append(LiquidezBroker(
            broker_id=bid, alias=alias,
            calculada=monto.quantize(Decimal("0.01")),
            reportada=reportada.quantize(Decimal("0.01")) if reportada is not None else None,
            diferencia=dif.quantize(Decimal("0.01")) if dif is not None else None,
        ))
        total_calc += monto
        total_disp += reportada if reportada is not None else monto

    por_broker.sort(key=lambda x: -float(x.calculada))
    return LiquidezResultado(
        total_calculada=total_calc.quantize(Decimal("0.01")),
        total_reportada=total_rep.quantize(Decimal("0.01")) if total_rep is not None else None,
        total_disponible=total_disp.quantize(Decimal("0.01")),
        por_broker=por_broker,
    )


def liquidez_fuera_estrategia(db: Session, cartera_id: str) -> Decimal:
    """Liquidez asignada a bloques fuera de estrategia (colchón S1 y cualquier
    otro `en_estrategia=False`). Doctrina WG: intocable para reinversión."""
    total = Decimal("0")
    for b in db.execute(
        select(models.Bloque).where(models.Bloque.cartera_id == cartera_id)
    ).scalars():
        if not b.en_estrategia and b.liquidez_asignada_eur:
            total += Decimal(str(b.liquidez_asignada_eur))
    return total.quantize(Decimal("0.01"))


def liquidez_para_invertir(db: Session, cartera_id: str) -> tuple[Decimal, Decimal, Decimal]:
    """Tripleta `(disponible, total, fuera_estrategia)`. La `disponible` es lo
    que el usuario puede realmente desplegar hoy: total real en cuentas menos
    la liquidez apartada en bloques fuera de estrategia. Clamp >= 0 si el
    usuario asignó más colchón del que tiene en cuentas."""
    total = calcular_liquidez(db, cartera_id).total_disponible
    fuera = liquidez_fuera_estrategia(db, cartera_id)
    disponible = max(total - fuera, Decimal("0"))
    return disponible, total, fuera
