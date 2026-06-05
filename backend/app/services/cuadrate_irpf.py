"""Generación del XLSX maestro IRPF estilo Cuádrate (Roadmap 1.9).

Cima ya tiene toda la fiscalidad española calculada (`fiscal_resumen.py`); este
servicio orquesta los datos de la BD al formato que espera el generador
`excel_cartera.generate_cartera_xlsx` vendorizado desde Cuádrate. El usuario
descarga un XLSX listo para entregar/imprimir/usar como pauta de RentaWEB.

MVP: cubre BUYs/SELLs/SPlits del año natural y delega en el motor FIFO
vendored para reconstruir lotes y G/P. Las hojas opcionales (dividendos por
país, opciones por contrato, forex, T-Bills, intereses IBKR, staking TR,
gastos plataforma, futuros) se pasan en None inicialmente — `generate_cartera_xlsx`
las omite o las muestra vacías. Iteraciones posteriores las van rellenando.
"""
from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import get_excel_cartera, get_motor_fiscal
from app.db import models


# Mapeo del tipo interno de Cima al código de operación que Cuádrate usa.
# - BUY → 'A' (Adquisición)
# - SELL → 'T' (Transmisión)
# - CORPORATE_SPLIT → 'SP' (split/contrasplit; la cantidad es la POST-split)
# - CORPORATE_SCRIP → 'AL' (acción liberada, coste 0)
# El resto de tipos (DIVIDEND, INTEREST, STAKING_REWARD, CORPORATE_RIGHTS,
# CORPORATE_ISIN_CHANGE, CORPORATE_MERGER, CORPORATE_OPA, OTRO) NO entran como
# operaciones del FIFO: viven en hojas dedicadas (Dividendos, Intereses, etc.).
_TIPO_CIMA_A_CUADRATE = {
    "BUY": "A",
    "SELL": "T",
    "CORPORATE_SPLIT": "SP",
    "CORPORATE_SCRIP": "AL",
}


def _broker_alias(db: Session, broker_id: str | None) -> str:
    """Etiqueta del broker para la hoja Operaciones. Vacío si no hay broker."""
    if not broker_id:
        return ""
    b = db.get(models.Broker, broker_id)
    return (b.alias or b.broker_tipo.upper()) if b else ""


def _parse_split_meta(notas: str | None) -> tuple[Decimal, Decimal, Decimal]:
    """Recupera (qty_old, qty_new, nominal_old) del JSON serializado en `notas`
    de las transacciones CORPORATE_SPLIT (escrito por adapters/cuadrate.py)."""
    if not notas:
        return Decimal("0"), Decimal("0"), Decimal("1")
    try:
        meta = json.loads(notas)
        sp = meta.get("split", {})
        return (
            Decimal(str(sp.get("qty_old", 0))),
            Decimal(str(sp.get("qty_new", 0))),
            Decimal(str(sp.get("nominal_old", 1))),
        )
    except (ValueError, TypeError):
        return Decimal("0"), Decimal("0"), Decimal("1")


def construir_operaciones(db: Session, cartera_id: str) -> list[dict]:
    """Traduce las Transacciones de Cima al formato in-memory que espera
    `motor_fiscal.calcular_fifo_from_ops` (y, en cascada, `generate_cartera_xlsx`).

    Devuelve TODAS las transacciones confirmadas (multi-año). El motor FIFO
    necesita el histórico completo para calcular el coste base correcto.

    Para CORPORATE_SPLIT el dict carga `qty_old`/`qty_new` (extraídos del
    JSON en notas) en los campos `cantidad`/`importe_eur` reusando la
    convención del parser DeGiro de Cuádrate. El motor sabe leerlos así.
    """
    posiciones = {
        p.id: p for p in db.execute(
            select(models.Posicion).where(models.Posicion.cartera_id == cartera_id)
        ).scalars()
    }
    txs = db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .order_by(models.Transaccion.fecha, models.Transaccion.id)
    ).scalars().all()

    ops: list[dict] = []
    broker_cache: dict[str | None, str] = {}
    for tx in txs:
        tipo_cuadrate = _TIPO_CIMA_A_CUADRATE.get(tx.tipo)
        if not tipo_cuadrate:
            continue   # Dividendos/intereses/staking/etc. — fuera del FIFO
        pos = posiciones.get(tx.posicion_id)
        if pos is None:
            continue   # Huérfana: ignoramos para no romper el motor
        broker_alias = broker_cache.get(tx.broker_id)
        if broker_alias is None:
            broker_alias = _broker_alias(db, tx.broker_id)
            broker_cache[tx.broker_id] = broker_alias

        if tipo_cuadrate == "SP":
            # Split: `cantidad` = qty_old, `importe_eur` = qty_new (estructura
            # heredada del parser DeGiro). `gastos_eur` = nominal_old.
            qty_old, qty_new, nominal_old = _parse_split_meta(tx.notas)
            ops.append({
                "tipo": "SP",
                "isin": pos.isin,
                "nombre": (pos.nombre or pos.isin)[:120],
                "fecha": tx.fecha,
                "cantidad": qty_old,
                "importe_eur": qty_new,
                "gastos_eur": nominal_old,
                "es_scrip": False,
                "es_derecho": False,
                "broker": broker_alias,
            })
            continue

        # BUY / SELL / AL
        importe = Decimal(tx.importe_eur or 0)
        # `gastos_eur` que ve el motor = comisión broker + AutoFX + tasas
        # externas (forman el coste de adquisición / minoran la venta).
        gastos_broker = Decimal(tx.gastos_eur or 0)
        tasas_ext = Decimal(tx.tasas_externas_eur or 0)
        op = {
            "tipo": tipo_cuadrate,
            "isin": pos.isin,
            "nombre": (pos.nombre or pos.isin)[:120],
            "fecha": tx.fecha,
            "cantidad": Decimal(tx.cantidad or 0),
            "importe_eur": importe,
            "gastos_eur": gastos_broker + tasas_ext,
            "es_scrip": tipo_cuadrate == "AL",
            "es_derecho": False,
            # Desglose informativo para la hoja Operaciones.
            "gastos_broker": gastos_broker,
            "gastos_autofx": Decimal("0"),
            "gastos_externos": tasas_ext,
            "broker": broker_alias,
            "instrument_type": "STOCK",   # MVP: clasificación fina queda para iter.
        }
        ops.append(op)
    return ops


def generar_xlsx(db: Session, cartera_id: str, ejercicio: int) -> Path:
    """Genera el XLSX maestro IRPF del ejercicio en un fichero temporal y
    devuelve la ruta. El llamador (router) se encarga de streamearlo y
    limpiarlo después.

    MVP: hojas Operaciones, G_P_por_valor, Pérdidas arrastradas y Resumen
    desde las transacciones BUY/SELL/SP. Hojas opcionales (dividendos por país,
    opciones por contrato, forex, T-Bills, intereses, staking, gastos
    plataforma, futuros) se pasan en None; el generador las omite o muestra
    vacías. Las siguientes iteraciones cubrirán cada una.
    """
    motor = get_motor_fiscal()
    excel = get_excel_cartera()

    todas_ops = construir_operaciones(db, cartera_id)
    # `return_ops=True` → cada compra recibe `_lote_id` para que el XLSX
    # pueda cross-linkear coste compra ↔ G/P prorrateada en la hoja
    # G_P_por_valor (fórmulas editables).
    fifo_results, all_ops_with_ids = motor.calcular_fifo_from_ops(
        todas_ops, return_ops=True,
    )

    ops_actuales = [op for op in all_ops_with_ids
                    if hasattr(op.get("fecha"), "year")
                    and op["fecha"].year == ejercicio]
    ops_historicas = [op for op in all_ops_with_ids
                      if hasattr(op.get("fecha"), "year")
                      and op["fecha"].year != ejercicio]

    out_dir = Path(tempfile.mkdtemp(prefix="cima_irpf_"))
    out_path = out_dir / f"cartera_valores_irpf_{ejercicio}.xlsx"

    excel.generate_cartera_xlsx(
        ejercicio=ejercicio,
        output_path=str(out_path),
        operaciones=ops_actuales,
        ops_motor_con_ids=ops_actuales,
        ops_historicas_con_ids=ops_historicas,
        fifo_results=fifo_results,
        # MVP: el resto pendiente de iteraciones (dividendos por país con CDI,
        # opciones por contrato, forex IBKR, T-Bills, intereses, staking, etc.).
        dividendos_resumen=None,
        opciones_por_contrato=None,
        opciones_totales=None,
        compensacion=None,
        paths_anteriores=None,
        fx_pl=None,
        ibkr_interest=None,
        tr_staking=None,
        gastos_plataforma=None,
        futuros_por_contrato=None,
        futuros_totales=None,
    )
    return out_path
