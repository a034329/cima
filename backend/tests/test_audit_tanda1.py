"""Regresiones de la auditoría Cima 2026-06-11 — Tanda 1 (dinero).

C1 — _coste_medio_ponderado ignoraba CORPORATE_SPLIT → plusvalía 100% falsa.
C2 — la retención ES de dividendos extranjeros (TR Sucursal) entraba al CDI
     (0588) en vez de a la retención española (0591).
C3 — DELETE /transacciones descartaba sin rebuild FIFO → lotes fantasma.
C4 — parse_tr_aportaciones tragaba depósitos ≥1.000 € ('1.234,56').
A1 — _fx_eur cacheaba el tipo de cambio para siempre (sin TTL).
A2 — STAKING_REWARD invisible: sin lote FIFO (venta total → FIFOInsuficiente)
     y sin RCM en intereses/resumen. + casilla 0023→0027 (V1 portado).
A5 — BUY/SELL TR sin transaction_id no se deduplicaban al reimportar.
A6 — el buffer fiscal contaba dos veces la pérdida del año.
A7 — el déficit de la hoja de ruta se calculaba sobre el patrimonio total
     (colchón incluido) en vez del capital en estrategia.

Cada test falla contra el código pre-fix.
"""
from __future__ import annotations

import csv as _csv
import tempfile
import time
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import models


# ── helpers ────────────────────────────────────────────────────────────────

def _tx_buy(cartera, posicion, fecha, qty, importe, broker_id=None):
    return models.Transaccion(
        cartera_id=cartera.id, broker_id=broker_id, posicion_id=posicion.id,
        fecha=fecha, tipo="BUY",
        cantidad=Decimal(str(qty)), precio_local=Decimal("0"),
        divisa_local="EUR", importe_local=Decimal(str(importe)),
        fx_rate=Decimal("1"), importe_eur=Decimal(str(importe)),
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"), estado="confirmada", origen="manual",
    )


def _tx(cartera, posicion, fecha, tipo, qty, importe, **kw):
    t = _tx_buy(cartera, posicion, fecha, qty, importe)
    t.tipo = tipo
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _posicion(db, cartera, isin="ES0000000001", nombre="ACME"):
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre,
                        divisa_local="EUR")
    db.add(p); db.flush()
    return p


# ═══════════════════════════════════════════════════════════════════════════
# C1 — coste medio ponderado con splits
# ═══════════════════════════════════════════════════════════════════════════

def _t(tipo, qty, importe="0", notas=None):
    return SimpleNamespace(tipo=tipo, cantidad=Decimal(str(qty)),
                           importe_eur=Decimal(str(importe)),
                           gastos_eur=Decimal("0"),
                           tasas_externas_eur=Decimal("0"), notas=notas)


def test_c1_venta_parcial_post_split_conserva_coste():
    """Compra 10 a 1.000 → split 2:1 → vende 10 (la mitad): el coste del
    holding restante es 500, no 0 (pre-fix: plusvalía 100% falsa)."""
    from app.services.posiciones import _coste_medio_ponderado
    txs = [
        _t("BUY", 10, "1000"),
        _t("CORPORATE_SPLIT", 0, notas='{"split": {"qty_old": 1, "qty_new": 2}}'),
        _t("SELL", 10),
    ]
    assert _coste_medio_ponderado(txs) == Decimal("500")


def test_c1_contrasplit_tambien_escala():
    from app.services.posiciones import _coste_medio_ponderado
    txs = [
        _t("BUY", 100, "1000"),
        _t("CORPORATE_SPLIT", 0, notas='{"split": {"qty_old": 10, "qty_new": 1}}'),
        _t("SELL", 5),   # vende la mitad de las 10 post-contrasplit
    ]
    assert _coste_medio_ponderado(txs) == Decimal("500")


def test_c1_split_meta_invalida_no_rompe():
    from app.services.posiciones import _coste_medio_ponderado
    txs = [_t("BUY", 10, "1000"),
           _t("CORPORATE_SPLIT", 0, notas="no-json"),
           _t("SELL", 5)]
    assert _coste_medio_ponderado(txs) == Decimal("500")


# ═══════════════════════════════════════════════════════════════════════════
# C2 — retención ES de dividendo extranjero → 0591, no CDI
# ═══════════════════════════════════════════════════════════════════════════

def test_c2_retencion_es_de_dividendo_extranjero_va_a_0591(db: Session, cartera):
    from app.services.fiscal_dividendos import calcular_dividendos
    pos = _posicion(db, cartera, isin="IE000U9J8HX9", nombre="JEPQ")  # ETF irlandés
    # Dividendo TR post-migración: bruto 100, retención 19 ESPAÑOLA.
    db.add(_tx(cartera, pos, date(2025, 7, 9), "DIVIDEND", 0, "100",
               retencion_eur=Decimal("19"), retencion_pais="ES"))
    db.commit()
    r = calcular_dividendos(db, cartera.id, 2025)
    assert Decimal(str(r.ret_es_total)) == Decimal("19"), \
        "la retención ES debe ir a 0591"
    assert Decimal(str(r.cdi_recuperable_total)) == Decimal("0"), \
        "no hay retención en origen: el CDI (0588) debe ser 0"


def test_c2_retencion_en_origen_sigue_en_cdi(db: Session, cartera):
    from app.services.fiscal_dividendos import calcular_dividendos
    pos = _posicion(db, cartera, isin="US4781601046", nombre="JNJ")
    # Retención en ORIGEN (sin retencion_pais='ES'): 15% US → CDI.
    db.add(_tx(cartera, pos, date(2025, 3, 4), "DIVIDEND", 0, "100",
               retencion_eur=Decimal("15"), retencion_pais="US"))
    db.commit()
    r = calcular_dividendos(db, cartera.id, 2025)
    assert Decimal(str(r.cdi_recuperable_total)) == Decimal("15")
    assert Decimal(str(r.ret_es_total)) == Decimal("0")


# ═══════════════════════════════════════════════════════════════════════════
# C3 — descartar transacción rebuildea el FIFO
# ═══════════════════════════════════════════════════════════════════════════

def test_c3_descartar_compra_elimina_lotes(db: Session, cartera):
    from app.routers.transacciones import descartar_transaccion
    from app.services import fifo
    pos = _posicion(db, cartera)
    tx = _tx_buy(cartera, pos, date(2025, 1, 10), 10, "1000")
    db.add(tx); db.flush()
    fifo.rebuild_for_posicion(db, pos.id)
    db.commit()
    assert fifo.estado_posicion(db, pos.id)["cantidad"] == Decimal("10")

    descartar_transaccion(tx.id, db)

    assert fifo.estado_posicion(db, pos.id)["cantidad"] == Decimal("0"), \
        "tras descartar la única compra, la posición no puede conservar lotes"


# ═══════════════════════════════════════════════════════════════════════════
# C4 — aportaciones TR con separador de miles
# ═══════════════════════════════════════════════════════════════════════════

def test_c4_deposito_tr_con_miles_no_se_traga():
    from app.services.aportaciones import parse_tr_aportaciones
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["datetime", "date", "account_type", "category", "type",
                    "asset_class", "name", "symbol", "shares", "price",
                    "amount", "fee", "tax", "currency", "original_amount",
                    "original_currency", "fx_rate", "description",
                    "transaction_id", "counterparty_name",
                    "counterparty_iban", "payment_reference", "mcc_code"])
        w.writerow(["2025-02-01T10:00:00Z", "2025-02-01", "DEFAULT", "CASH",
                    "CUSTOMER_INBOUND", "", "ANGEL", "", "", "", "1.234,56",
                    "", "", "EUR", "", "", "", "Transferencia", "tx-1",
                    "", "", "", ""])
        w.writerow(["2025-03-01T10:00:00Z", "2025-03-01", "DEFAULT", "CASH",
                    "CUSTOMER_INBOUND", "", "ANGEL", "", "", "", "500,25",
                    "", "", "EUR", "", "", "", "Transferencia", "tx-2",
                    "", "", "", ""])
    try:
        out = parse_tr_aportaciones(path, broker_id="b1")
    finally:
        os.unlink(path)
    importes = sorted(a.importe_eur for a in out)
    assert importes == [Decimal("500.25"), Decimal("1234.56")], \
        f"el depósito con separador de miles desaparecía: {importes}"


# ═══════════════════════════════════════════════════════════════════════════
# A1 — el FX caduca (TTL) y sobrevive a un feed caído
# ═══════════════════════════════════════════════════════════════════════════

def test_a1_fx_rancio_se_refresca(monkeypatch):
    import app.services.precios as precios
    cache = {"fx:EURUSD=X": {"valor": 1.5, "ts": time.time() - 30 * 24 * 3600}}
    monkeypatch.setattr(precios, "_precio_y_divisa", lambda par, **k: (1.08, "USD"))
    fac = precios._fx_eur("USD", cache)
    assert fac == Decimal("1") / Decimal("1.08"), \
        "un FX de hace un mes debe refrescarse (TTL 6h)"
    assert cache["fx:EURUSD=X"]["valor"] == 1.08


def test_a1_fx_fresco_no_refetchea(monkeypatch):
    import app.services.precios as precios
    cache = {"fx:EURUSD=X": {"valor": 1.10, "ts": time.time()}}
    def _boom(par, **k):
        raise AssertionError("no debe ir a la red con caché fresca")
    monkeypatch.setattr(precios, "_precio_y_divisa", _boom)
    assert precios._fx_eur("USD", cache) == Decimal("1") / Decimal("1.10")


def test_a1_fx_feed_caido_conserva_el_rancio(monkeypatch):
    import app.services.precios as precios
    cache = {"fx:EURUSD=X": {"valor": 1.10, "ts": time.time() - 30 * 24 * 3600}}
    monkeypatch.setattr(precios, "_precio_y_divisa", lambda par, **k: None)
    assert precios._fx_eur("USD", cache) == Decimal("1") / Decimal("1.10"), \
        "mejor FX viejo que posición sin valorar"


# ═══════════════════════════════════════════════════════════════════════════
# A2 — staking: lote FIFO + RCM 0027
# ═══════════════════════════════════════════════════════════════════════════

def test_a2_staking_crea_lote_y_la_venta_total_no_falla(db: Session, cartera):
    from app.services import fifo
    pos = _posicion(db, cartera, isin="XF000SOL0012", nombre="SOL")
    db.add(_tx_buy(cartera, pos, date(2025, 2, 1), 10, "1000"))
    db.add(_tx(cartera, pos, date(2025, 3, 15), "STAKING_REWARD", 2, "200"))
    db.add(_tx(cartera, pos, date(2025, 9, 1), "SELL", 12, "1500"))
    db.flush()
    res = fifo.rebuild_for_posicion(db, pos.id)
    db.commit()
    assert res.avisos == [], f"la venta total no debe quedar huérfana: {res.avisos}"
    assert fifo.estado_posicion(db, pos.id)["cantidad"] == Decimal("0")


def test_a2_staking_suma_al_rcm_de_intereses_con_0027(db: Session, cartera):
    from app.services.fiscal_intereses import calcular_intereses
    pos = _posicion(db, cartera, isin="XF000SOL0012", nombre="SOL")
    db.add(_tx(cartera, pos, date(2025, 3, 15), "STAKING_REWARD", 2, "5.23"))
    db.commit()
    r = calcular_intereses(db, cartera.id, 2025)
    assert r.rcm_total == Decimal("5.23"), "el staking es RCM (V1766-22)"
    assert any(l.tipo == "staking" and l.casilla == "0027" for l in r.lineas)


def test_a2_casilla_legacy_0023_se_normaliza_a_0027(db: Session, cartera):
    """V1 portado: notas antiguas con casilla '0023' deben leerse como 0027."""
    import json
    from app.services.fiscal_intereses import calcular_intereses
    pos = _posicion(db, cartera, isin="IE000XXXXXX1", nombre="CASH")
    db.add(_tx(cartera, pos, date(2025, 7, 1), "INTEREST", 0, "10",
               notas=json.dumps({"interes": {"tipo": "credit", "casilla": "0023",
                                             "descripcion": "x", "divisa": "EUR"}})))
    db.commit()
    r = calcular_intereses(db, cartera.id, 2025)
    assert all(l.casilla != "0023" for l in r.lineas)
    assert any(l.casilla == "0027" for l in r.lineas)


# ═══════════════════════════════════════════════════════════════════════════
# A5 — BUY TR sin transaction_id obtiene external_id sintético
# ═══════════════════════════════════════════════════════════════════════════

def test_a5_buy_tr_sin_tx_id_lleva_external_id_sintetico():
    from app.adapters.cuadrate import parse_tr_csv
    header = ('"datetime","date","account_type","category","type","asset_class",'
              '"name","symbol","shares","price","amount","fee","tax","currency",'
              '"original_amount","original_currency","fx_rate","description",'
              '"transaction_id","counterparty_name","counterparty_iban",'
              '"payment_reference","mcc_code"')
    row = ('"2025-02-03T05:45:09.061Z","2025-02-03","DEFAULT","TRADING","BUY","STOCK",'
           '"Apple","US0378331005","10","100.00","-1000.00","-1.00","","EUR",'
           '"","","","Buy trade US0378331005 Apple","","","","",""')   # sin tx_id
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(header + "\n" + row + "\n")
    try:
        cands = parse_tr_csv(path, broker_id="b1")
    finally:
        os.unlink(path)
    buys = [c for c in cands if c.tipo == "BUY"]
    assert len(buys) == 1
    assert buys[0].external_id, \
        "sin external_id la dedup no corre y cada re-import duplica la compra"


# ═══════════════════════════════════════════════════════════════════════════
# A6 — el buffer no cuenta dos veces la pérdida del año
# ═══════════════════════════════════════════════════════════════════════════

def test_a6_perdida_del_anio_no_se_duplica_en_buffer(db: Session, cartera, monkeypatch):
    import app.services.precios as precios
    from app.services import fifo
    from app.services.fiscal_contexto import calcular_contexto
    monkeypatch.setattr(precios, "obtener_precios_eur", lambda db, cid, *a, **k: ({}, []))
    pos = _posicion(db, cartera)
    ej = date.today().year
    db.add(_tx_buy(cartera, pos, date(ej - 2, 1, 10), 10, "1000"))
    db.add(_tx(cartera, pos, date(ej, 2, 1), "SELL", 10, "600"))  # pérdida −400 este año
    db.flush()
    fifo.rebuild_for_posicion(db, pos.id)
    db.commit()
    c = calcular_contexto(db, cartera.id, ej)
    assert c.buffer_disponible_eur == Decimal("400.00"), \
        f"la pérdida del año se contaba dos veces (esperado 400): {c.buffer_disponible_eur}"


# ═══════════════════════════════════════════════════════════════════════════
# A7 — déficit de la hoja de ruta sobre el capital en estrategia
# ═══════════════════════════════════════════════════════════════════════════

def test_a7_colchon_fuera_de_estrategia_no_infla_deficits(db: Session, cartera, monkeypatch):
    import app.services.precios as precios
    from app.services.hoja_ruta import analizar_deficit
    bloque = models.Bloque(cartera_id=cartera.id, nombre="Compounders",
                           categoria_base="growth", orden=1, es_base=True,
                           peso_objetivo=Decimal("1.0"))
    colchon = models.Bloque(cartera_id=cartera.id, nombre="Colchón",
                            categoria_base="colchon", orden=2, es_base=True,
                            en_estrategia=False, peso_objetivo=None)
    db.add_all([bloque, colchon]); db.flush()

    def _pos_con_lote(isin, bloque_id, coste):
        p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=isin,
                            divisa_local="EUR", bloque_id=bloque_id)
        db.add(p); db.flush()
        db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                          cantidad_inicial=Decimal("10"),
                          cantidad_restante=Decimal("10"),
                          coste_unit_eur=Decimal(str(coste / 10)),
                          coste_total_eur=Decimal(str(coste)),
                          gastos_eur=Decimal("0")))
        return p

    _pos_con_lote("US_G00000001", bloque.id, 7000)
    _pos_con_lote("IE_F00000001", colchon.id, 3000)
    db.commit()
    monkeypatch.setattr(precios, "obtener_precios_eur",
                        lambda db, cid, *a, **k: ({"US_G00000001": Decimal("700"),
                                                   "IE_F00000001": Decimal("300")}, []))
    gaps, total = analizar_deficit(db, cartera.id)
    assert total == Decimal("7000"), \
        f"la base debe ser el capital EN ESTRATEGIA, no el patrimonio: {total}"
    g = next(x for x in gaps if x.categoria_base == "growth")
    assert abs(g.deficit_eur) < 1.0, \
        f"con objetivo 100% y todo el capital de estrategia en el bloque, déficit ≈ 0 (pre-fix ≈ +3.000): {g.deficit_eur}"
