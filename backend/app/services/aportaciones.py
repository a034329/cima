"""Aportaciones de capital externo (dinero del bolsillo del usuario).

Parsers de depósitos/retiradas externos:
  - IBKR: sección "Deposits & Withdrawals" (Electronic Fund Transfer, etc.).
  - Trade Republic: filas CASH/CUSTOMER_INBOUND y CASH/TRANSFER_INBOUND
    (depósitos) y TRANSFER_OUTBOUND/CUSTOMER_OUTBOUND (retiradas).
  - DEGIRO: NO disponible en sus CSV (las SEPA externas están en el banco
    flatex, extracto aparte) → entrada manual.

Reconciliación: dedup por (broker_id, external_id) determinista.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import models


@dataclass
class AportacionCandidata:
    fecha: date
    importe_eur: Decimal       # + aportación / − retirada
    descripcion: str | None
    external_id: str
    broker_id: str | None


@dataclass
class ImportAportacionesResultado:
    insertadas: int = 0
    deduplicadas: int = 0


def _existe(db: Session, broker_id: str | None, external_id: str) -> bool:
    return db.execute(
        select(models.Aportacion)
        .where(models.Aportacion.broker_id == broker_id)
        .where(models.Aportacion.external_id == external_id)
    ).scalar_one_or_none() is not None


def reconciliar_aportaciones(
    db: Session, cartera_id: str, candidatas: list[AportacionCandidata]
) -> ImportAportacionesResultado:
    res = ImportAportacionesResultado()
    for c in candidatas:
        if c.external_id and _existe(db, c.broker_id, c.external_id):
            res.deduplicadas += 1
            continue
        db.add(models.Aportacion(
            cartera_id=cartera_id, broker_id=c.broker_id, fecha=c.fecha,
            importe_eur=c.importe_eur, descripcion=c.descripcion,
            origen="extracto", external_id=c.external_id,
        ))
        db.flush()
        res.insertadas += 1
    db.commit()
    return res


# ── Parsers ──────────────────────────────────────────────────────────────


def parse_ibkr_aportaciones(
    csv_path: str | Path, broker_id: str
) -> list[AportacionCandidata]:
    """Sección 'Deposits & Withdrawals' del Activity Statement IBKR.
    Formato: Deposits & Withdrawals,Data,<CUR>,<fecha ISO>,<desc>,<amount>."""
    import csv as _csv

    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    out: list[AportacionCandidata] = []
    with open(str(csv_path), encoding="utf-8") as f:
        for row in _csv.reader(f):
            if len(row) < 6 or row[0] != "Deposits & Withdrawals" or row[1] != "Data":
                continue
            cur = (row[2] or "").strip()
            if cur.lower().startswith("total") or len(cur) != 3:
                continue   # filas Total / subtotales
            fecha_str = (row[3] or "").strip()
            desc = (row[4] or "").strip()
            amt_str = (row[5] or "").strip().replace(",", "")
            try:
                fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
                importe = Decimal(amt_str or "0")
            except (ValueError, Exception):
                continue
            if importe == 0:
                continue
            # Depósitos en divisa ≠ EUR: convertir al BCE de la fecha
            # (auditoría Cima 2026-06-11, A11 — antes 10.000 USD se
            # registraban como 10.000 EUR, inflando el capital aportado).
            # Sin tipo de cambio disponible, NO registrar un importe
            # erróneo: se omite la fila (mismo criterio que el vendor).
            if cur != "EUR":
                rate = g._ibkr_eur_per_unit(fecha_str, cur)
                if rate is None:
                    continue
                importe = (importe * rate).quantize(Decimal("0.01"))
            ext_id = f"ibkr-dw-{fecha.isoformat()}-{int(importe * 100)}-{desc[:20]}"
            out.append(AportacionCandidata(
                fecha=fecha, importe_eur=importe, descripcion=desc,
                external_id=ext_id, broker_id=broker_id,
            ))
    return out


def parse_tr_aportaciones(
    csv_path: str | Path, broker_id: str
) -> list[AportacionCandidata]:
    """Depósitos/retiradas del CSV de Trade Republic. Categorías CASH con
    type CUSTOMER_INBOUND / TRANSFER_INBOUND (+) y *_OUTBOUND (−)."""
    import csv as _csv

    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g  # type: ignore[import-not-found]

    out: list[AportacionCandidata] = []
    try:
        with open(str(csv_path), encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                cat = (row.get("category") or "").upper()
                tipo = (row.get("type") or "").upper()
                if cat != "CASH":
                    continue
                signo = 0
                if tipo in ("CUSTOMER_INBOUND", "TRANSFER_INBOUND"):
                    signo = 1
                elif tipo in ("CUSTOMER_OUTBOUND", "TRANSFER_OUTBOUND"):
                    signo = -1
                else:
                    continue
                # parse_es del vendor: maneja '1234.56', '1234,56' y el
                # mixto es-ES '1.234,56'. El replace(',','.') ingenuo
                # anterior rompía con separador de miles ('1.234,56' →
                # InvalidOperation → fila tragada): los depósitos ≥1.000 €
                # desaparecían en silencio del capital aportado (auditoría
                # Cima 2026-06-11, C4 — misma familia que CL5/F8 de Cuádrate).
                importe = g.parse_es((row.get("amount") or "0").strip()) * signo
                if importe == 0:
                    continue
                fecha_str = (row.get("date") or row.get("datetime") or "")[:10]
                try:
                    fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                tx_id = row.get("transaction_id") or ""
                ext_id = (tx_id or
                          f"tr-dw-{fecha.isoformat()}-{int(importe * 100)}")
                out.append(AportacionCandidata(
                    fecha=fecha, importe_eur=importe,
                    descripcion=row.get("description") or tipo,
                    external_id=ext_id, broker_id=broker_id,
                ))
    except Exception:
        return []
    return out


# ── Resumen por año ────────────────────────────────────────────────────────


def _to_dec(s: str) -> Decimal:
    s = (s or "").strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def saldo_degiro_cuenta(cuenta_path: str | Path) -> tuple[Decimal, date] | None:
    """Última fila con Saldo del cuenta DEGIRO (el CSV viene en orden
    descendente, así que la primera fila de datos con Saldo EUR es la más
    reciente). Devuelve (saldo, fecha) o None."""
    import csv as _csv

    try:
        with open(str(cuenta_path), encoding="utf-8") as f:
            reader = _csv.reader(f)
            next(reader, None)
            for row in reader:
                # 0 Fecha 1 Hora 2 FechaValor 3 Prod 4 ISIN 5 Desc 6 Tipo
                # 7 Var.divisa 8 Var.importe 9 Saldo.divisa 10 Saldo.importe
                if len(row) < 11:
                    continue
                saldo_div = (row[9] or "").strip()
                saldo_imp = (row[10] or "").strip()
                if saldo_div != "EUR" or not saldo_imp:
                    continue
                fecha = _parse_fecha_dmy(row[0])
                if fecha is None:
                    continue
                return _to_dec(saldo_imp), fecha
    except Exception:
        return None
    return None


def saldo_ibkr_ending_cash(csv_path: str | Path) -> Decimal | None:
    """Ending Cash (Base Currency Summary) del Cash Report de IBKR."""
    import csv as _csv

    try:
        with open(str(csv_path), encoding="utf-8") as f:
            for row in _csv.reader(f):
                if (len(row) >= 5 and row[0] == "Cash Report" and row[1] == "Data"
                        and row[2] == "Ending Cash"
                        and row[3] == "Base Currency Summary"):
                    return Decimal((row[4] or "0").replace(",", ""))
    except Exception:
        return None
    return None


def _parse_fecha_dmy(s: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def aportaciones_por_anio(db: Session, cartera_id: str) -> dict[int, Decimal]:
    """Aportación neta por año (Σ importes con signo)."""
    rows = db.execute(
        select(models.Aportacion).where(models.Aportacion.cartera_id == cartera_id)
    ).scalars()
    por_anio: dict[int, Decimal] = {}
    for a in rows:
        por_anio[a.fecha.year] = por_anio.get(a.fecha.year, Decimal("0")) + Decimal(
            str(a.importe_eur)
        )
    return por_anio
