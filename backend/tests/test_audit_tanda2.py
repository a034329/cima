"""Regresiones de la auditoría Cima 2026-06-11 — Tanda 2 (importación/dedup).

A3  — external_id repetidos dentro del mismo extracto (IBKR sin trade-id,
      rolling de opciones intradía con el mismo OrderID) colapsaban: la
      segunda operación LEGÍTIMA se "deduplicaba" y desaparecía.
A4  — el agrupado de dividendos por (isin, fecha) sobreescribía: dos
      dividendos del mismo día (ordinario+extraordinario, reversal+rebill)
      perdían el primero.
A11 — depósitos IBKR en divisa ≠ EUR se registraban sin conversión.
J5  — re-importar un statement IBKR PARCIAL (ene-jun) sobrescribía el
      realized del año completo ya registrado, en silencio.

Cada test de bug falla contra el código pre-fix.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models


def _broker(db, cartera, tipo="ibkr"):
    b = models.Broker(user_id=cartera.user_id, broker_tipo=tipo, alias=tipo.upper())
    db.add(b); db.flush()
    return b


def _cand(broker_id, isin="US0378331005", fecha=date(2025, 2, 3), tipo="BUY",
          qty="10", importe="1000", ext_id="dup-1"):
    from app.adapters.cuadrate import TxCandidata
    return TxCandidata(
        fecha=fecha, tipo=tipo, isin=isin, nombre="Apple",
        cantidad=Decimal(qty), precio_local=Decimal("100"),
        divisa_local="EUR", importe_local=Decimal(importe),
        fx_rate=Decimal("1"), importe_eur=Decimal(importe),
        gastos_eur=Decimal("0"), tasas_externas_eur=Decimal("0"),
        retencion_eur=Decimal("0"), retencion_pais=None,
        external_id=ext_id, broker_id=broker_id, notas=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# A3 — dos operaciones legítimas con el mismo external_id sobreviven
# ═══════════════════════════════════════════════════════════════════════════

def test_a3_dos_compras_identicas_mismo_dia_se_insertan_ambas(db: Session, cartera):
    """Dos compras limit idénticas el mismo día (mismo sintético IBKR):
    pre-fix la segunda se contaba como duplicado y desaparecía."""
    from app.services.transacciones import reconciliar_extracto
    b = _broker(db, cartera)
    cands = [_cand(b.id), _cand(b.id)]   # external_id idéntico
    r = reconciliar_extracto(db, cartera.id, "ibkr", cands)
    assert r.insertadas == 2, f"ambas compras son legítimas: {r}"
    assert r.deduplicadas == 0


def test_a3_reimportar_el_mismo_extracto_deduplica_las_dos(db: Session, cartera):
    """El sufijo es determinista: reimportar el mismo extracto no duplica."""
    from app.services.transacciones import reconciliar_extracto
    b = _broker(db, cartera)
    reconciliar_extracto(db, cartera.id, "ibkr", [_cand(b.id), _cand(b.id)])
    r2 = reconciliar_extracto(db, cartera.id, "ibkr", [_cand(b.id), _cand(b.id)])
    assert r2.insertadas == 0
    assert r2.deduplicadas == 2


def test_a3_extracto_parcial_deduplica_la_primera(db: Session, cartera):
    """Un extracto posterior que solo trae UNA de las dos operaciones casa
    con la primera (id sin sufijo)."""
    from app.services.transacciones import reconciliar_extracto
    b = _broker(db, cartera)
    reconciliar_extracto(db, cartera.id, "ibkr", [_cand(b.id), _cand(b.id)])
    r = reconciliar_extracto(db, cartera.id, "ibkr", [_cand(b.id)])
    assert r.insertadas == 0 and r.deduplicadas == 1


# ═══════════════════════════════════════════════════════════════════════════
# A4 — dividendos múltiples del mismo día se ACUMULAN
# ═══════════════════════════════════════════════════════════════════════════

def test_a4_dos_dividendos_mismo_dia_suman():
    from app.adapters.cuadrate import _consolidar_dividendos
    divs = [
        {"isin": "US0378331005", "fecha": "07/03/2025", "tipo": "DIV",
         "importe_eur": Decimal("70.11"), "nombre": "Apple", "pais": "US"},
        {"isin": "US0378331005", "fecha": "07/03/2025", "tipo": "DIV",
         "importe_eur": Decimal("12.50"), "nombre": "Apple", "pais": "US"},
        {"isin": "US0378331005", "fecha": "07/03/2025", "tipo": "RET",
         "importe_eur": Decimal("10.52"), "nombre": "Apple", "pais": "US"},
        {"isin": "US0378331005", "fecha": "07/03/2025", "tipo": "RET",
         "importe_eur": Decimal("1.88"), "nombre": "Apple", "pais": "US"},
    ]
    out = _consolidar_dividendos(divs, broker_id="b1", prefix="ib", notas="x")
    assert len(out) == 1
    c = out[0]
    assert c.importe_eur == Decimal("82.61"), "el primer dividendo se perdía"
    assert c.retencion_eur == Decimal("12.40")


def test_a4_reversal_negativo_netea():
    """F4 de Cuádrate emite reversals como DIV negativos: deben netear."""
    from app.adapters.cuadrate import _consolidar_dividendos
    divs = [
        {"isin": "FR0000052292", "fecha": "10/04/2025", "tipo": "DIV",
         "importe_eur": Decimal("100"), "nombre": "Hermes", "pais": "FR"},
        {"isin": "FR0000052292", "fecha": "10/04/2025", "tipo": "DIV",
         "importe_eur": Decimal("-100"), "nombre": "Hermes", "pais": "FR"},
        {"isin": "FR0000052292", "fecha": "10/04/2025", "tipo": "DIV",
         "importe_eur": Decimal("98"), "nombre": "Hermes", "pais": "FR"},
    ]
    out = _consolidar_dividendos(divs, broker_id="b1", prefix="dg", notas="x")
    assert len(out) == 1
    assert out[0].importe_eur == Decimal("98")


# ═══════════════════════════════════════════════════════════════════════════
# A11 — depósitos IBKR en divisa ≠ EUR
# ═══════════════════════════════════════════════════════════════════════════

def _csv_ibkr_dw(tmp_path, cur, amount):
    p = tmp_path / "ibkr.csv"
    p.write_text(
        "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount\n"
        f"Deposits & Withdrawals,Data,{cur},2025-03-10,Electronic Fund Transfer,{amount}\n",
        encoding="utf-8",
    )
    return str(p)


def test_a11_deposito_usd_se_convierte(tmp_path, monkeypatch):
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g
    from app.services.aportaciones import parse_ibkr_aportaciones
    monkeypatch.setattr(g, "_ibkr_eur_per_unit", lambda f, c: Decimal("0.90"))
    out = parse_ibkr_aportaciones(_csv_ibkr_dw(tmp_path, "USD", "10000"), "b1")
    assert len(out) == 1
    assert out[0].importe_eur == Decimal("9000.00"), \
        "10.000 USD se registraban como 10.000 EUR"


def test_a11_deposito_usd_sin_fx_no_registra_cifra_erronea(tmp_path, monkeypatch):
    from app.adapters.cuadrate import _ensure_cuadrate_importable
    _ensure_cuadrate_importable()
    import generar_irpf as g
    from app.services.aportaciones import parse_ibkr_aportaciones
    monkeypatch.setattr(g, "_ibkr_eur_per_unit", lambda f, c: None)
    out = parse_ibkr_aportaciones(_csv_ibkr_dw(tmp_path, "USD", "10000"), "b1")
    assert out == []


def test_a11_deposito_eur_intacto(tmp_path):
    from app.services.aportaciones import parse_ibkr_aportaciones
    out = parse_ibkr_aportaciones(_csv_ibkr_dw(tmp_path, "EUR", "5000"), "b1")
    assert len(out) == 1 and out[0].importe_eur == Decimal("5000")


# ═══════════════════════════════════════════════════════════════════════════
# J5 — un statement parcial no machaca el realized del año completo
# ═══════════════════════════════════════════════════════════════════════════

def _res_cand(broker_id, realized, inicio, fin):
    from app.adapters.cuadrate import ResultadoCandidata
    return ResultadoCandidata(
        categoria="FOREX", ejercicio=fin.year, clave="USD",
        realized_eur=Decimal(str(realized)), unrealized_eur=Decimal("0"),
        periodo_inicio=inicio, periodo_fin=fin,
        external_id=f"ibkr-fx-{fin.year}-USD", broker_id=broker_id,
    )


def test_j5_statement_parcial_no_sobrescribe_el_anual(db: Session, cartera):
    from app.services.resultados import upsert_resultados_ibkr
    b = _broker(db, cartera)
    anual = _res_cand(b.id, "-500", date(2025, 1, 1), date(2025, 12, 31))
    upsert_resultados_ibkr(db, cartera.id, [anual])

    parcial = _res_cand(b.id, "-120", date(2025, 1, 1), date(2025, 6, 30))
    r = upsert_resultados_ibkr(db, cartera.id, [parcial])
    assert r.ignoradas == 1 and r.actualizadas == 0

    fila = db.query(models.ResultadoIbkr).one()
    assert fila.realized_eur == Decimal("-500"), \
        "el semestre machacaba el realized del año completo"


def test_j5_statement_mas_amplio_si_actualiza(db: Session, cartera):
    from app.services.resultados import upsert_resultados_ibkr
    b = _broker(db, cartera)
    parcial = _res_cand(b.id, "-120", date(2025, 1, 1), date(2025, 6, 30))
    upsert_resultados_ibkr(db, cartera.id, [parcial])

    anual = _res_cand(b.id, "-500", date(2025, 1, 1), date(2025, 12, 31))
    r = upsert_resultados_ibkr(db, cartera.id, [anual])
    assert r.actualizadas == 1 and r.ignoradas == 0
    fila = db.query(models.ResultadoIbkr).one()
    assert fila.realized_eur == Decimal("-500")


# ═══════════════════════════════════════════════════════════════════════════
# Tanda 5 (backend de soporte al frontend): A9 serie mensual + F6 isin en API
# ═══════════════════════════════════════════════════════════════════════════

def test_a9_serie_mensual_lleva_neto(db: Session, cartera):
    """La vista mensual del chart mostraba neto=bruto porque la serie
    mensual de la API no exponía el neto (auditoría Cima 2026-06-11, A9)."""
    from app.services.fiscal_dividendos import serie_dividendos
    pos = models.Posicion(cartera_id=cartera.id, isin="US0000000009",
                          nombre="Pagadora", divisa_local="EUR")
    db.add(pos); db.flush()
    db.add(models.Transaccion(
        cartera_id=cartera.id, broker_id=None, posicion_id=pos.id,
        fecha=date(2025, 3, 7), tipo="DIVIDEND",
        cantidad=Decimal("0"), precio_local=Decimal("0"), divisa_local="EUR",
        importe_local=Decimal("100"), fx_rate=Decimal("1"),
        importe_eur=Decimal("100"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("15"),
        retencion_pais="US", estado="confirmada", origen="extracto",
    ))
    db.commit()
    serie = serie_dividendos(db, cartera.id)
    m = serie.mensual[0]
    assert m.bruto == Decimal("100.00")
    assert m.neto == Decimal("85.00"), "el neto mensual debe descontar la retención"


def test_f6_transaccion_expone_isin(db: Session, cartera):
    """La columna 'ISIN' del frontend mostraba un prefijo del UUID interno
    porque la API no exponía el ISIN de la posición."""
    pos = models.Posicion(cartera_id=cartera.id, isin="US0378331005",
                          nombre="Apple", divisa_local="EUR")
    db.add(pos); db.flush()
    tx = models.Transaccion(
        cartera_id=cartera.id, broker_id=None, posicion_id=pos.id,
        fecha=date(2025, 1, 10), tipo="BUY",
        cantidad=Decimal("10"), precio_local=Decimal("10"), divisa_local="EUR",
        importe_local=Decimal("100"), fx_rate=Decimal("1"),
        importe_eur=Decimal("100"), gastos_eur=Decimal("0"),
        tasas_externas_eur=Decimal("0"), retencion_eur=Decimal("0"),
        estado="confirmada", origen="manual",
    )
    db.add(tx); db.commit()
    from app.schemas.transaccion import TransaccionOut
    out = TransaccionOut.model_validate(tx)
    assert out.isin == "US0378331005"
    assert out.posicion_nombre == "Apple"
