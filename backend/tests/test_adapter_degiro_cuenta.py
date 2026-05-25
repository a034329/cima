"""Tests del parser DEGIRO Cuenta (dividendos + retenciones).

`parse_degiro_cuenta_csv` envuelve `parse_degiro_cuenta` de Cuádrate
(que filtra por EJERCICIO global) iterando años para producir un resultado
multi-año en una sola llamada. Reagrupa DIV+RET por (isin, fecha) en una
sola fila `DIVIDEND` con retención.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.cuadrate import (
    broker_tipo_db,
    brokers_soportados,
    parse_degiro_cuenta_csv,
)
from app.services.transacciones import TxCandidata


DG_CUENTA = Path("/app/720/irpf/DeGiro_Cuenta_2025.csv")


pytestmark = pytest.mark.skipif(
    not DG_CUENTA.is_file(),
    reason=f"CSV DEGIRO Cuenta no presente en {DG_CUENTA}",
)


# ── Shape ─────────────────────────────────────────────────────────────────


def test_devuelve_lista_de_txcandidata() -> None:
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    assert isinstance(cands, list)
    assert len(cands) > 0
    assert all(isinstance(c, TxCandidata) for c in cands)


def test_todas_tipo_DIVIDEND() -> None:
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    tipos = {c.tipo for c in cands}
    assert tipos == {"DIVIDEND"}


def test_cantidad_y_precio_cero_para_dividendos() -> None:
    """Los dividendos no llevan unidades de acciones — `cantidad` y
    `precio_local` deben ser 0 (no entran al FIFO)."""
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    for c in cands:
        assert c.cantidad == Decimal("0"), (
            f"Dividendo con cantidad={c.cantidad} ≠ 0 en {c.isin}"
        )
        assert c.precio_local == Decimal("0")


def test_importe_eur_positivo() -> None:
    """Dividendos cobrados → importe positivo. Las correcciones/reversiones
    pueden venir negativas pero deben ser minoría."""
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    positivos = [c for c in cands if c.importe_eur > 0]
    assert len(positivos) >= len(cands) * 0.95, (
        "Más de un 5% de dividendos con importe ≤ 0 — algo raro"
    )


def test_external_id_deterministicos_para_dedup() -> None:
    """Reimportar dos veces debe producir los mismos external_id para que
    `(broker_id, external_id)` deduplique."""
    cands1 = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    cands2 = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    ids1 = sorted(c.external_id for c in cands1)
    ids2 = sorted(c.external_id for c in cands2)
    assert ids1 == ids2
    # Y todos son únicos dentro del lote
    assert len(set(ids1)) == len(ids1)


def test_retencion_es_marcada_correctamente() -> None:
    """Para dividendos de valores españoles (ES*), la retención debería
    venir etiquetada con país 'ES'."""
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    es_con_ret = [
        c for c in cands
        if c.isin.startswith("ES") and c.retencion_eur > 0
    ]
    if es_con_ret:
        # Al menos uno debería estar marcado retención_pais=ES
        marcados = [c for c in es_con_ret if c.retencion_pais == "ES"]
        assert len(marcados) > 0, (
            "Dividendos ES con retención pero ninguno marcado retencion_pais=ES"
        )


def test_retenciones_extranjeras_marcadas_con_algun_pais() -> None:
    """Dividendos extranjeros con retención deben tener `retencion_pais` no
    nulo y un código ISO de 2 letras. No siempre es el país del ISIN: los
    ADRs US (ISIN US*) emitidos sobre empresas extranjeras llevan la
    retención del país de origen (ej. TSM ADR US8740391003 → pais TW)."""
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    con_ret = [c for c in cands if c.retencion_eur > 0]
    assert len(con_ret) > 0
    for c in con_ret:
        assert c.retencion_pais is not None, (
            f"Dividendo {c.isin} con retención pero retencion_pais=None"
        )
        assert len(c.retencion_pais) == 2, (
            f"retencion_pais {c.retencion_pais!r} no es código ISO 2 letras"
        )


def test_suma_brutos_y_retenciones_coherentes() -> None:
    """Suma de brutos y retenciones debe coincidir aproximadamente con lo
    que Cuádrate calcularía para 2025 (lo único en este CSV)."""
    cands = parse_degiro_cuenta_csv(DG_CUENTA, broker_id="dg-test")
    bruto_total = sum(c.importe_eur for c in cands)
    ret_total = sum(c.retencion_eur for c in cands)
    # Sanity checks razonables — no exacto pero rango plausible
    assert bruto_total > Decimal("100"), f"Bruto total {bruto_total} demasiado bajo"
    assert ret_total >= 0
    assert ret_total <= bruto_total * Decimal("0.5"), (
        f"Retenciones {ret_total} > 50% del bruto {bruto_total} — sospechoso"
    )


# ── Dispatch ───────────────────────────────────────────────────────────────


def test_degiro_cuenta_aparece_en_brokers_soportados() -> None:
    assert "degiro_cuenta" in brokers_soportados()


def test_broker_tipo_db_mapea_cuenta_a_degiro() -> None:
    """El endpoint resuelve `degiro_cuenta` → broker `degiro` para que ambos
    formatos compartan la misma entidad Broker en BD."""
    assert broker_tipo_db("degiro_cuenta") == "degiro"
    assert broker_tipo_db("degiro") == "degiro"
    assert broker_tipo_db("tr") == "tr"
    assert broker_tipo_db("ibkr") == "ibkr"
