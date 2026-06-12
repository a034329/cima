"""Tests del panel de fugas fiscales (exceso CDI no recuperable)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.db import models
from app.services import fugas_fiscales as ff


def _div(*, cartera, broker, posicion, fecha, bruto, retencion, pais=None):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"), divisa_local="EUR",
        importe_local=Decimal(str(bruto)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(bruto)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal(str(retencion)),
        retencion_pais=pais, estado="confirmada", origen="extracto",
    )


def _buy(*, cartera, broker, posicion, cantidad):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=date(2025, 1, 2), tipo="BUY",
        cantidad=Decimal(str(cantidad)), precio_local=Decimal("10"),
        divisa_local="EUR", importe_local=Decimal(str(cantidad)) * 10,
        fx_rate=Decimal("1"), importe_eur=Decimal(str(cantidad)) * 10,
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        estado="confirmada", origen="extracto",
    )


@pytest.fixture()
def pos_ch(db: Session, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="CH0038863350",
                        nombre="Nestle SA", divisa_local="CHF")
    db.add(p); db.flush()
    return p


@pytest.fixture()
def pos_us(db: Session, cartera) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin="US5949181045",
                        nombre="Microsoft Corp", divisa_local="USD")
    db.add(p); db.flush()
    return p


class _Calc:
    def __init__(self, isin: str, yld: Decimal | None):
        self.isin = isin
        self.div_yield_pct = yld


def _sin_proyeccion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anula la parte de proyección (estimaciones/precios) para aislar el YTD."""
    from app.services import estimaciones, precios
    monkeypatch.setattr(estimaciones, "calcular_estimaciones", lambda db, cid: [])
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid: ({}, None))


def test_exceso_real_ch(db, cartera, broker_degiro, pos_ch, monkeypatch) -> None:
    """Suiza: bruto 100, retención 35 → tope CDI 15% ⇒ fuga real 20."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                fecha=date(date.today().year, 4, 1), bruto=100, retencion=35,
                pais="CH"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    p = r.por_pais[0]
    assert p.pais == "CH"
    assert "90" in p.mecanismo  # formulario 90 ESTV (residentes ES; el 85 era errata)
    assert p.reclamable_pendiente_eur == Decimal("20.00")
    assert r.total_reclamable_pendiente_eur == Decimal("20.00")
    assert p.posiciones[0].exceso_real_total_eur == Decimal("20.00")
    a = p.anios[0]
    assert a.ejercicio == date.today().year and a.dentro_plazo and not a.reclamado


def test_retencion_es_no_es_fuga(db, cartera, broker_degiro, pos_ch,
                                 monkeypatch) -> None:
    """La retención española es crédito 0591, nunca fuga."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                fecha=date(date.today().year, 4, 1), bruto=100, retencion=19,
                pais="ES"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    assert r.por_pais == []
    assert r.total_reclamable_pendiente_eur == Decimal("0.00")


def test_us_dentro_de_tope_sin_fuga(db, cartera, broker_degiro, pos_us,
                                    monkeypatch) -> None:
    """US con W-8BEN (15% == tope CDI) → sin exceso real ni proyectado."""
    _sin_proyeccion(monkeypatch)
    db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_us,
                fecha=date(date.today().year, 3, 1), bruto=100, retencion=15,
                pais="US"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    assert r.por_pais == []


def test_proyeccion_anual_ch(db, cartera, broker_degiro, pos_ch,
                             monkeypatch) -> None:
    """Proyección: 100 acc × 10 € × yield 3% × exceso CH (35−15 = 20%) = 6 €."""
    from app.services import estimaciones, precios
    db.add(_buy(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                cantidad=100))
    db.commit()
    from app.services.fifo import rebuild_for_posicion
    rebuild_for_posicion(db, pos_ch.id)
    db.commit()
    monkeypatch.setattr(
        estimaciones, "calcular_estimaciones",
        lambda db, cid: [_Calc("CH0038863350", Decimal("0.03"))])
    monkeypatch.setattr(
        precios, "obtener_precios_eur",
        lambda db, cid: ({"CH0038863350": Decimal("10")}, None))
    r = ff.calcular_fugas(db, cartera.id)
    assert r.total_fuga_anual_estimada_eur == Decimal("6.00")
    p = r.por_pais[0]
    assert p.pais == "CH"
    x = p.posiciones[0]
    assert x.div_anual_estimado_eur == Decimal("30.00")
    assert x.fuga_anual_estimada_eur == Decimal("6.00")


def test_endpoint_fugas_no_choca_con_ruta_dinamica(monkeypatch) -> None:
    """Regresión: /fiscal/fugas se registró DESPUÉS de /fiscal/{ejercicio} y
    FastAPI intentaba parsear "fugas" como año → 422. El endpoint debe
    resolver a la ruta estática (no 422)."""
    from collections.abc import Generator  # noqa: F401
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import get_db
    from app.db.base import Base
    from app.main import app

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            r = client.get("/api/fiscal/fugas")
            assert r.status_code == 404          # sin cartera, pero NO 422
            with SessionLocal() as s:
                user = models.User(email="f@cima.local", modo="owner")
                s.add(user); s.flush()
                s.add(models.Cartera(user_id=user.id, nombre="Test"))
                s.commit()
            r = client.get("/api/fiscal/fugas")
            assert r.status_code == 200
            assert r.json()["por_pais"] == []
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_out_admite_excesos_con_5_decimales() -> None:
    """Regresión: el exceso alemán (26,375% − 15% = 0.11375) tiene 5 decimales
    y Field(decimal_places=4) lo rechazaba → 500 en /fiscal/fugas."""
    from app.routers.fiscal import FugaPaisOut, FugaPosicionOut

    x = FugaPosicionOut(isin="DE0007164600", nombre="SAP", pais="DE",
                        exceso_pct=Decimal("0.11375"),
                        div_anual_estimado_eur=None, fuga_anual_estimada_eur=None,
                        exceso_real_total_eur=Decimal("0.00"))
    p = FugaPaisOut(pais="DE", exceso_pct=Decimal("0.11375"),
                    fuga_anual_estimada_eur=Decimal("0.00"),
                    reclamable_pendiente_eur=Decimal("0.00"),
                    reclamado_eur=Decimal("0.00"),
                    fuera_plazo_eur=Decimal("0.00"),
                    plazo_anios=4, plazo_verificado=True,
                    mecanismo="BZSt", anios=[], posiciones=[x])
    assert p.exceso_pct == Decimal("0.11375")


def test_ventana_multianio_y_plazos(db, cartera, broker_degiro, pos_ch,
                                    monkeypatch) -> None:
    """CH (plazo 3 años): dividendos de varios años — los de hace ≤3 años son
    reclamables; el de hace 4 está prescrito (fuera_plazo)."""
    _sin_proyeccion(monkeypatch)
    hoy = date.today().year
    for anios_atras, ret in ((0, 35), (2, 35), (3, 35), (4, 35)):
        db.add(_div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
                    fecha=date(hoy - anios_atras, 4, 1), bruto=100,
                    retencion=ret, pais="CH"))
    db.commit()
    r = ff.calcular_fugas(db, cartera.id)
    p = r.por_pais[0]
    assert p.plazo_anios == 3 and p.plazo_verificado
    # 4 años de exceso de 20 €; el de hace 4 años (límite 31-dic hace 1) prescrito
    assert p.reclamable_pendiente_eur == Decimal("60.00")
    assert p.fuera_plazo_eur == Decimal("20.00")
    por_anio = {a.ejercicio: a for a in p.anios}
    assert not por_anio[hoy - 4].dentro_plazo
    assert por_anio[hoy - 3].dentro_plazo   # límite 31-dic de este año
    assert por_anio[hoy - 3].limite == date(hoy, 12, 31)


def test_marcar_reclamado_descuenta(db, cartera, broker_degiro, pos_ch,
                                    monkeypatch) -> None:
    _sin_proyeccion(monkeypatch)
    hoy = date.today().year
    db.add_all([
        _div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
             fecha=date(hoy, 4, 1), bruto=100, retencion=35, pais="CH"),
        _div(cartera=cartera, broker=broker_degiro, posicion=pos_ch,
             fecha=date(hoy - 1, 4, 1), bruto=100, retencion=35, pais="CH"),
    ])
    db.commit()
    ff.marcar_reclamado(db, cartera.id, "CH", hoy - 1, True)
    r = ff.calcular_fugas(db, cartera.id)
    p = r.por_pais[0]
    assert p.reclamable_pendiente_eur == Decimal("20.00")   # solo el año en curso
    assert p.reclamado_eur == Decimal("20.00")
    assert {a.ejercicio: a.reclamado for a in p.anios} == {hoy: False, hoy - 1: True}
    # Desmarcar es idempotente y lo devuelve a pendiente
    ff.marcar_reclamado(db, cartera.id, "CH", hoy - 1, False)
    ff.marcar_reclamado(db, cartera.id, "CH", hoy - 1, False)
    r = ff.calcular_fugas(db, cartera.id)
    assert r.por_pais[0].reclamable_pendiente_eur == Decimal("40.00")
