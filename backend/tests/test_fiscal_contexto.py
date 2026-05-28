"""Tests del contexto fiscal para la recomendación (buffer, caducidad, 2M)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import fiscal_contexto as svc


def test_sin_historico_resumen_neutro(db: Session, cartera, monkeypatch) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    c = svc.calcular_contexto(db, cartera.id, 2026)
    assert c.perdidas_pendientes_eur == Decimal("0.00")
    assert c.buffer_disponible_eur == Decimal("0.00")
    assert "No hay pérdidas" in c.resumen


def test_buffer_y_caducidad_de_perdidas_pendientes(db: Session, cartera, monkeypatch) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    ej = 2026
    # Pérdida disponible (no caduca) + pérdida que caduca este ejercicio (origen ej-4).
    db.add(models.PerdidaPendienteManual(
        cartera_id=cartera.id, ejercicio_origen=ej - 1, importe_eur=Decimal("1000")))
    db.add(models.PerdidaPendienteManual(
        cartera_id=cartera.id, ejercicio_origen=ej - 4, importe_eur=Decimal("400")))
    db.commit()

    c = svc.calcular_contexto(db, cartera.id, ej)
    assert c.perdidas_pendientes_eur == Decimal("1400.00")
    assert c.buffer_disponible_eur >= Decimal("1400.00")
    assert c.caducan_este_anio_eur == Decimal("400.00")        # la de origen ej-4 expira en ej
    assert "absorberán" in c.resumen and "CADUCAN" in c.resumen


def test_resumen_2m_dice_diferida_no_anulada() -> None:
    c = svc.FiscalContexto(
        ejercicio=2026, base_ahorro_ytd_eur=Decimal("0"), gp_realizada_ytd_eur=Decimal("0"),
        perdidas_pendientes_eur=Decimal("0"), caducan_este_anio_eur=Decimal("0"),
        arrastre_anio_eur=Decimal("0"), buffer_disponible_eur=Decimal("0"),
        cosechable_latente_eur=Decimal("0"), compensable_ahora_eur=Decimal("0"),
        diferidas_2m_eur=Decimal("0"), bloqueo_2m=["Acme"],
    )
    r = svc._resumen(c)
    assert "DIFERIDA" in r and "no se pierde" in r and "anula" not in r.lower()
