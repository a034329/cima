"""Tests del adapter DEGIRO contra el CSV real del propio Cuádrate.

Estos tests garantizan que el adapter (`app.adapters.cuadrate.parse_degiro_csv`)
mantiene el shape `TxCandidata` y sigue casando con `parse_degiro` de Cuádrate
si su contrato cambia.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.cuadrate import parse_degiro_csv
from app.services.transacciones import TxCandidata


# Fichero real de Cuádrate. Si no existe en el entorno (CI sin acceso al
# repo de Cuádrate), saltamos los tests; la lógica del adapter está cubierta
# por los unit tests de tipo abajo.
DG_CSV = Path("/app/720/irpf/DeGiro_Transacciones_2025.csv")
DG_CUENTA = Path("/app/720/irpf/DeGiro_Cuenta_2025.csv")


pytestmark = pytest.mark.skipif(
    not DG_CSV.is_file(),
    reason=f"Fichero DEGIRO no presente en {DG_CSV}",
)


# ── Shape ─────────────────────────────────────────────────────────────────

def test_devuelve_lista_de_txcandidata() -> None:
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    assert isinstance(cands, list)
    assert len(cands) > 0
    assert all(isinstance(c, TxCandidata) for c in cands)


def test_tipos_permitidos_sin_cuenta() -> None:
    """Sin cuenta_path → BUY/SELL/CORPORATE_SPLIT. Sin DIVIDEND (eso es del
    CSV de cuenta)."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    tipos = {c.tipo for c in cands}
    assert tipos.issubset({"BUY", "SELL", "CORPORATE_SPLIT"})


def test_cantidad_positiva_y_precio_coherente() -> None:
    """Cantidad nunca negativa; precio_local ~= importe_eur / cantidad."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    for c in cands:
        assert c.cantidad > 0, f"Cantidad no positiva en {c.isin} {c.fecha}"
        assert c.importe_eur >= 0
        # precio_local recompuesto desde importe/cantidad — tolerar 0.01 EUR
        precio_esperado = (c.importe_eur / c.cantidad).quantize(Decimal("0.0001"))
        assert abs(c.precio_local - precio_esperado) < Decimal("0.0002"), (
            f"Precio incoherente en {c.isin}: {c.precio_local} vs {precio_esperado}"
        )


def test_broker_id_propaga() -> None:
    cands = parse_degiro_csv(DG_CSV, broker_id="mi-broker-id-xyz")
    assert all(c.broker_id == "mi-broker-id-xyz" for c in cands)


def test_external_id_es_order_id_uuid_mayoritariamente() -> None:
    """La mayoría de filas DEGIRO tienen Order ID UUID. Hay excepciones
    legítimas: corto forzado intra-broker (§3.9 patrones_degiro.md), scrip
    dividends sin UUID, M&A processing y filas legacy 2017-2018. Tolerar
    hasta un 15% sin order_id."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    con_ext = sum(1 for c in cands if c.external_id)
    ratio = con_ext / len(cands)
    assert ratio > 0.80, (
        f"Sólo {con_ext}/{len(cands)} ({ratio:.0%}) tienen external_id — "
        f"baja inusual; revisar regex _RE_DEGIRO_ORDER_ID o formato del CSV"
    )


def test_external_id_vacio_se_normaliza_a_none() -> None:
    """`_order_id` puede venir como '' del parser de Cuádrate. Debe
    normalizarse a None para que la dedup por (broker_id, external_id) no
    trate cadenas vacías como colisionables."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    for c in cands:
        assert c.external_id is None or c.external_id != "", (
            f"external_id vacío sin normalizar en {c.isin}"
        )


def test_external_ids_son_unicos() -> None:
    """Crítico para dedup: el (broker_id, external_id) debe ser único."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    ids = [c.external_id for c in cands if c.external_id]
    assert len(ids) == len(set(ids)), "external_ids duplicados — rompería dedup"


def test_divisa_local_eur_v1() -> None:
    """v1: DEGIRO entrega importe en EUR; native currency pendiente."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    assert all(c.divisa_local == "EUR" for c in cands)
    assert all(c.fx_rate == Decimal("1") for c in cands)


def test_isin_y_fecha_validos() -> None:
    from datetime import date as _date
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    for c in cands:
        assert len(c.isin) == 12, f"ISIN mal formado: {c.isin!r}"
        assert isinstance(c.fecha, _date)


# ── Con cuenta CSV (tasas externas) ────────────────────────────────────────

@pytest.mark.skipif(
    not DG_CUENTA.is_file(),
    reason=f"Fichero DEGIRO cuenta no presente en {DG_CUENTA}",
)
def test_cuenta_path_inyecta_tasas_externas() -> None:
    """Sin cuenta_path, tasas_externas_eur=0 en todas. Con cuenta_path, varias > 0."""
    sin_cuenta = parse_degiro_csv(DG_CSV, broker_id="dg-test")
    # Filtramos a BUY/SELL (los CORPORATE_SPLIT tampoco llevan tasas)
    sin_cuenta_trades = [c for c in sin_cuenta if c.tipo in ("BUY", "SELL")]
    assert all(c.tasas_externas_eur == 0 for c in sin_cuenta_trades)

    con_cuenta = parse_degiro_csv(DG_CSV, broker_id="dg-test", cuenta_path=DG_CUENTA)
    with_fees = [
        c for c in con_cuenta
        if c.tipo in ("BUY", "SELL") and c.tasas_externas_eur > 0
    ]
    assert len(with_fees) > 0, "Esperábamos al menos 1 fila con tasas externas (UK/FR)"

    # Con cuenta_path se añaden DIVIDEND adicionales del CSV de cuenta, así
    # que el total cambia. Las BUY/SELL/SPLIT siguen siendo las mismas.
    sin_count_trades = len([c for c in sin_cuenta if c.tipo in ("BUY", "SELL", "CORPORATE_SPLIT")])
    con_count_trades = len([c for c in con_cuenta if c.tipo in ("BUY", "SELL", "CORPORATE_SPLIT")])
    assert sin_count_trades == con_count_trades
    # Y al añadir cuenta, deben aparecer dividendos
    assert any(c.tipo == "DIVIDEND" for c in con_cuenta)


@pytest.mark.skipif(
    not DG_CUENTA.is_file(),
    reason=f"Fichero DEGIRO cuenta no presente en {DG_CUENTA}",
)
def test_uk_stamp_duty_aparece_en_isin_gb() -> None:
    """Stamp Duty del 0.5% UK debería aparecer en compras de ISINs GB*."""
    cands = parse_degiro_csv(DG_CSV, broker_id="dg-test", cuenta_path=DG_CUENTA)
    compras_gb = [c for c in cands if c.tipo == "BUY" and c.isin.startswith("GB")]
    con_stamp = [c for c in compras_gb if c.tasas_externas_eur > 0]
    assert len(con_stamp) > 0, "Esperábamos UK Stamp Duty en al menos 1 compra GB"
