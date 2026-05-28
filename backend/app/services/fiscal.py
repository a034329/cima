"""Servicio fiscal — orquesta el motor de Cuádrate para una cartera Cima.

Arquitectura:
  - Cima mantiene sus propios `Lot` para PM real + dashboard tiempo real.
  - Este servicio invoca `motor_fiscal.FIFOTracker` + `compensacion_perdidas`
    de Cuádrate como librerías puras (sin tocar el orquestador
    `generar_irpf.py`). Es el cálculo fiscal "de verdad": FIFO multi-año,
    regla 2 meses, pérdidas diferidas/afloradas, RCM↔patrimoniales 25%,
    bolsas pérdidas 4 años.

  - In-process: sin subprocess, sin filesystem temporal. El resultado se
    devuelve serializado al frontend. No se persiste en BD — cada llamada
    recomputa desde las transacciones confirmadas. Si esto se vuelve
    costoso, se cachea con TTL.

Lo que NO está aún en esta v1:
  - Forex G/P (Art. 33.5.e LIRPF) — pendiente.
  - Opciones (5 casos DGT V2172-21) — pendiente.
  - Corporate actions complejas (scrip mixto, M&A) — pendiente.
  - CDI casilla 0588 dividend deduction — pendiente.

Las transacciones de tipo BUY/SELL se traducen a 'A'/'T' (formato canónico
del motor). DIVIDEND e INTEREST se agregan como RCM neto (sin entrar al
FIFO). El resto de tipos se ignora por ahora.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import Session as _SASession

from app.adapters.cuadrate import (
    get_compensacion_perdidas,
    get_motor_fiscal,
)
from app.db import models


# ── Serialización Cima → motor canónico ───────────────────────────────────


_TIPO_CIMA_A_MOTOR = {
    "BUY": "A",
    "SELL": "T",
    "CORPORATE_SPLIT": "SP",   # el motor reusa cantidad/importe_eur/gastos_eur
                               # para qty_old/qty_new/nominal_old respectivamente
}


def _tx_to_motor_dict(tx: models.Transaccion, broker_alias: str) -> dict[str, Any]:
    """Convierte una `Transaccion` de Cima al dict canónico que ingiere
    `FIFOTracker.process_all`.

    BUY/SELL → 'A'/'T' con cantidad e importe_eur en sus fields naturales.
    CORPORATE_SPLIT → 'SP' donde el motor reusa los slots de modo distinto:
       cantidad = qty_old, importe_eur = qty_new, gastos_eur = nominal_old.
    Esto sigue el contrato que `build_sp_row` define en `generar_irpf.py`.
    """
    nombre = tx.posicion.nombre or tx.posicion.isin
    tipo_motor = _TIPO_CIMA_A_MOTOR[tx.tipo]

    if tipo_motor == "SP":
        # Recuperar qty_old / qty_new / nominal_old del meta JSON en `notas`.
        try:
            meta = json.loads(tx.notas or "{}")
            sp = meta["split"]
            qty_old = Decimal(str(sp["qty_old"]))
            qty_new = Decimal(str(sp["qty_new"]))
            nominal_old = Decimal(str(sp.get("nominal_old", "1")))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ValueError(
                f"Split {tx.id[:8]} sin meta JSON parseable en `notas`: {e}"
            ) from e
        return {
            "tipo": "SP",
            "isin": tx.posicion.isin,
            "nombre": nombre,
            "fecha": tx.fecha,
            "cantidad": qty_old,
            "importe_eur": qty_new,
            "gastos_eur": nominal_old,
            "broker": broker_alias,
        }

    return {
        "tipo": tipo_motor,
        "isin": tx.posicion.isin,
        "nombre": nombre,
        "fecha": tx.fecha,
        "cantidad": Decimal(str(tx.cantidad)),
        "importe_eur": Decimal(str(tx.importe_eur)),
        "gastos_eur": Decimal(str(tx.gastos_eur)) + Decimal(str(tx.tasas_externas_eur)),
        "broker": broker_alias,
    }


def _serializar_operaciones(
    db: Session, cartera_id: str, fecha_corte: date
) -> list[dict[str, Any]]:
    """Carga todas las tx confirmadas relevantes para FIFO de una cartera
    (BUY/SELL/CORPORATE_SPLIT) hasta `fecha_corte` y las serializa
    ordenadas cronológicamente para el motor."""
    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo.in_(
            ["BUY", "SELL", "CORPORATE_SPLIT"]
        ))
        .where(models.Transaccion.fecha <= fecha_corte)
        .order_by(
            models.Transaccion.fecha,
            models.Transaccion.created_at,
        )
    ).scalars())

    # Cache de aliases de broker para evitar N+1
    broker_aliases: dict[str | None, str] = {}
    for tx in txs:
        if tx.broker_id not in broker_aliases:
            broker_aliases[tx.broker_id] = _broker_alias(db, tx.broker_id)

    return [_tx_to_motor_dict(tx, broker_aliases[tx.broker_id]) for tx in txs]


def _broker_alias(db: Session, broker_id: str | None) -> str:
    if broker_id is None:
        return "manual"
    b = db.get(models.Broker, broker_id)
    if b is None:
        return "manual"
    return (b.alias or b.broker_tipo).upper()


# ── RCM neto del ejercicio (dividendos + intereses - retenciones) ─────────

def _calcular_rcm_neto(
    db: Session, cartera_id: str, ejercicio: int | None
) -> Decimal:
    """Suma DIVIDEND + INTEREST del ejercicio, restando retenciones.

    `ejercicio=None` → acumulado (todos los años en BD).

    Es el `rcm_neto` que pide `calcular_compensacion`. NO incluye CDI casilla
    0588 (deducción doble imposición) — eso es una deducción de cuota
    posterior al cálculo de base, fuera del scope de la compensación.
    """
    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo.in_(["DIVIDEND", "INTEREST"]))
    ).scalars())

    rcm = Decimal("0")
    for tx in txs:
        if ejercicio is not None and tx.fecha.year != ejercicio:
            continue
        # importe_eur ya es BRUTO; restamos retención ES (la extranjera entra
        # como deducción en cuota, no reduce el bruto del RCM).
        bruto = Decimal(str(tx.importe_eur))
        ret_es = (
            Decimal(str(tx.retencion_eur))
            if tx.retencion_pais == "ES"
            else Decimal("0")
        )
        rcm += bruto - ret_es
    return rcm


# ── Extras para la compensación integrada (Resumen del ejercicio) ──────────

@dataclass
class FiscalExtras:
    """Aportes externos al FIFO de acciones que también entran en la base del
    ahorro y deben pasar por la misma compensación (RCM↔patrimoniales 25% +
    bolsas 4 años). Los usa el Resumen del ejercicio para integrar forex,
    opciones y letras en UN solo cálculo de compensación.

    - `gp_patrimonial_extra`: G/P patrimonial extra de la base del ahorro
      (p.ej. forex realizado, Art. 33.5.e). Se suma a `gp_bruto`.
    - `opciones_pl`: P&L declarable de opciones cerradas/expiradas (casilla
      1626). El motor lo trata como `gp_total = gp_deducible + opciones_pl`.
    - `rcm_neto_override`: si se da, sustituye el RCM interno (dividendos +
      intereses) por uno completo y correcto (div neto + intereses RCM +
      letras), excluyendo el interés de débito no deducible.
    """
    gp_patrimonial_extra: Decimal = Decimal("0")
    opciones_pl: Decimal = Decimal("0")
    rcm_neto_override: Decimal | None = None


# ── DTO de respuesta ──────────────────────────────────────────────────────

@dataclass
class FiscalResultado:
    """Resultado consolidado del cálculo fiscal de un ejercicio."""
    ejercicio: int
    cartera_id: str

    # Del FIFO multi-año
    n_matches: int
    gp_bruto: Decimal                      # ya descuenta afloradas; incluye 2M
    gp_no_deducible_2m: Decimal            # positivo: pérdidas bloqueadas
    total_perdida_aflorada: Decimal        # positivo: pérdidas diferidas que afloran este año
    matches: list[Any]                     # list[FIFOMatch]
    positions: list[Any]                   # list[PositionSummary]
    perdidas_diferidas_latentes: list[Any]
    warnings: list[str]
    orphan_sales: list[Any]

    # De la compensación
    rcm_neto: Decimal
    resultado_compensacion: Any            # ResultadoCompensacion

    # Metadatos
    fecha_corte: date
    fecha_calculo: date


# ── Entry point ───────────────────────────────────────────────────────────

def calcular_fiscal(
    db: Session, cartera_id: str, ejercicio: int | None,
    extras: "FiscalExtras | None" = None,
) -> FiscalResultado:
    """Resultado fiscal MEMOIZADO por petición (se llama varias veces por carga:
    dashboard directo + optimizador + rotación…). Solo cachea el caso común
    (`extras=None`); se invalida en cualquier flush/rollback. Tratar como solo
    lectura (no mutar el resultado)."""
    if extras is not None:
        return _calcular_fiscal_impl(db, cartera_id, ejercicio, extras)
    cache = db.info.setdefault("_fiscal_cache", {})
    key = (cartera_id, ejercicio)
    if key not in cache:
        cache[key] = _calcular_fiscal_impl(db, cartera_id, ejercicio, None)
    return cache[key]


@event.listens_for(_SASession, "after_flush")
def _invalidar_fiscal_cache(session: Session, flush_context: object) -> None:  # noqa: ARG001
    session.info.pop("_fiscal_cache", None)


@event.listens_for(_SASession, "after_rollback")
def _invalidar_fiscal_cache_rb(session: Session) -> None:
    session.info.pop("_fiscal_cache", None)


def _calcular_fiscal_impl(
    db: Session, cartera_id: str, ejercicio: int | None,
    extras: "FiscalExtras | None" = None,
) -> FiscalResultado:
    """Calcula el resultado fiscal de un ejercicio o el acumulado.

    `ejercicio=None` → modo ACUMULADO: incluye todos los matches FIFO de
    cualquier año + todos los dividendos del histórico. La compensación
    pierde sentido temporal en acumulado (no es lo que va a RentaWEB), pero
    se sigue devolviendo con `ejercicio=0` como un agregado informativo.

    1. Serializa transacciones BUY/SELL confirmadas hasta 31-dic-`ejercicio`
       (o todas si acumulado).
    2. Las pasa a `FIFOTracker` de Cuádrate → FIFOResults con flags 2M,
       pérdidas diferidas/afloradas, etc.
    3. Calcula G/P bruto y G/P no deducible (suma de matches con flag 2M).
    4. Suma RCM neto del ejercicio (dividendos + intereses).
    5. Auto-detecta pérdidas de ejercicios previos desde los matches FIFO.
    6. Aplica `calcular_compensacion`: RCM↔patrimoniales 25%, bolsas 4 años.
    7. Devuelve `FiscalResultado` con todo serializable para la UI.
    """
    extras = extras or FiscalExtras()
    mf = get_motor_fiscal()
    cp = get_compensacion_perdidas()

    if ejercicio is None:
        # Acumulado: tomamos todas las tx (corte muy lejano)
        fecha_corte = date(9999, 12, 31)
    else:
        fecha_corte = date(ejercicio, 12, 31)
    ops = _serializar_operaciones(db, cartera_id, fecha_corte)

    tracker = mf.FIFOTracker()
    tracker.process_all(ops)
    fifo = tracker.get_results()

    # Matches del ejercicio (la venta cae en `ejercicio`); en acumulado, todos.
    if ejercicio is None:
        matches_ej = list(fifo.matches)
    else:
        matches_ej = [m for m in fifo.matches if m.ejercicio_fiscal == ejercicio]

    # G/P bruto del ejercicio. Sigue el mismo cálculo que pdf_generator.py:
    # el G/P efectivo de cada match es `ganancia_perdida - perdida_diferida_aflorada_eur`,
    # porque `perdida_diferida_aflorada_eur` es positiva (valor absoluto del
    # aflorado) y aplica como pérdida adicional en el ejercicio en que se
    # transmite el lote bloqueado (Art. 33.5.f LIRPF último párrafo).
    gp_bruto = sum(
        (m.ganancia_perdida - m.perdida_diferida_aflorada_eur for m in matches_ej),
        Decimal("0"),
    )

    # G/P no deducible por regla 2M (pérdidas bloqueadas, valor absoluto positivo).
    gp_no_deducible_2m = sum(
        (
            -m.ganancia_perdida   # convertimos pérdida (negativa) en positivo
            for m in matches_ej
            if m.regla_2_meses and m.ganancia_perdida < 0
        ),
        Decimal("0"),
    )

    # Total de pérdidas diferidas que afloran este ejercicio (positivo).
    total_perdida_aflorada = sum(
        (m.perdida_diferida_aflorada_eur for m in matches_ej),
        Decimal("0"),
    )

    rcm_neto = _calcular_rcm_neto(db, cartera_id, ejercicio)
    # El Resumen del ejercicio puede sustituir el RCM por uno completo
    # (div neto + intereses RCM + letras) y añadir G/P patrimonial extra
    # (forex) + P&L de opciones, para una compensación integrada.
    rcm_para_comp = (
        extras.rcm_neto_override if extras.rcm_neto_override is not None else rcm_neto
    )
    gp_para_comp = gp_bruto + extras.gp_patrimonial_extra

    # En acumulado no hay "ejercicio actual" para detectar bolsas previas.
    # Pasamos el año siguiente al último match para que entren todos.
    if ejercicio is None:
        if fifo.matches:
            ejercicio_compensacion = max(m.ejercicio_fiscal for m in fifo.matches) + 1
        else:
            ejercicio_compensacion = date.today().year
    else:
        ejercicio_compensacion = ejercicio

    # Pérdidas pendientes de años anteriores: si el usuario las ha introducido
    # manualmente (desde sus declaraciones), son autoritativas. Si no, se
    # auto-detectan desde los matches FIFO (best-effort).
    from app.services.perdidas import perdidas_previas_motor
    perdidas_previas = perdidas_previas_motor(db, cartera_id)
    if not perdidas_previas:
        perdidas_previas = cp.auto_detectar_perdidas_anteriores(
            fifo.matches, ejercicio_actual=ejercicio_compensacion,
        )

    comp = cp.calcular_compensacion(
        ejercicio=ejercicio_compensacion,
        gp_bruto=gp_para_comp,
        gp_no_deducible_2m=gp_no_deducible_2m,
        rcm_neto=rcm_para_comp,
        opciones_pl=extras.opciones_pl,
        perdidas_previas=perdidas_previas,
        auto_guardar=False,               # nunca persistimos en disco
    )

    return FiscalResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,   # 0 = acumulado
        cartera_id=cartera_id,
        n_matches=len(matches_ej),
        gp_bruto=gp_bruto,
        gp_no_deducible_2m=gp_no_deducible_2m,
        total_perdida_aflorada=total_perdida_aflorada,
        matches=matches_ej,
        positions=fifo.positions,
        perdidas_diferidas_latentes=fifo.perdidas_diferidas_latentes,
        warnings=fifo.warnings,
        orphan_sales=fifo.orphan_sales,
        rcm_neto=rcm_para_comp,
        resultado_compensacion=comp,
        fecha_corte=fecha_corte,
        fecha_calculo=date.today(),
    )
