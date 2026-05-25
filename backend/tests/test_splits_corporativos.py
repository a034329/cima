"""Tests del procesamiento de splits y eventos corporativos.

Reproduce el caso real reportado: posiciones huérfanas en GOOGL, NVDA,
AMZN porque sus splits no se aplicaban a los lots. Tras el fix, el FIFO
local de Cima y el motor fiscal de Cuádrate ven la cantidad correcta
post-split y las ventas dejan de ser huérfanas.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.cuadrate import parse_degiro_csv
from app.db import models
from app.services.fifo import (
    aplicar_split,
    estado_posicion,
    rebuild_for_posicion,
)
from app.services.fiscal import calcular_fiscal
from app.services.transacciones import reconciliar_extracto


DG_CSV = Path("/app/720/irpf/DeGiro_Transacciones_2025.csv")


# ── Adapter: emit splits as CORPORATE_SPLIT ────────────────────────────────


def _mk_full_multi_year_csv(tmp_path: Path) -> Path:
    """Concatena los CSVs por año de Cuádrate en uno multi-año."""
    out = tmp_path / "dg_full.csv"
    header_written = False
    with out.open("w", encoding="utf-8") as fout:
        for year in range(2017, 2026):
            f = Path(f"/app/720/irpf/DeGiro_Transacciones_{year}.csv")
            if not f.is_file():
                continue
            with f.open(encoding="utf-8") as fin:
                header = fin.readline()
                if not header_written:
                    fout.write(header)
                    header_written = True
                fout.write(fin.read())
    return out


@pytest.mark.skipif(
    not DG_CSV.is_file(),
    reason="CSV DEGIRO no presente",
)
def test_adapter_emite_corporate_split_para_splits_detectados(
    tmp_path: Path,
) -> None:
    """En el CSV multi-año del repo, hay 5 splits documentados.
    El adapter debe emitir 5 TxCandidata con tipo='CORPORATE_SPLIT'."""
    csv_path = _mk_full_multi_year_csv(tmp_path)
    cands = parse_degiro_csv(csv_path, broker_id="dg-test")
    splits = [c for c in cands if c.tipo == "CORPORATE_SPLIT"]
    assert len(splits) >= 5, (
        f"Esperábamos al menos 5 splits (NVDA 2021/2024, GOOGL, AMZN, DINO). "
        f"Hay {len(splits)}: {[(s.isin, s.fecha) for s in splits]}"
    )

    # Cada split tiene meta JSON con qty_old + qty_new
    for s in splits:
        meta = json.loads(s.notas)
        assert "split" in meta
        assert "qty_old" in meta["split"]
        assert "qty_new" in meta["split"]
        # cantidad de la TxCandidata = qty_new (post-split)
        assert s.cantidad == Decimal(meta["split"]["qty_new"])


# ── FIFO local: aplicar_split mutates lots correctly ──────────────────────


ISIN_GOOGL = "US02079K3059"


def _tx(
    *, cartera, broker, posicion, fecha, tipo, cantidad, precio, notas=None,
) -> models.Transaccion:
    importe = Decimal(str(cantidad)) * Decimal(str(precio))
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker.id, posicion_id=posicion.id,
        fecha=fecha, tipo=tipo,
        cantidad=Decimal(str(cantidad)),
        precio_local=Decimal(str(precio)), divisa_local="EUR",
        importe_local=importe, fx_rate=Decimal("1"), importe_eur=importe,
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
        notas=notas,
    )


@pytest.fixture()
def pos_googl(db: Session, cartera: models.Cartera) -> models.Posicion:
    p = models.Posicion(
        cartera_id=cartera.id, isin=ISIN_GOOGL,
        nombre="Alphabet Inc Class A", divisa_local="USD",
    )
    db.add(p); db.flush()
    return p


def test_aplicar_split_multiplica_lots(
    db: Session, cartera, broker_degiro, pos_googl,
) -> None:
    """Compra 3 GOOGL @ 1000 → split 20:1 → debe tener 60 @ 50 (mismo coste total)."""
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 1, 10), tipo="BUY", cantidad=3, precio=1000))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 7, 15), tipo="CORPORATE_SPLIT", cantidad=60, precio=0,
               notas=json.dumps({"split": {"qty_old": "3", "qty_new": "60", "nominal_old": "1"}})))
    db.commit()

    # Rebuild aplica BUY y luego split
    rebuild_for_posicion(db, pos_googl.id)

    est = estado_posicion(db, pos_googl.id)
    assert est["cantidad"] == Decimal("60.0000000000"), (
        f"Esperábamos 60 unidades post-split. Got {est['cantidad']}"
    )
    # Coste total preservado: 3 × 1000 = 3000 EUR
    assert est["coste_total_eur"] == Decimal("3000.00")
    # PM ahora 50 €/unidad
    assert est["pm_real_eur"] == Decimal("50.0000")


def test_split_permite_que_la_venta_post_split_no_quede_huerfana(
    db: Session, cartera, broker_degiro, pos_googl,
) -> None:
    """Caso real: 3 GOOGL → split 20:1 → vendo 60. Sin split, los 60 son
    huérfanos; con split, consumen los lots correctamente."""
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 1, 10), tipo="BUY", cantidad=3, precio=1000))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 7, 15), tipo="CORPORATE_SPLIT", cantidad=60, precio=0,
               notas=json.dumps({"split": {"qty_old": "3", "qty_new": "60", "nominal_old": "1"}})))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2024, 3, 18), tipo="SELL", cantidad=60, precio=150))
    db.commit()

    rb = rebuild_for_posicion(db, pos_googl.id)
    # La venta consume todo el inventario post-split
    assert rb.avisos == [], f"Aviso inesperado: {rb.avisos}"

    est = estado_posicion(db, pos_googl.id)
    assert est["cantidad"] == Decimal("0E-10")


# ── Smoke E2E: CSV multi-año real → adapter → BD → fiscal ─────────────────


@pytest.mark.skipif(
    not DG_CSV.is_file(),
    reason="CSV DEGIRO no presente",
)
def test_e2e_csv_multi_anio_no_genera_orphans_en_isins_con_split(
    db: Session, cartera, broker_degiro, tmp_path: Path,
) -> None:
    """End-to-end: importar el CSV multi-año real (con sus splits NVDA,
    GOOGL, AMZN, DINO) y verificar que NO quedan ventas huérfanas en esas
    posiciones tras aplicar splits."""
    csv_path = _mk_full_multi_year_csv(tmp_path)
    cands = parse_degiro_csv(csv_path, broker_id=broker_degiro.id)
    r = reconciliar_extracto(db, cartera.id, "degiro", cands)

    # ISINs con split conocido
    isin_googl = "US02079K3059"
    isin_nvda = "US67066G1040"
    isin_amzn = "US0231351067"

    avisos_orfanos_googl = [
        a for a in r.avisos if "[FIFO]" in a and isin_googl in a
    ]
    avisos_orfanos_nvda = [
        a for a in r.avisos if "[FIFO]" in a and isin_nvda in a
    ]
    avisos_orfanos_amzn = [
        a for a in r.avisos if "[FIFO]" in a and isin_amzn in a
    ]

    # Estos no deberían tener avisos por culpa de splits no aplicados.
    # (Si los hay, sería por OTRO motivo — opening balance pre-2017 u
    # otra anomalía, no por splits.)
    assert avisos_orfanos_googl == [], (
        f"GOOGL sigue con avisos huérfanos: {avisos_orfanos_googl[:2]}"
    )
    assert avisos_orfanos_nvda == [], (
        f"NVDA sigue con avisos huérfanos: {avisos_orfanos_nvda[:2]}"
    )
    assert avisos_orfanos_amzn == [], (
        f"AMZN sigue con avisos huérfanos: {avisos_orfanos_amzn[:2]}"
    )

    # Y el motor fiscal debe producir matches para esas posiciones
    # (al menos uno cada uno, dado que el usuario tiene SELLs históricos).
    r_fiscal = calcular_fiscal(db, cartera.id, None)
    isins_con_matches = {m.isin for m in r_fiscal.matches}
    assert isin_googl in isins_con_matches
    assert isin_nvda in isins_con_matches


# ── Avisos de eventos corporativos no-split ────────────────────────────────


# ── El motor fiscal también ve los splits (no solo el FIFO local) ─────────


def test_calcular_fiscal_recibe_splits_y_no_genera_huerfanas(
    db: Session, cartera, broker_degiro, pos_googl,
) -> None:
    """Bug crítico que el usuario reportó: aunque el FIFO local de Cima
    aplicaba splits, el motor fiscal recibía solo BUY/SELL → veía 3 GOOGL
    y la venta de 60 quedaba como 57 huérfanas en /fiscal/2024.

    Tras el fix, _serializar_operaciones incluye CORPORATE_SPLIT y
    _tx_to_motor_dict lo mapea a 'SP'. El motor procesa correctamente.
    """
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 1, 10), tipo="BUY", cantidad=3, precio=1000))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2022, 7, 15), tipo="CORPORATE_SPLIT", cantidad=60, precio=0,
               notas=json.dumps({"split": {"qty_old": "3", "qty_new": "60", "nominal_old": "1"}})))
    db.add(_tx(cartera=cartera, broker=broker_degiro, posicion=pos_googl,
               fecha=date(2024, 3, 18), tipo="SELL", cantidad=60, precio=150))
    db.commit()

    r = calcular_fiscal(db, cartera.id, 2024)

    # Cero huérfanas (clave del fix)
    assert r.orphan_sales == [], (
        f"Esperábamos 0 huérfanas tras split. Got {len(r.orphan_sales)}: "
        f"{[(o.isin, o.fecha, o.cantidad_faltante) for o in r.orphan_sales]}"
    )

    # Match correcto: 60 unidades vendidas, todas consumieron lots (post-split @ 50€)
    assert r.n_matches == 1
    m = r.matches[0]
    assert m.cantidad == Decimal("60")
    # G/P = 60 × 150 − 60 × 50 = 9000 − 3000 = 6000 (sin gastos)
    assert m.ganancia_perdida == Decimal("6000")


def test_calcular_fiscal_sin_split_si_genera_huerfanas() -> None:
    """Test negativo: SIN split en BD, el motor fiscal reproduce el bug
    reportado (huérfanas). Es la red de seguridad: si alguien retoca
    _serializar_operaciones y deja de pasar splits, este test falla."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db.base import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    user = models.User(email="x@x.x", modo="owner"); db.add(user); db.flush()
    cart = models.Cartera(user_id=user.id, nombre="x"); db.add(cart); db.flush()
    broker = models.Broker(user_id=user.id, broker_tipo="degiro", alias="DG"); db.add(broker); db.flush()
    pos = models.Posicion(cartera_id=cart.id, isin=ISIN_GOOGL, nombre="GOOGL", divisa_local="USD")
    db.add(pos); db.flush()

    db.add(_tx(cartera=cart, broker=broker, posicion=pos,
               fecha=date(2022, 1, 10), tipo="BUY", cantidad=3, precio=1000))
    # SIN tx CORPORATE_SPLIT en BD ← reproduce el escenario pre-fix
    db.add(_tx(cartera=cart, broker=broker, posicion=pos,
               fecha=date(2024, 3, 18), tipo="SELL", cantidad=60, precio=150))
    db.commit()

    r = calcular_fiscal(db, cart.id, 2024)
    assert len(r.orphan_sales) > 0 or any(
        "huérfan" in w.lower() or "sin lotes" in w.lower() for w in r.warnings
    ), "Sin split, esperábamos al menos 1 huérfana — control negativo"


@pytest.mark.skipif(
    not DG_CSV.is_file(),
    reason="CSV DEGIRO no presente",
)
def test_avisos_emitidos_para_spin_offs_rights_y_otros(tmp_path: Path) -> None:
    """El adapter llena `avisos` con descripciones de spin-offs, rights,
    market_transfers, etc. detectados por parse_degiro."""
    csv_path = _mk_full_multi_year_csv(tmp_path)
    avisos: list[str] = []
    parse_degiro_csv(csv_path, broker_id="dg-test", avisos=avisos)

    # Debería haber al menos: 2 spin-offs (Mason, Solventum), 4 rights, 1 corto forzado
    tipos_esperados = ["SPIN_OFF", "RIGHTS asignados", "CORTO_FORZADO", "RIGHTS_EXERCISED"]
    for t in tipos_esperados:
        assert any(t in a for a in avisos), (
            f"Esperábamos aviso del tipo '{t}'. Avisos: {avisos}"
        )
