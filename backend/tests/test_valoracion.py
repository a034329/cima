"""Tests de la valoración asistida (parse+cálculo, proponer PER+persistencia, no-PER)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import valoracion as svc

_JSON = ('```json\n{"escenarios": ['
         '{"nombre": "conservador", "multiplo": 15, "eps_4y": 8, "razon": "PER bajo del rango histórico"},'
         '{"nombre": "base", "multiplo": 20, "eps_4y": 10, "razon": "En línea con el consenso"},'
         '{"nombre": "optimista", "multiplo": 25, "eps_4y": 12, "razon": "Expansión de múltiplo"}'
         ']}\n```')


class _FakeIA:
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return _JSON


def test_parse_calcula_precio_objetivo_y_cagr() -> None:
    esc = svc.parse(_JSON, precio_actual=100.0)
    assert len(esc) == 3
    base = [e for e in esc if e.nombre == "base"][0]
    assert base.precio_objetivo == 200.0                     # 20 × 10 (el sistema lo calcula)
    assert base.cagr4_pct is not None and abs(base.cagr4_pct - ((2 ** 0.25) - 1)) < 1e-9


def _pos_per(db, cartera, isin, tipo_val="PER") -> None:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre="Acme", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Estimacion(cartera_id=cartera.id, isin=isin, tipo_val=tipo_val,
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
                            metrica_base_4y=Decimal("10")))
    db.commit()


def _mock_precios(monkeypatch, isin) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin", lambda db, cid: {isin: {"sector": "Tech"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {isin: (Decimal("100"), "EUR")})
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA())


def test_proponer_per_persiste_y_relee(db: Session, cartera, monkeypatch) -> None:
    _pos_per(db, cartera, "US_X")
    _mock_precios(monkeypatch, "US_X")

    assert svc.guardado(db, cartera.id, "US_X") is None
    v = svc.proponer(db, cartera.id, "US_X")
    assert v.tipo_val == "PER" and len(v.escenarios) == 3
    assert v.anclas["eps_actual"] == 5.0
    assert v.fecha == date.today().isoformat()

    re = svc.guardado(db, cartera.id, "US_X")
    assert re is not None and len(re.escenarios) == 3
    assert re.escenarios[1].precio_objetivo == 200.0          # base reconstruido del JSON guardado


_JSON_FRE = ('{"escenarios": ['
             '{"nombre": "conservador", "multiplo": 18, "metrica_4y": 4, "razon": "P/FRE bajo del sector"},'
             '{"nombre": "base", "multiplo": 22, "metrica_4y": 5, "razon": "En línea con comparables"},'
             '{"nombre": "optimista", "multiplo": 26, "metrica_4y": 6, "razon": "Expansión de FRE"}'
             ']}')


class _FakeIAFre:
    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        return _JSON_FRE


def test_proponer_no_per_usa_metrica_4y(db: Session, cartera, monkeypatch) -> None:
    """Para un valor no-PER (P_FRE, p.ej. una gestora tipo BAM) la valoración ya NO
    se rechaza: propone escenarios usando el campo genérico metrica_4y."""
    _pos_per(db, cartera, "US_Y", tipo_val="P_FRE")
    _mock_precios(monkeypatch, "US_Y")
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIAFre())

    v = svc.proponer(db, cartera.id, "US_Y")
    assert v.tipo_val == "P_FRE" and len(v.escenarios) == 3
    base = [e for e in v.escenarios if e.nombre == "base"][0]
    assert base.metrica_base_4y == 5.0 and base.precio_objetivo == 110.0   # 22 × 5


def test_build_prompt_no_per_habla_de_su_multiplo() -> None:
    system, user = svc.build_prompt("BAM", {"multiplo_actual": 22, "metrica_actual": 5}, "P_FRE")
    assert "P/FRE" in system and "metrica_4y" in system
    assert "PER" not in user            # no habla de PER para un valor que no se valora por beneficios


# ── Guardias (bug BAM 5-jun-2026: IA mezcló FRE total $5.63B con FRE/acc) ──


def test_guardia_cagr_bloquea_escenario_irreal() -> None:
    """Caso BAM real: multiplo 27, métrica reportada 5.63 (era agregado),
    precio 38.92 → precio_obj 152, CAGR 40.6% > 35% → bloqueado."""
    j = ('{"escenarios":[{"nombre":"base","multiplo":27,"metrica_4y":5.63,'
         '"razon":"FRE crece 17%"}]}')
    esc = svc.parse(j, precio_actual=38.92, metrica_actual=1.92,
                    tipo_val="P_FRE", categoria_bloque="income")
    assert len(esc) == 1
    e = esc[0]
    assert e.bloqueado is True
    assert any("CAGR" in a for a in e.alertas)


def test_guardia_dimensional_bloquea_cuando_metrica_supera_mitad_precio() -> None:
    """Si la IA devuelve la métrica en millones (no millardos), supera con
    creces el 50% del precio. Caso defensivo: muy útil cuando el múltiplo
    es pequeño y el CAGR solo no lo coge."""
    # Si BAM reportara metrica = 5630 (millones de FRE total) con precio 38.92,
    # el ratio sería 144 >> 0.5 → guardia dimensional dispara.
    j = ('{"escenarios":[{"nombre":"base","multiplo":1,"metrica_4y":50,'
         '"razon":"test"}]}')
    # precio_actual 80 → metrica 50 > 80*0.5=40 → dispara dimensional
    esc = svc.parse(j, precio_actual=80.0, metrica_actual=2.0,
                    tipo_val="P_FRE", categoria_bloque="income")
    e = esc[0]
    assert e.bloqueado is True
    assert any("VALOR TOTAL" in a or "agregado" in a.lower() or "POR ACCIÓN" in a
               for a in e.alertas)


def test_guardia_cagr_alerta_pero_no_bloquea_por_bloque() -> None:
    """Crecimiento elevado para 'income' (>20% pero <35%) → alerta amarilla,
    no bloqueo."""
    # mult 10, met 5, precio 30 → precio_obj 50, CAGR = (50/30)^(1/4)-1 = 13.6%
    # Necesito un caso con CAGR entre 20% y 35%.
    # mult 10, met 5, precio 22 → 50/22 = 2.27, ^.25 = 1.227 → CAGR 22.7%
    j = ('{"escenarios":[{"nombre":"base","multiplo":10,"metrica_4y":5,'
         '"razon":"test"}]}')
    esc = svc.parse(j, precio_actual=22.0, metrica_actual=4.5,
                    tipo_val="P_FRE", categoria_bloque="income")
    e = esc[0]
    assert e.bloqueado is False                      # CAGR 22% < 35% bloqueo absoluto
    assert any("supera el umbral del bloque" in a for a in e.alertas)


def test_escenario_sano_sin_alertas_ni_bloqueo() -> None:
    """Caso normal PER: mult 20, EPS 10, precio 150 → CAGR 7.5%, sin guardias."""
    j = ('{"escenarios":[{"nombre":"base","multiplo":20,"eps_4y":10,'
         '"razon":"En línea con consenso"}]}')
    esc = svc.parse(j, precio_actual=150.0, metrica_actual=8.0,
                    tipo_val="PER", categoria_bloque="growth")
    e = esc[0]
    assert e.bloqueado is False
    assert e.alertas == []


def test_desglose_incluye_pasos_basicos_del_calculo() -> None:
    """El desglose paso-a-paso debe contener Método, Precio objetivo y CAGR."""
    j = ('{"escenarios":[{"nombre":"base","multiplo":20,"eps_4y":10,'
         '"razon":"x"}]}')
    esc = svc.parse(j, precio_actual=150.0, metrica_actual=8.0,
                    tipo_val="PER", categoria_bloque="growth")
    e = esc[0]
    etiquetas = [p["etiqueta"] for p in e.desglose]
    assert any("Método" in et for et in etiquetas)
    assert any("Precio objetivo" in et for et in etiquetas)
    assert any("CAGR implícito" in et for et in etiquetas)
    # El paso "Precio objetivo" debe mostrar el cálculo Múltiplo × Métrica
    pobj = next(p for p in e.desglose if "Precio objetivo" in p["etiqueta"])
    assert "20" in pobj["calc"] and "10" in pobj["calc"]


def test_prompt_refuerza_per_share() -> None:
    """El prompt debe explicitar que la métrica es POR ACCIÓN para evitar
    el bug BAM en origen."""
    system, _ = svc.build_prompt("X", {}, "P_FRE")
    assert "POR ACCIÓN" in system
    assert "50%" in system               # menciona el límite de cordura


def test_prompt_incluye_reglas_integridad_analitica() -> None:
    """Anti-fallos LVMH→Hermès (2026-06-09): el prompt debe instruir sobre
    serie del múltiplo (TTM vs Forward), divisa, contraste de fuentes y
    auto-check de coherencia. Mismo conjunto de guardrails que el asesor."""
    system, _ = svc.build_prompt("X", {}, "PER")
    # 1. Serie del múltiplo declarada (TTM vs Forward)
    assert "TTM" in system and "Forward" in system or "FORWARD" in system
    assert "SERIE" in system               # debe insistir en series compatibles
    # 2. Divisa explícita
    assert "DIVISA" in system or "divisa" in system
    assert "USD" in system or "EUR" in system        # ejemplos de divisas
    # 3. Contraste de fuentes con discrepancia >10%
    assert "10%" in system
    assert "discrepancia" in system.lower() or "DIFIERE" in system
    # 4. Auto-check de coherencia (CAGR razonable)
    assert "CAGR" in system
    assert "30%" in system               # umbral de sospecha
    # Y el JSON de salida pide divisa y discrepancia en la razón
    assert "misma divisa que el precio" in system
    assert "discrepancia" in system.lower() or "discrepancias" in system.lower()


def test_umbral_cagr_aggressive_es_mas_alto_que_income() -> None:
    """Un BDC/REIT (aggressive) admite CAGR más alto antes de alertar
    (24% no debería disparar; en income sí dispararía)."""
    # mult 8, met 5, precio 20 → 40/20 = 2.0, ^.25 - 1 = 18.9%
    # No me sirve. Necesito 24%.
    # 8 × 5 = 40, precio 17 → 40/17 = 2.35, ^.25 = 1.238 → CAGR 23.8%
    j = ('{"escenarios":[{"nombre":"base","multiplo":8,"metrica_4y":5,'
         '"razon":"x"}]}')
    e_aggr = svc.parse(j, precio_actual=17.0, metrica_actual=4.0,
                       tipo_val="P_BV", categoria_bloque="aggressive")[0]
    e_inc = svc.parse(j, precio_actual=17.0, metrica_actual=4.0,
                      tipo_val="P_BV", categoria_bloque="income")[0]
    # aggressive umbral 25% → 23.8% NO alerta; income umbral 20% → SÍ alerta.
    assert not any("supera el umbral" in a for a in e_aggr.alertas)
    assert any("supera el umbral" in a for a in e_inc.alertas)
