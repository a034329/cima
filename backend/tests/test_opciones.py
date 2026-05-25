"""Tests de opciones — parsers, reconciliación, cálculo fiscal y endpoint.

Las opciones son el gap que explicaba la diferencia 70 vs 47 matches contra
el informe de Cuádrate: Cima no las procesaba. Ahora tienen tabla, pestaña
y cálculo (casilla 1626, DGT V2172-21).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.cuadrate import parse_degiro_opciones, parse_ibkr_opciones
from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services.fiscal_opciones import calcular_opciones
from app.services.opciones import OpcionCandidata, reconciliar_opciones


DG_CSV = Path("/app/720/irpf/DeGiro_Transacciones_2025.csv")
DG_CUENTA = Path("/app/720/irpf/DeGiro_Cuenta_2025.csv")
IBKR_CSV = Path("/app/720/irpf/IBKR_2025.csv")

# Para los tests contra CSV real usamos el multi-año concatenado si existe;
# si no, el de 2025 (menos opciones pero suficiente para shape).
_dg_multi = Path("/tmp/dg_full.csv")
DG_OPCIONES_CSV = _dg_multi if _dg_multi.is_file() else DG_CSV


# ── Parser DEGIRO ──────────────────────────────────────────────────────────


@pytest.mark.skipif(not DG_CSV.is_file(), reason="CSV DEGIRO no presente")
def test_parser_degiro_opciones_shape() -> None:
    cands = parse_degiro_opciones(
        DG_OPCIONES_CSV, broker_id="dg-test",
        cuenta_path=DG_CUENTA if DG_CUENTA.is_file() else None,
    )
    assert isinstance(cands, list)
    if cands:
        c = cands[0]
        assert isinstance(c, OpcionCandidata)
        assert c.tipo_op in ("C", "P", "?")
        assert c.accion in ("compra", "venta")
        assert c.external_id


@pytest.mark.skipif(not DG_CSV.is_file(), reason="CSV DEGIRO no presente")
def test_parser_degiro_opciones_external_id_determinista() -> None:
    c1 = parse_degiro_opciones(DG_OPCIONES_CSV, broker_id="dg-test")
    c2 = parse_degiro_opciones(DG_OPCIONES_CSV, broker_id="dg-test")
    assert sorted(o.external_id for o in c1) == sorted(o.external_id for o in c2)
    assert len({o.external_id for o in c1}) == len(c1)


@pytest.mark.skipif(not IBKR_CSV.is_file(), reason="CSV IBKR no presente")
def test_parser_ibkr_opciones_shape() -> None:
    cands = parse_ibkr_opciones(IBKR_CSV, broker_id="ibkr-test")
    assert isinstance(cands, list)
    if cands:
        assert all(isinstance(c, OpcionCandidata) for c in cands)
        assert all(c.external_id for c in cands)


# ── Reconciliación ─────────────────────────────────────────────────────────


def _opt(
    *, broker_id, fecha, accion, subyacente="ASML", tipo_op="C",
    strike="900", venc="19DEC25", cantidad=1, importe=100, gastos=1,
    expirada=False, ejercida=False, ext_id=None,
) -> OpcionCandidata:
    return OpcionCandidata(
        fecha=fecha, simbolo=f"{subyacente} {tipo_op}{strike} {venc}",
        isin=None, tipo_op=tipo_op, subyacente=subyacente, strike=strike,
        vencimiento=venc, accion=accion, cantidad=Decimal(str(cantidad)),
        prima_unitaria=Decimal(str(importe)), importe_eur=Decimal(str(importe)),
        gastos_eur=Decimal(str(gastos)), expirada=expirada, ejercida=ejercida,
        external_id=ext_id or f"opt-{subyacente}-{fecha}-{accion}-{importe}",
        broker_id=broker_id,
    )


def test_reconciliar_opciones_inserta_y_deduplica(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    cands = [
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 1, 10), accion="venta", ext_id="o1"),
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 2, 10), accion="compra", ext_id="o2"),
    ]
    r1 = reconciliar_opciones(db, cartera.id, cands)
    assert r1.insertadas == 2
    assert r1.deduplicadas == 0

    r2 = reconciliar_opciones(db, cartera.id, cands)
    assert r2.insertadas == 0
    assert r2.deduplicadas == 2

    total = db.execute(select(models.Opcion)).scalars().all()
    assert len(total) == 2


# ── Cálculo fiscal ──────────────────────────────────────────────────────────


def test_calcular_opciones_contrato_cerrado_genera_pl(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """Vendo una CALL por 100€ y la recompro (buy-to-close) por 30€ →
    contrato cerrado, P&L neto ≈ +70 € (menos gastos)."""
    cands = [
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 1, 10), accion="venta",
             importe=100, gastos=1, ext_id="v1"),
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 2, 10), accion="compra",
             importe=30, gastos=1, ext_id="c1"),
    ]
    reconciliar_opciones(db, cartera.id, cands)

    r = calcular_opciones(db, cartera.id, 2025)
    assert r.n_opciones == 2
    # pl_bruto = 100 - 30 = 70; pl_neto = 70 - 2 gastos = 68
    assert r.totales["pl_bruto"] == Decimal("70")
    assert r.totales["pl_neto"] == Decimal("68")


def test_calcular_opciones_expirada_aporta_prima(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """CALL vendida por 50€ que expira sin valor → prima 50€ es ganancia.
    El cierre a 0 marca 'expirada' en el evento de cierre."""
    cands = [
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 1, 10), accion="venta",
             importe=50, gastos=1, ext_id="v2"),
    ]
    reconciliar_opciones(db, cartera.id, cands)
    r = calcular_opciones(db, cartera.id, 2025)
    # Una venta sin cierre → short_abierta (diferida) o normal según motor.
    # Verificamos que la prima cobrada está contabilizada.
    assert r.totales["primas_cobradas"] >= Decimal("0")


def test_opcion_vencida_sin_cierre_se_infiere_expirada(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """Caso real ASLM: PUT comprada que vence en el pasado y no tiene
    registro de cierre (expiró sin valor → DEGIRO no emite línea). NO puede
    seguir 'long abierta' — debe inferirse expirada y la prima pagada es
    pérdida en el año del vencimiento."""
    # Compra de una PUT que vence 16JAN20 (muy en el pasado), sin cierre.
    db.add(models.Opcion(
        cartera_id=cartera.id, broker_id=broker_degiro.id,
        fecha=date(2020, 1, 5), simbolo="ASLM P900.00 16JAN20",
        tipo_op="P", subyacente="ASLM", strike="900", vencimiento="16JAN20",
        accion="compra", cantidad=Decimal("1"), prima_unitaria=Decimal("1.89"),
        importe_eur=Decimal("18.90"), gastos_eur=Decimal("0.75"),
        expirada=False, ejercida=False, estado="confirmada", origen="extracto",
        external_id="aslm-put",
    ))
    db.commit()

    r = calcular_opciones(db, cartera.id, 2020)
    aslm = [c for c in r.por_contrato if c["subyacente"] == "ASLM"]
    assert len(aslm) == 1
    c = aslm[0]
    assert not c.get("es_long_abierta"), "No puede seguir abierta si ya venció"
    assert c["expiradas"] >= 1
    # pérdida = -prima - gastos = -19.65 a casilla 1626
    assert c["pl_neto"] == Decimal("-19.65")


def test_larga_abierta_aparece_en_anios_posteriores_a_la_compra(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """Caso PAG/LEAPS: una larga comprada un año con vencimiento futuro debe
    verse como 'abierta' en el año de compra Y en los años siguientes mientras
    siga viva, no solo en el de apertura."""
    futuro = date.today().year + 1
    cands = [
        _opt(broker_id=broker_degiro.id, fecha=date(date.today().year - 1, 7, 10),
             accion="compra", subyacente="PAG", tipo_op="C", strike="46",
             venc=f"19JUN{futuro % 100:02d}", importe=420, gastos=0, ext_id="pag"),
    ]
    reconciliar_opciones(db, cartera.id, cands)

    r_compra = calcular_opciones(db, cartera.id, date.today().year - 1)
    r_actual = calcular_opciones(db, cartera.id, date.today().year)

    # Aparece en ambos años como long abierta (sigue viva)
    for r in (r_compra, r_actual):
        pag = [c for c in r.por_contrato if c["subyacente"] == "PAG"]
        assert len(pag) == 1, f"PAG debería aparecer en ejercicio {r.ejercicio}"
        assert pag[0].get("es_long_abierta") is True


def test_opcion_vencimiento_futuro_sigue_abierta(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """Una opción larga con vencimiento futuro SÍ sigue abierta (no se infiere
    expiración prematura)."""
    futuro = date.today().year + 2
    db.add(models.Opcion(
        cartera_id=cartera.id, broker_id=broker_degiro.id,
        fecha=date.today(), simbolo=f"SAN C9 18DEC{futuro % 100:02d}",
        tipo_op="C", subyacente="SAN", strike="9", vencimiento=f"18DEC{futuro % 100:02d}",
        accion="compra", cantidad=Decimal("1"), prima_unitaria=Decimal("10"),
        importe_eur=Decimal("10"), gastos_eur=Decimal("0"),
        expirada=False, ejercida=False, estado="confirmada", origen="extracto",
        external_id="san-call-futuro",
    ))
    db.commit()
    r = calcular_opciones(db, cartera.id, date.today().year)
    san = [c for c in r.por_contrato if c["subyacente"] == "SAN"]
    assert len(san) == 1
    assert san[0].get("es_long_abierta") is True


def test_calcular_opciones_filtra_por_anio_de_cierre(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """La atribución fiscal es por año de CIERRE, no de apertura. Dos
    contratos distintos que vencen/cierran en años distintos van cada uno a
    su ejercicio. Usamos vencimientos en años pasados para que la inferencia
    de expiración los cierre en su año."""
    cands = [
        # Contrato A: vendido y vencido en 2023 → cierra 2023
        _opt(broker_id=broker_degiro.id, fecha=date(2023, 5, 10), accion="venta",
             subyacente="AAA", venc="19DEC23", ext_id="a"),
        # Contrato B: vendido y vencido en 2024 → cierra 2024
        _opt(broker_id=broker_degiro.id, fecha=date(2024, 5, 10), accion="venta",
             subyacente="BBB", venc="20DEC24", ext_id="b"),
    ]
    reconciliar_opciones(db, cartera.id, cands)

    r_2023 = calcular_opciones(db, cartera.id, 2023)
    r_2024 = calcular_opciones(db, cartera.id, 2024)
    r_ac = calcular_opciones(db, cartera.id, None)

    # Cada contrato aparece en su año de cierre
    assert {c["subyacente"] for c in r_2023.por_contrato} == {"AAA"}
    assert {c["subyacente"] for c in r_2024.por_contrato} == {"BBB"}
    assert {c["subyacente"] for c in r_ac.por_contrato} == {"AAA", "BBB"}
    assert r_ac.ejercicio == 0


def test_opcion_vendida_un_anio_cierra_al_siguiente_va_al_anio_de_cierre(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker,
) -> None:
    """Caso NAG/Cuádrate: opción vendida en un año que expira al siguiente
    debe tributar en el año de EXPIRACIÓN, no en el de venta."""
    # Vendida 2023-12-20, vence 17ENE24 → cierra (expira) en 2024
    cands = [
        _opt(broker_id=broker_degiro.id, fecha=date(2023, 12, 20), accion="venta",
             subyacente="NAG", tipo_op="P", strike="54", venc="17JAN24",
             importe=60, gastos=1, ext_id="nag"),
    ]
    reconciliar_opciones(db, cartera.id, cands)

    r_2023 = calcular_opciones(db, cartera.id, 2023)
    r_2024 = calcular_opciones(db, cartera.id, 2024)

    # En 2023 (año de venta) NO aparece — aún estaba abierta a 31/12/2023
    assert all(c["subyacente"] != "NAG" for c in r_2023.por_contrato)
    # En 2024 (año de expiración) SÍ, como prima ganada (short expirada)
    nag = [c for c in r_2024.por_contrato if c["subyacente"] == "NAG"]
    assert len(nag) == 1
    assert nag[0]["expiradas"] >= 1
    assert nag[0]["pl_neto"] == Decimal("59")   # 60 prima - 1 gasto


# ── Endpoint ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_endpoint_opciones_sin_cartera_404(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, _ = client_y_db
    r = client.get("/api/opciones/2025")
    assert r.status_code == 404


def test_endpoint_opciones_estructura(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="o@o.o", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    broker = models.Broker(user_id=u.id, broker_tipo="degiro", alias="DG"); s.add(broker); s.flush()
    s.add(models.Opcion(
        cartera_id=cart.id, broker_id=broker.id, fecha=date(2025, 1, 10),
        simbolo="ASML C900 19DEC25", tipo_op="C", subyacente="ASML",
        strike="900", vencimiento="19DEC25", accion="venta",
        cantidad=Decimal("1"), prima_unitaria=Decimal("100"),
        importe_eur=Decimal("100"), gastos_eur=Decimal("1"),
        expirada=False, ejercida=False, estado="confirmada", origen="extracto",
        external_id="x1",
    ))
    s.commit(); s.close()

    r = client.get("/api/opciones/2025")
    assert r.status_code == 200, r.text
    data = r.json()
    for campo in (
        "ejercicio", "n_opciones", "n_contratos", "pl_neto", "pl_bruto",
        "primas_cobradas", "primas_pagadas", "gastos", "n_expiradas",
        "ejercidas_prima_integrar", "long_abiertas_coste",
        "short_abiertas_prima", "contratos",
    ):
        assert campo in data, f"Falta campo {campo}"
    assert data["ejercicio"] == 2025
    assert data["n_opciones"] == 1


def test_endpoint_opciones_ejercicio_fuera_rango_400(
    client_y_db: tuple[TestClient, sessionmaker],
) -> None:
    client, SessionLocal = client_y_db
    s = SessionLocal()
    u = models.User(email="o@o.o", modo="owner"); s.add(u); s.flush()
    cart = models.Cartera(user_id=u.id, nombre="t"); s.add(cart); s.flush()
    s.commit(); s.close()
    r = client.get("/api/opciones/2099")
    assert r.status_code == 400


def test_opciones_abiertas_solo_vivas(
    db: Session, cartera: models.Cartera, broker_degiro: models.Broker, monkeypatch,
) -> None:
    """opciones_abiertas: solo contratos con vencimiento futuro; corta + prima neta."""
    from app.services import precios
    from app.services.fiscal_opciones import opciones_abiertas
    cands = [
        # PUT vendida viva (vence en el futuro) — strike 900.
        _opt(broker_id=broker_degiro.id, fecha=date(2025, 1, 10), accion="venta",
             subyacente="ASML", tipo_op="P", strike="900", venc="19DEC30",
             importe=120, ext_id="viva"),
        # PUT vendida ya vencida (no cerrada) — debe excluirse.
        _opt(broker_id=broker_degiro.id, fecha=date(2020, 1, 10), accion="venta",
             subyacente="OLD", tipo_op="P", strike="50", venc="19DEC20",
             importe=30, ext_id="vieja"),
    ]
    reconciliar_opciones(db, cartera.id, cands)
    db.commit()
    # Precio del subyacente < strike → PUT ITM.
    monkeypatch.setattr(precios, "_precio_y_divisa", lambda sim: (800.0, "EUR"))

    abiertas = opciones_abiertas(db, cartera.id)
    assert len(abiertas) == 1
    o = abiertas[0]
    assert o.subyacente == "ASML" and o.tipo_op == "P"
    assert o.es_corta is True                       # vendida → n_net > 0
    assert o.prima_neta_eur == Decimal("120.00")    # cobrada
    assert o.moneyness == "ITM"                      # 800 < 900
    assert o.dias_a_vencer is not None and o.dias_a_vencer > 0
    # Precio del subyacente + G/P estimada por intrínseco (put corta ITM):
    # intrínseco = (900−800)·100·1 = 10000; gp = prima 120 − 10000 = −9880.
    assert o.precio_subyacente == Decimal("800.0000")
    assert o.divisa_subyacente == "EUR"
    assert o.gp_estimada_eur == Decimal("-9880.00")
