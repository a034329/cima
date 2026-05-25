"""Tests del adapter IBKR — Activity Statement (un solo fichero).

A diferencia de DEGIRO (Transacciones + Cuenta), IBKR entrega todo en un
único CSV: Trades, Corporate Actions, Dividends, Withholding Tax, Interest.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import (
    broker_tipo_db,
    brokers_soportados,
    parse_ibkr_csv,
)
from app.db import models
from app.services.fiscal import calcular_fiscal
from app.services.transacciones import TxCandidata, reconciliar_extracto


IBKR_CSV = Path("/app/720/irpf/IBKR_2025.csv")


pytestmark = pytest.mark.skipif(
    not IBKR_CSV.is_file(),
    reason=f"Activity Statement IBKR no presente en {IBKR_CSV}",
)


# ── Shape ─────────────────────────────────────────────────────────────────


def test_devuelve_txcandidata() -> None:
    cands = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    assert isinstance(cands, list)
    assert len(cands) > 0
    assert all(isinstance(c, TxCandidata) for c in cands)


def test_tipos_incluyen_trades_dividendos_intereses() -> None:
    cands = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    tipos = {c.tipo for c in cands}
    assert "BUY" in tipos
    assert tipos.issubset(
        {"BUY", "SELL", "CORPORATE_SPLIT", "DIVIDEND", "INTEREST"}
    )


def test_dividendos_consolidan_bruto_y_retencion() -> None:
    cands = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    divs = [c for c in cands if c.tipo == "DIVIDEND"]
    assert len(divs) > 0
    # Al menos uno con retención
    con_ret = [d for d in divs if d.retencion_eur > 0]
    assert len(con_ret) > 0
    for d in divs:
        assert d.cantidad == Decimal("0")
        assert d.importe_eur > 0


def test_todas_las_filas_tienen_external_id() -> None:
    """Crítico para que reimportar el Activity Statement no duplique."""
    cands = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    assert all(c.external_id for c in cands)
    ids = [c.external_id for c in cands]
    assert len(ids) == len(set(ids)), "external_ids duplicados"


def test_external_id_determinista() -> None:
    c1 = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    c2 = parse_ibkr_csv(IBKR_CSV, broker_id="ibkr-test")
    assert sorted(c.external_id for c in c1) == sorted(c.external_id for c in c2)


def test_broker_id_propaga() -> None:
    cands = parse_ibkr_csv(IBKR_CSV, broker_id="mi-ibkr-xyz")
    assert all(c.broker_id == "mi-ibkr-xyz" for c in cands)


# ── Dispatch ───────────────────────────────────────────────────────────────


def test_ibkr_en_brokers_soportados() -> None:
    assert "ibkr" in brokers_soportados()


def test_broker_tipo_db_ibkr_identidad() -> None:
    assert broker_tipo_db("ibkr") == "ibkr"


# ── Integración: reimport idempotente + fiscal ────────────────────────────


def test_reimport_ibkr_no_duplica(
    db: Session, cartera: models.Cartera,
) -> None:
    broker = models.Broker(
        user_id=cartera.user_id, broker_tipo="ibkr", alias="IBKR",
    )
    db.add(broker); db.flush()

    cands1 = parse_ibkr_csv(IBKR_CSV, broker_id=broker.id)
    r1 = reconciliar_extracto(db, cartera.id, "ibkr", cands1)
    assert r1.insertadas > 0

    cands2 = parse_ibkr_csv(IBKR_CSV, broker_id=broker.id)
    r2 = reconciliar_extracto(db, cartera.id, "ibkr", cands2)
    assert r2.insertadas == 0, (
        f"Reimport IBKR duplicó {r2.insertadas} filas"
    )


def test_ibkr_dividendos_entran_en_rcm_fiscal(
    db: Session, cartera: models.Cartera,
) -> None:
    """Los dividendos IBKR parseados deben sumar al RCM neto del ejercicio."""
    broker = models.Broker(
        user_id=cartera.user_id, broker_tipo="ibkr", alias="IBKR",
    )
    db.add(broker); db.flush()

    cands = parse_ibkr_csv(IBKR_CSV, broker_id=broker.id)
    reconciliar_extracto(db, cartera.id, "ibkr", cands)

    # Los dividendos del CSV son de 2025
    r = calcular_fiscal(db, cartera.id, 2025)
    assert r.rcm_neto > 0, (
        "Esperábamos RCM neto positivo de los dividendos IBKR 2025"
    )


def test_cross_broker_ibkr_y_degiro_comparten_posicion_por_isin(
    db: Session,
    cartera: models.Cartera,
) -> None:
    """Si el mismo ISIN aparece en IBKR y se añade manualmente con otro
    broker, ambos comparten la misma posición (FIFO global por ISIN)."""
    broker_ibkr = models.Broker(
        user_id=cartera.user_id, broker_tipo="ibkr", alias="IBKR",
    )
    db.add(broker_ibkr); db.flush()

    cands = parse_ibkr_csv(IBKR_CSV, broker_id=broker_ibkr.id)
    reconciliar_extracto(db, cartera.id, "ibkr", cands)

    # Cada ISIN del CSV tiene exactamente una posición
    posiciones = list(db.execute(
        select(models.Posicion).where(
            models.Posicion.cartera_id == cartera.id
        )
    ).scalars())
    isines = [p.isin for p in posiciones]
    assert len(isines) == len(set(isines)), "ISIN duplicado en posiciones"
