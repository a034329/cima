"""Regresiones del backlog final de la auditoría Cima 2026-06-11.

D6 — brokers con saldo reportado pero sin movimientos no sumaban al dry
     powder (y el select de brokers no filtraba por usuario).
S1 — bootstrap creaba usuarios modo='owner' hardcodeado aunque el backend
     corriera como SaaS.
S2 — GET/DELETE de transacciones y aportaciones por id global sin scoping
     a la cartera activa (IDOR latente multi-usuario).
PAG — el historial del asesor crecía sin cota en el GET.
J8 — range(2010, 2030) hardcodeado descartaría dividendos futuros.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import models


def test_d6_broker_sin_movimientos_con_saldo_suma(db: Session, cartera):
    from app.services.liquidez import calcular_liquidez
    b = models.Broker(user_id=cartera.user_id, broker_tipo="degiro", alias="DG",
                      saldo_reportado_eur=Decimal("2500"), saldo_fecha=date(2025, 6, 1))
    db.add(b); db.commit()
    r = calcular_liquidez(db, cartera.id)
    assert any(x.broker_id == b.id for x in r.por_broker), \
        "el broker con saldo reportado y cero movimientos no aparecía"
    assert r.total_disponible == Decimal("2500.00")


def test_s1_bootstrap_respeta_modo_saas(db: Session):
    from app.routers.bootstrap import BootstrapIn, bootstrap
    out = bootstrap(BootstrapIn(), db)  # conftest fija settings.mode = SAAS
    u = db.query(models.User).one()
    assert u.modo == "saas", "se persistía 'owner' hardcodeado en modo SaaS"


def test_s2_transaccion_de_otra_cartera_es_404(db: Session, cartera):
    from app.routers.transacciones import obtener_transaccion, descartar_transaccion
    # Segunda cartera (otro usuario) con una transacción.
    u2 = models.User(email="otro@cima.local", modo="saas")
    db.add(u2); db.flush()
    c2 = models.Cartera(user_id=u2.id, nombre="Ajena")
    db.add(c2); db.flush()
    p2 = models.Posicion(cartera_id=c2.id, isin="US0000000002", nombre="X",
                         divisa_local="EUR")
    db.add(p2); db.flush()
    tx2 = models.Transaccion(
        cartera_id=c2.id, broker_id=None, posicion_id=p2.id,
        fecha=date(2025, 1, 1), tipo="BUY", cantidad=Decimal("1"),
        precio_local=Decimal("1"), divisa_local="EUR",
        importe_local=Decimal("1"), fx_rate=Decimal("1"),
        importe_eur=Decimal("1"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )
    db.add(tx2); db.commit()
    # La cartera activa (por defecto) es la del fixture, no c2 → 404.
    with pytest.raises(HTTPException) as e1:
        obtener_transaccion(tx2.id, db)
    assert e1.value.status_code == 404
    with pytest.raises(HTTPException) as e2:
        descartar_transaccion(tx2.id, db)
    assert e2.value.status_code == 404
    db.refresh(tx2)
    assert tx2.estado == "confirmada", "no debe poder descartarse desde otra cartera"


def test_pag_historial_asesor_acotado(db: Session, cartera):
    from app.services.asesor import historial
    for i in range(250):
        db.add(models.MensajeAsesor(cartera_id=cartera.id, rol="user",
                                    contenido=f"m{i}"))
    db.commit()
    out = historial(db, cartera.id)          # default 200
    assert len(out) == 200
    assert out[-1].contenido == "m249", "deben ser los ÚLTIMOS, en orden cronológico"
    assert out[0].contenido == "m50"
    assert len(historial(db, cartera.id, limit=None)) == 250


def test_j8_sin_anio_tope_hardcodeado():
    import inspect
    from app.adapters import cuadrate
    src = inspect.getsource(cuadrate)
    assert "range(2010, 2030)" not in src, \
        "el tope 2030 hardcodeado descartaría dividendos futuros en silencio"
    assert "_EJERCICIO_LOCK" in src
