"""Tests de bloques de estrategia: distribución, asignación y CRUD."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import models
from app.services import bloques as svc


def _pos_con_lote(db: Session, cartera, isin: str, nombre: str, coste: Decimal) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2025, 1, 1),
        cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
        coste_unit_eur=coste / Decimal("10"),
        coste_total_eur=coste, gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def _bloque(db: Session, cartera, nombre: str, categoria: str, es_base=True) -> models.Bloque:
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=categoria,
                      orden=1, es_base=es_base)
    db.add(b); db.flush()
    return b


def test_distribucion_sin_asignar_todo_sin_clasificar(db, cartera) -> None:
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    _pos_con_lote(db, cartera, "US2", "Beta", Decimal("4000"))
    db.commit()
    r = svc.calcular_distribucion(db, cartera.id)
    assert r.total_eur == Decimal("10000")
    sin = [b for b in r.bloques if b.id == svc.SIN_CLASIFICAR_ID][0]
    assert sin.n_posiciones == 2
    assert sin.peso_actual == Decimal("1")


def test_asignar_mueve_a_bloque_y_actualiza_peso(db, cartera) -> None:
    growth = _bloque(db, cartera, "Growth", "growth")
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    _pos_con_lote(db, cartera, "US2", "Beta", Decimal("4000"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, p.isin, growth.id)
    r = svc.calcular_distribucion(db, cartera.id)
    g = [b for b in r.bloques if b.id == growth.id][0]
    assert g.valor_eur == Decimal("6000")
    assert g.peso_actual == Decimal("0.6")
    assert g.n_posiciones == 1


def test_asignar_sin_clasificar_desasigna(db, cartera) -> None:
    growth = _bloque(db, cartera, "Growth", "growth")
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, p.isin, growth.id)
    svc.asignar_bloque(db, cartera.id, p.isin, "sin_clasificar")
    db.refresh(p)
    assert p.bloque_id is None


def test_crear_bloque_respeta_tope(db, cartera) -> None:
    for i in range(svc.TOPE_BLOQUES):
        _bloque(db, cartera, f"B{i}", "growth", es_base=False)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        svc.crear_bloque(db, cartera.id, "Excedente", "income")
    assert exc.value.status_code == 400


def test_crear_bloque_nombre_duplicado(db, cartera) -> None:
    svc.crear_bloque(db, cartera.id, "Compounders", "growth")
    with pytest.raises(HTTPException) as exc:
        svc.crear_bloque(db, cartera.id, "compounders", "income")  # case-insensitive
    assert exc.value.status_code == 409


def test_colchon_efectivo_entra_en_valor_y_total(db, cartera) -> None:
    colchon = _bloque(db, cartera, "Colchón", "colchon")
    _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("10000"))
    db.commit()
    # 18.000 € de efectivo al 3,25%
    svc.editar_bloque(db, cartera.id, colchon.id, set_liquidez=True,
                      liquidez_asignada_eur=Decimal("18000"),
                      set_rendimiento=True, rendimiento_pct=Decimal("0.0325"))
    r = svc.calcular_distribucion(db, cartera.id)
    assert r.total_eur == Decimal("28000")          # 10.000 posiciones + 18.000 efectivo
    cc = [b for b in r.bloques if b.id == colchon.id][0]
    assert cc.valor_eur == Decimal("18000")
    assert cc.liquidez_asignada_eur == Decimal("18000")
    assert cc.rendimiento_pct == Decimal("0.0325")
    # peso del colchón = 18000/28000
    assert cc.peso_actual == (Decimal("18000") / Decimal("28000"))


def test_colchon_admite_posiciones_ademas_de_efectivo(db, cartera) -> None:
    colchon = _bloque(db, cartera, "Colchón", "colchon")
    etf = _pos_con_lote(db, cartera, "IE_ETF", "ETF conservador", Decimal("5000"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, etf.isin, colchon.id)
    svc.editar_bloque(db, cartera.id, colchon.id, set_liquidez=True,
                      liquidez_asignada_eur=Decimal("3000"))
    r = svc.calcular_distribucion(db, cartera.id)
    cc = [b for b in r.bloques if b.id == colchon.id][0]
    assert cc.valor_eur == Decimal("8000")          # 5.000 ETF + 3.000 efectivo
    assert cc.n_posiciones == 1


def test_objetivo_peso_calcula_desviacion_y_alerta(db, cartera) -> None:
    growth = _bloque(db, cartera, "Growth", "growth")
    _bloque(db, cartera, "Otro", "income")
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    _pos_con_lote(db, cartera, "US2", "Beta", Decimal("4000"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, p.isin, growth.id)   # Growth = 60%
    # objetivo 40%, tolerancia 5% → desviación +20% → fuera de tolerancia
    svc.editar_bloque(db, cartera.id, growth.id, set_peso=True,
                      peso_objetivo=Decimal("0.40"), tolerancia=Decimal("0.05"))
    r = svc.calcular_distribucion(db, cartera.id)
    g = [b for b in r.bloques if b.id == growth.id][0]
    assert g.peso_objetivo == Decimal("0.40")
    assert g.peso_actual == Decimal("0.6")
    assert g.desviacion == Decimal("0.2")        # 0.6 − 0.4
    assert g.fuera_tolerancia is True
    # dentro de tolerancia si objetivo 58%
    svc.editar_bloque(db, cartera.id, growth.id, set_peso=True,
                      peso_objetivo=Decimal("0.58"))
    g2 = [b for b in svc.calcular_distribucion(db, cartera.id).bloques if b.id == growth.id][0]
    assert g2.fuera_tolerancia is False          # |0.6−0.58|=0.02 < 0.05


def test_eliminar_bloque_devuelve_posiciones_a_sin_clasificar(db, cartera) -> None:
    growth = _bloque(db, cartera, "Growth", "growth", es_base=False)
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("6000"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, p.isin, growth.id)
    svc.eliminar_bloque(db, cartera.id, growth.id)
    db.refresh(p)
    assert p.bloque_id is None
    assert db.get(models.Bloque, growth.id) is None


# ── captura de overrides (semilla del few-shot) ─────────────────────────────

def test_asignar_registra_override_cuando_difiere(db, cartera) -> None:
    defensivo = _bloque(db, cartera, "Estable", "defensivo")
    p = _pos_con_lote(db, cartera, "DK_NOVO", "Novo Nordisk", Decimal("3000"))
    db.commit()
    # La IA sugirió growth; el usuario lo pone en defensivo (su sesgo).
    svc.asignar_bloque(db, cartera.id, p.isin, defensivo.id,
                       categoria_sugerida="growth", confianza_ia=0.8,
                       razon="lo siento defensivo")
    ej = svc.overrides_recientes(db, cartera.id)
    assert len(ej) == 1
    assert ej[0]["categoria_sugerida"] == "growth"
    assert ej[0]["categoria_elegida"] == "defensivo"
    assert ej[0]["razon"] == "lo siento defensivo"
    assert ej[0]["nombre"] == "Novo Nordisk"


def test_asignar_no_registra_override_si_coincide(db, cartera) -> None:
    growth = _bloque(db, cartera, "Compounders", "growth")
    p = _pos_con_lote(db, cartera, "US_MSFT", "Microsoft", Decimal("9000"))
    db.commit()
    # La IA sugirió growth y el usuario acepta growth → no es override.
    svc.asignar_bloque(db, cartera.id, p.isin, growth.id, categoria_sugerida="growth")
    assert svc.overrides_recientes(db, cartera.id) == []


def test_overrides_recientes_solo_discrepancias_y_limite(db, cartera) -> None:
    defensivo = _bloque(db, cartera, "Estable", "defensivo")
    p = _pos_con_lote(db, cartera, "US1", "Alpha", Decimal("1000"))
    db.commit()
    # Coincidente (defensivo→defensivo): no debe aparecer.
    svc.asignar_bloque(db, cartera.id, p.isin, defensivo.id, categoria_sugerida="defensivo")
    # Discrepante: sí aparece.
    svc.asignar_bloque(db, cartera.id, p.isin, defensivo.id, categoria_sugerida="aggressive")
    ej = svc.overrides_recientes(db, cartera.id, n=8)
    assert len(ej) == 1
    assert ej[0]["categoria_sugerida"] == "aggressive"


def test_asignar_bloque_a_seguimiento_y_override(db, cartera) -> None:
    """Un candidato del watchlist (Seguimiento) también se asigna a un bloque, y
    si la categoría difiere de la sugerida por la IA, se captura el override."""
    defensivo = _bloque(db, cartera, "Estable", "defensivo")
    db.add(models.Seguimiento(cartera_id=cartera.id, isin="US_NVDA", ticker="NVDA",
                              nombre="Nvidia"))
    db.commit()
    svc.asignar_bloque(db, cartera.id, "US_NVDA", defensivo.id,
                       categoria_sugerida="growth", razon="lo veo seguro")
    s = db.query(models.Seguimiento).filter_by(isin="US_NVDA").first()
    assert s.bloque_id == defensivo.id
    ej = svc.overrides_recientes(db, cartera.id)
    assert len(ej) == 1
    assert ej[0]["categoria_elegida"] == "defensivo"
    assert ej[0]["nombre"] == "Nvidia"


def test_asignar_no_lo_tapa_una_posicion_cerrada(db, cartera) -> None:
    """Si un ISIN es a la vez posición CERRADA (vendida) y seguimiento, asignar
    bloque va al SEGUIMIENTO, no a la posición fantasma. (Bug NVDA 2026-05-25.)"""
    growth = _bloque(db, cartera, "Compounders", "growth")
    # Posición cerrada: lote con cantidad_restante = 0.
    p = models.Posicion(cartera_id=cartera.id, isin="US_NVDA", nombre="NVIDIA", divisa_local="USD")
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("0"),
        coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("0"), gastos_eur=Decimal("0"),
    ))
    db.add(models.Seguimiento(cartera_id=cartera.id, isin="US_NVDA", ticker="NVDA",
                              nombre="Nvidia"))
    db.commit()

    svc.asignar_bloque(db, cartera.id, "US_NVDA", growth.id)

    seg = db.query(models.Seguimiento).filter_by(isin="US_NVDA").first()
    pos = db.query(models.Posicion).filter_by(isin="US_NVDA").first()
    assert seg.bloque_id == growth.id        # el seguimiento recibe el bloque
    assert pos.bloque_id is None             # la posición cerrada NO


def test_catalogo_base_son_seis_y_opcionales_no() -> None:
    """El catálogo del que siembra bootstrap: 6 base + 2 opcionales que NO se
    siembran. (Si esto cambia, ajustar el seed de bootstrap.)"""
    from app.adapters.ia.prompt import FICHAS

    base = [cod for cod, f in FICHAS.items() if f.es_base]
    assert set(base) == {"growth", "income", "defensivo", "aggressive",
                         "satelite", "colchon"}
    assert not FICHAS["indice"].es_base
    assert not FICHAS["renta_fija"].es_base
