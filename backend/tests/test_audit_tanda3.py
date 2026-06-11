"""Regresiones de la auditoría Cima 2026-06-11 — Tanda 3 (jobs/atomicidad).

J1 — jobs IA sin guard de duplicados (doble clic → dos llamadas de minutos)
     y zombis en_curso eternos tras un crash/restart.
J2 — crear_manual commiteaba la tx confirmada ANTES del rebuild: una
     excepción del rebuild dejaba una transacción confirmada sin lotes.
J6 — GET /contexto relanzaba la generación IA completa en cada petición
     concurrente/repetida (coste real).
J7 — doble submit de aportación manual creaba dos filas idénticas.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, UTC
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import models


# ═══════════════════════════════════════════════════════════════════════════
# J1 — guard de duplicados + zombis
# ═══════════════════════════════════════════════════════════════════════════

def _job(db, cartera, estado="en_curso", hace_s=0):
    j = models.AnalisisJob(cartera_id=cartera.id, isin="US0378331005",
                           tipo="one_pager", estado=estado)
    db.add(j); db.flush()
    if hace_s:
        j.updated_at = datetime.now(UTC) - timedelta(seconds=hace_s)
        j.created_at = j.updated_at
        db.flush()
    db.commit()
    return j


def test_j1_job_en_curso_fresco_no_se_relanza(db: Session, cartera, monkeypatch):
    from app.services import jobs
    _job(db, cartera)
    lanzados = []
    monkeypatch.setattr(jobs.threading, "Thread",
                        lambda *a, **k: lanzados.append(1) or _ThreadStub())
    ok = jobs.lanzar(db, cartera.id, "US0378331005", "one_pager", lambda *a: None)
    assert ok is False, "doble clic lanzaba una segunda llamada IA de minutos"
    assert lanzados == []


def test_j1_job_zombi_se_relanza(db: Session, cartera, monkeypatch):
    from app.services import jobs
    _job(db, cartera, hace_s=jobs.STALE_S + 60)
    lanzados = []
    monkeypatch.setattr(jobs.threading, "Thread",
                        lambda *a, **k: lanzados.append(1) or _ThreadStub())
    ok = jobs.lanzar(db, cartera.id, "US0378331005", "one_pager", lambda *a: None)
    assert ok is True, "un en_curso rancio (hilo muerto) debe poder relanzarse"
    assert lanzados == [1]


class _ThreadStub:
    def start(self) -> None:  # noqa: D102
        pass


def test_j1_limpiar_zombis_al_arrancar(db: Session, cartera):
    from app.services import jobs
    _job(db, cartera)
    n = jobs.limpiar_zombis(db)
    assert n == 1
    j = db.query(models.AnalisisJob).one()
    assert j.estado == "error"
    assert "reinicio" in (j.error or "")


# ═══════════════════════════════════════════════════════════════════════════
# J2 — crear_manual es atómico
# ═══════════════════════════════════════════════════════════════════════════

def test_j2_excepcion_en_rebuild_no_deja_tx_confirmada(db: Session, cartera, monkeypatch):
    """Pre-fix: la tx confirmada se commiteaba primero; si el rebuild
    explotaba quedaba una transacción confirmada SIN lotes."""
    from app.adapters.cuadrate import TxCandidata
    from app.services import fifo
    from app.services.transacciones import crear_manual

    def _boom(db_, pos_id):
        raise RuntimeError("rebuild roto")
    monkeypatch.setattr(fifo, "rebuild_for_posicion", _boom)

    cand = TxCandidata(
        fecha=date(2025, 5, 1), tipo="BUY", isin="ES0000000001", nombre="ACME",
        cantidad=Decimal("10"), precio_local=Decimal("10"), divisa_local="EUR",
        importe_local=Decimal("100"), fx_rate=Decimal("1"),
        importe_eur=Decimal("100"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        retencion_pais=None, external_id=None, broker_id=None, notas=None,
    )
    with pytest.raises(RuntimeError):
        crear_manual(db, cartera.id, cand, confirmar_directo=True)
    assert db.query(models.Transaccion).count() == 0, \
        "la tx confirmada no puede sobrevivir a un rebuild fallido"


def test_j2_venta_sin_inventario_revierte_todo(db: Session, cartera):
    from app.adapters.cuadrate import TxCandidata
    from app.services.transacciones import crear_manual
    cand = TxCandidata(
        fecha=date(2025, 5, 1), tipo="SELL", isin="ES0000000001", nombre="ACME",
        cantidad=Decimal("10"), precio_local=Decimal("10"), divisa_local="EUR",
        importe_local=Decimal("100"), fx_rate=Decimal("1"),
        importe_eur=Decimal("100"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        retencion_pais=None, external_id=None, broker_id=None, notas=None,
    )
    with pytest.raises(HTTPException) as exc:
        crear_manual(db, cartera.id, cand, confirmar_directo=True)
    assert exc.value.status_code == 409
    assert db.query(models.Transaccion).count() == 0


# ═══════════════════════════════════════════════════════════════════════════
# J6 — PASO 0 coalescente
# ═══════════════════════════════════════════════════════════════════════════

def test_j6_get_repetido_no_relanza_la_ia(db: Session, cartera, monkeypatch):
    from app.services import paso0
    paso0._CTX_CACHE.clear()
    llamadas = []

    def _impl(db_, cid, isin):
        llamadas.append(isin)
        return "RESULTADO"
    monkeypatch.setattr(paso0, "_analizar_contexto_impl", _impl)

    r1 = paso0.analizar_contexto(db, cartera.id, "US0378331005")
    r2 = paso0.analizar_contexto(db, cartera.id, "US0378331005")
    assert r1 == r2 == "RESULTADO"
    assert llamadas == ["US0378331005"], \
        "el segundo GET relanzaba otra generación IA completa"


def test_j6_cache_caducada_regenera(db: Session, cartera, monkeypatch):
    from app.services import paso0
    paso0._CTX_CACHE.clear()
    llamadas = []
    monkeypatch.setattr(paso0, "_analizar_contexto_impl",
                        lambda db_, cid, isin: llamadas.append(isin) or "R")
    paso0.analizar_contexto(db, cartera.id, "US0378331005")
    key = f"{cartera.id}:US0378331005"
    ts, val = paso0._CTX_CACHE[key]
    paso0._CTX_CACHE[key] = (ts - paso0._CTX_TTL_S - 1, val)
    paso0.analizar_contexto(db, cartera.id, "US0378331005")
    assert len(llamadas) == 2


# ═══════════════════════════════════════════════════════════════════════════
# J7 — doble submit de aportación manual
# ═══════════════════════════════════════════════════════════════════════════

def test_j7_doble_submit_no_duplica(db: Session, cartera):
    from app.routers.aportaciones import AportacionIn, crear
    payload = AportacionIn(fecha=date(2025, 3, 1), importe_eur=Decimal("1000"),
                           descripcion="Transferencia", broker_id=None)
    crear(payload, db)
    crear(payload, db)
    assert db.query(models.Aportacion).count() == 1, \
        "el doble clic creaba dos filas y corrompía el neto anual"


def test_j7_importe_cero_se_rechaza(db: Session, cartera):
    from app.routers.aportaciones import AportacionIn, crear
    payload = AportacionIn(fecha=date(2025, 3, 1), importe_eur=Decimal("0"),
                           descripcion=None, broker_id=None)
    with pytest.raises(HTTPException) as exc:
        crear(payload, db)
    assert exc.value.status_code == 422
