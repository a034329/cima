"""Tests del optimizador fiscal de cierre de año (tax-loss harvesting)."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services.fiscal_optimizador import calcular_optimizador
from app.services.precios import obtener_precios_eur


def _pos(db, cartera, isin, nombre) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    return p


def _lote(db, pos, qty, coste) -> None:
    db.add(models.Lot(
        posicion_id=pos.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))


def _tx(db, cartera, pos, fecha, tipo, qty, importe) -> None:
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=None, posicion_id=pos.id, fecha=fecha,
        tipo=tipo, cantidad=Decimal(str(qty)), precio_local=Decimal("0"),
        divisa_local="EUR", importe_local=Decimal(str(importe)), fx_rate=Decimal("1"),
        importe_eur=Decimal(str(importe)), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual", external_id=f"{tipo}-{pos.isin}-{fecha}",
    ))


def test_optimizador_realizada_cosechable_y_2m(db: Session, cartera) -> None:
    # Posición CERRADA en 2025 con plusvalía +500 (BUY 1000 → SELL 1500).
    cerrada = _pos(db, cartera, "US_CERR", "Cerrada")
    _tx(db, cartera, cerrada, date(2025, 1, 10), "BUY", 10, 1000)
    _tx(db, cartera, cerrada, date(2025, 6, 10), "SELL", 10, 1500)
    # Posición ABIERTA con pérdida latente, SIN compra reciente → cosechable.
    ab = _pos(db, cartera, "US_AB", "Abierta")
    _lote(db, ab, 10, 1000)        # PM 100
    # Posición ABIERTA con pérdida latente PERO compra reciente → bloqueo 2M.
    bloq = _pos(db, cartera, "US_BLOQ", "Bloqueada")
    _lote(db, bloq, 10, 1000)
    _tx(db, cartera, bloq, date.today() - timedelta(days=10), "BUY", 1, 100)
    db.commit()

    # Precios inyectados: ambas abiertas a 70 (pérdida −300 cada una).
    precios = {"US_AB": Decimal("70"), "US_BLOQ": Decimal("70")}
    r = calcular_optimizador(db, cartera.id, 2025, precios=precios, no_resueltos=[])

    # Bolsas de AÑOS ANTERIORES = 0 (no hay pérdidas previas); la pérdida nueva
    # de este año NO debe contarse como "años anteriores".
    assert r.bolsas_pendientes == Decimal("0")
    assert r.gp_realizada_ytd == Decimal("500")        # plusvalía realizada YTD
    # Solo la abierta sin 2M cuenta como cosechable.
    assert r.perdida_latente_cosechable == Decimal("-300")
    # compensable = min(realizada+, |cosechable|) = min(500, 300)
    assert r.compensable_ahora == Decimal("300")
    # La bloqueada aparece con flag 2M y NO suma a cosechable.
    bl = [x for x in r.latentes if x.isin == "US_BLOQ"][0]
    assert bl.bloqueo_2m is True
    assert bl.es_perdida is True


def test_perdidas_manuales_son_autoritativas(db: Session, cartera) -> None:
    from app.services import perdidas
    from app.services.fiscal import calcular_fiscal
    # Sin matches → auto-detect vacío. Una pérdida manual de 2024 debe aparecer.
    perdidas.set_perdida(db, cartera.id, 2024, Decimal("3000"))
    f = calcular_fiscal(db, cartera.id, 2026)
    prev = f.resultado_compensacion.perdidas_anteriores
    assert any(p.ejercicio_origen == 2024 and p.detalle == "manual" for p in prev)
    # listar + borrar
    assert len(perdidas.listar(db, cartera.id)) == 1
    perdidas.set_perdida(db, cartera.id, 2024, None)
    assert perdidas.listar(db, cartera.id) == []


def test_precio_manual_override_sin_red(db: Session, cartera) -> None:
    # Con precio manual fijado, obtener_precios_eur NO necesita red.
    p = _pos(db, cartera, "US_AB", "Abierta")
    _lote(db, p, 10, 1000)
    p.precio_manual_eur = Decimal("88.50")
    db.commit()
    precios, no = obtener_precios_eur(db, cartera.id)
    assert precios["US_AB"] == Decimal("88.50")
    assert no == []
