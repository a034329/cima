"""Test del dashboard agregado (offline: precio_manual_eur evita la red)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services.dashboard import calcular_dashboard


def _pos(db, cartera, isin, nombre, qty, coste, precio_actual) -> models.Posicion:
    p = models.Posicion(
        cartera_id=cartera.id, isin=isin, nombre=nombre, divisa_local="EUR",
        precio_manual_eur=Decimal(str(precio_actual)),
    )
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def test_dashboard_gp_no_realizada_y_composicion(db: Session, cartera) -> None:
    # A: coste 1000, ahora 1200 (+200). B: coste 1000, ahora 700 (−300).
    _pos(db, cartera, "US_A", "Alpha", 10, 1000, 120)   # 10 × 120 = 1200
    _pos(db, cartera, "US_B", "Beta", 10, 1000, 70)     # 10 × 70 = 700
    db.commit()

    r = calcular_dashboard(db, cartera.id)
    assert r.capital_mercado_eur == Decimal("1900")          # 1200 + 700
    assert r.gp_no_realizada_eur == Decimal("-100")          # 1900 − 2000
    # pct = -100 / 2000
    assert r.gp_no_realizada_pct == Decimal("-100") / Decimal("2000")
    # sin bloques asignados → todo en 'Sin clasificar'. La composición es por
    # COSTE (consistente con la pestaña Bloques), no por valor de mercado.
    comp = [c for c in r.composicion if c.valor_eur > 0]
    assert len(comp) == 1
    assert comp[0].categoria_base == "sin_clasificar"
    assert comp[0].valor_eur == Decimal("2000.00")


def test_dashboard_progreso_if(db: Session, cartera) -> None:
    _pos(db, cartera, "US_A", "Alpha", 10, 100000, 15000)   # 150.000 € mercado
    db.commit()
    r = calcular_dashboard(db, cartera.id)
    # capital invertido 150.000 / 300.000 = 0.5 (sin colchón ni liquidez)
    assert r.progreso_if_pct == Decimal("0.5")
    assert r.anios_if is not None      # alcanzable con supuesto 7%


def test_progreso_if_excluye_colchon(db: Session, cartera) -> None:
    """El colchón (Bloque F) cuenta en 'Invertido' pero NO en el progreso IF."""
    estrategia = _pos(db, cartera, "US_A", "Alpha", 10, 100000, 15000)  # 150.000
    colchon_pos = _pos(db, cartera, "US_F", "MinVol ETF", 10, 50000, 6000)  # 60.000
    bloque_f = models.Bloque(
        cartera_id=cartera.id, nombre="Colchón", categoria_base="colchon", orden=9,
        en_estrategia=False,   # el colchón nace fuera de la estrategia IF
    )
    db.add(bloque_f); db.flush()
    colchon_pos.bloque_id = bloque_f.id
    db.commit()

    r = calcular_dashboard(db, cartera.id)
    # "Invertido" incluye TODO: 150.000 + 60.000 = 210.000
    assert r.capital_mercado_eur == Decimal("210000")
    # Progreso IF excluye el colchón: 150.000 / 300.000 = 0.5
    assert r.progreso_if_pct == Decimal("0.5")


def test_progreso_if_excluye_cualquier_bloque_fuera_de_estrategia(db: Session, cartera) -> None:
    """Generalización del colchón: cualquier bloque con en_estrategia=False queda
    fuera del progreso IF (p.ej. cripto a largo)."""
    _pos(db, cartera, "US_A", "Alpha", 10, 100000, 15000)   # 150.000 en estrategia
    cripto_pos = _pos(db, cartera, "XF_BTC", "Bitcoin", 10, 50000, 6000)  # 60.000
    b_cripto = models.Bloque(
        cartera_id=cartera.id, nombre="Cripto", categoria_base="cripto", orden=9,
        en_estrategia=False,
    )
    db.add(b_cripto); db.flush()
    cripto_pos.bloque_id = b_cripto.id
    db.commit()

    r = calcular_dashboard(db, cartera.id)
    assert r.capital_mercado_eur == Decimal("210000")   # "Invertido" incluye la cripto
    assert r.progreso_if_pct == Decimal("0.5")          # IF la excluye: 150k/300k


def test_progreso_if_usa_objetivo_configurable(db: Session, cartera) -> None:
    """El progreso IF usa el objetivo de la cartera, no un 300k fijo."""
    from decimal import Decimal as D
    _pos(db, cartera, "US_A", "Alpha", 10, 100000, 15000)   # 150.000 € invertido
    cartera.objetivo_if_eur = D("150000")                   # objetivo = 150k
    db.commit()
    r = calcular_dashboard(db, cartera.id)
    assert r.progreso_if_pct == D("1")                      # 150.000 / 150.000 = 100%


def test_anios_if_usa_aportacion_prevista(db: Session, cartera) -> None:
    """La aportación mensual prevista (×12) alimenta la proyección de años a IF."""
    from decimal import Decimal as D
    _pos(db, cartera, "US_A", "Alpha", 10, 100000, 10000)   # 100.000 € invertido
    cartera.objetivo_if_eur = D("300000")
    cartera.aportacion_mensual_eur = D("2000")              # 24.000 €/año
    db.commit()
    r = calcular_dashboard(db, cartera.id)
    assert r.anios_if is not None
    # Sin aportación tardaría más; con 24k/año debe ser alcanzable y menor.
    cartera.aportacion_mensual_eur = D("0")
    db.commit()
    r0 = calcular_dashboard(db, cartera.id)
    assert r0.anios_if is None or r0.anios_if >= r.anios_if


def test_anios_a_if_fraccionado_y_sensible_al_retorno() -> None:
    """Años fraccionados y dependientes del retorno: más rentabilidad → antes."""
    from decimal import Decimal as D
    from app.services.dashboard import _anios_a_if
    a7 = _anios_a_if(D("200000"), D("0"), D("300000"), D("0.07"))
    a15 = _anios_a_if(D("200000"), D("0"), D("300000"), D("0.15"))
    assert a7 is not None and a15 is not None
    assert a15 < a7                      # mayor retorno → menos años
    assert D("5") < a7 < D("7")          # 200k al 7% → ~6 años
    assert a7 % 1 != 0 or a15 % 1 != 0   # al menos uno fraccionado (no entero)
