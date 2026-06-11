"""Cálculo de intereses — RCM (casilla 0027) e informativo no deducible.

Lee las transacciones tipo INTEREST y STAKING_REWARD. El tipo (credit / debit / bond_interest)
y la casilla se guardaron en `notas` como JSON al importar (ver adapter). Si la
nota no es JSON (formato antiguo) se infiere el tipo por el signo del importe.

Clasificación fiscal (Cuádrate):
  - credit  → RCM, casilla 0027 (intereses de cuentas).
  - bond_interest → RCM, casilla 0027 (cupones).
  - staking → RCM en especie, casilla 0027 (DGT V1766-22, Art. 25.2 + 43.1
    LIRPF; alternativa doctrinal 0031, cuota idéntica).

NOTA casilla: el 0023 anterior era el default legacy erróneo arrastrado de
Cuádrate (V1 auditoría 2026-06-11 — la casilla verificada contra RentaWEB
es 0027). Las notas JSON de importaciones antiguas pueden traer '0023';
se normalizan a '0027' al leer.
  - debit   → interés pagado al broker. NO deducible automáticamente para
    particulares (Art. 26.1.b LIRPF, criterio AEAT). Informativo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class InteresLinea:
    fecha: date
    tipo: str                # credit / debit / bond_interest
    casilla: str | None      # '0027' o None
    descripcion: str
    divisa: str
    importe_eur: Decimal
    broker: str


@dataclass
class InteresesResultado:
    ejercicio: int                   # 0 = acumulado
    lineas: list[InteresLinea]
    rcm_total: Decimal               # credit + bond + staking → casilla 0027
    debit_total: Decimal             # informativo, no deducible (negativo)
    neto_total: Decimal              # suma de todos los importes
    fecha_calculo: date


def _broker_alias(db: Session, broker_id: str | None) -> str:
    if broker_id is None:
        return "manual"
    b = db.get(models.Broker, broker_id)
    return (b.alias or b.broker_tipo).upper() if b else "manual"


def _meta_de_notas(notas: str | None, importe: Decimal) -> dict:
    """Extrae {tipo, casilla, descripcion, divisa} de las notas JSON. Si no es
    JSON (interés antiguo), infiere por el signo: negativo → debit."""
    if notas:
        try:
            data = json.loads(notas)
            if isinstance(data, dict) and "interes" in data:
                return data["interes"]
        except (ValueError, TypeError):
            pass
    if importe < 0:
        return {"tipo": "debit", "casilla": None, "descripcion": notas or "",
                "divisa": "EUR"}
    return {"tipo": "credit", "casilla": "0027", "descripcion": notas or "",
            "divisa": "EUR"}


def calcular_intereses(
    db: Session, cartera_id: str, ejercicio: int | None
) -> InteresesResultado:
    txs = list(db.execute(
        select(models.Transaccion)
        .where(models.Transaccion.cartera_id == cartera_id)
        .where(models.Transaccion.estado == "confirmada")
        .where(models.Transaccion.tipo.in_(["INTEREST", "STAKING_REWARD"]))
        .order_by(models.Transaccion.fecha)
    ).scalars())
    if ejercicio is not None:
        txs = [t for t in txs if t.fecha.year == ejercicio]

    alias_cache: dict[str | None, str] = {}
    lineas: list[InteresLinea] = []
    rcm_total = Decimal("0")
    debit_total = Decimal("0")
    neto_total = Decimal("0")
    for t in txs:
        importe = Decimal(str(t.importe_eur))
        if t.tipo == "STAKING_REWARD":
            # RCM en especie (DGT V1766-22): valor EUR al momento de la
            # recepción. Antes era INVISIBLE para el resumen fiscal
            # (auditoría Cima 2026-06-11, A2 — espejo del CL7 de Cuádrate).
            meta = {"tipo": "staking", "casilla": "0027",
                    "descripcion": t.notas or "Staking reward", "divisa": "EUR"}
        else:
            meta = _meta_de_notas(t.notas, importe)
        tipo = meta.get("tipo") or ("debit" if importe < 0 else "credit")
        casilla = meta.get("casilla")
        if casilla == "0023":
            casilla = "0027"   # normalizar legacy (V1)
        if t.broker_id not in alias_cache:
            alias_cache[t.broker_id] = _broker_alias(db, t.broker_id)
        lineas.append(InteresLinea(
            fecha=t.fecha,
            tipo=tipo,
            casilla=casilla,
            descripcion=meta.get("descripcion") or "",
            divisa=meta.get("divisa") or "EUR",
            importe_eur=importe,
            broker=alias_cache[t.broker_id],
        ))
        neto_total += importe
        if tipo in ("credit", "bond_interest", "staking"):
            rcm_total += importe
        elif tipo == "debit":
            debit_total += importe

    return InteresesResultado(
        ejercicio=ejercicio if ejercicio is not None else 0,
        lineas=lineas,
        rcm_total=rcm_total,
        debit_total=debit_total,
        neto_total=neto_total,
        fecha_calculo=date.today(),
    )
