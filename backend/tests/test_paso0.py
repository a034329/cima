"""Tests de PASO 0 (parse tolerante + analizar_contexto con IA mock, sin red)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import paso0 as svc


def test_parse_extrae_de_json_con_prosa() -> None:
    texto = ('Aquí tienes el análisis:\n```json\n'
             '{"resumen": "Ruido de corto plazo.", "clasificacion": "coyuntural", '
             '"preguntas": [{"pregunta": "¿Genera caja?", "respuesta": "Sí", "senal": "coyuntural"}], '
             '"riesgo_principal": "Márgenes.", "fuentes": ["https://x.com/a", ""]}\n```\nEspero que ayude.')
    d = svc.parse(texto)
    assert d["clasificacion"] == "COYUNTURAL"          # normalizado a mayúsculas
    assert len(d["preguntas"]) == 1
    assert d["fuentes"] == ["https://x.com/a"]         # vacío filtrado
    assert d["resumen"].startswith("Ruido")


def test_parse_fallback_si_no_hay_json() -> None:
    d = svc.parse("no pude buscar nada útil")
    assert d["clasificacion"] == "SIN_DATOS"
    assert "no pude" in d["resumen"]


def test_parse_clasificacion_invalida_a_sin_datos() -> None:
    d = svc.parse('{"clasificacion": "INVENTADA", "resumen": "x"}')
    assert d["clasificacion"] == "SIN_DATOS"


def test_analizar_contexto_mock(db: Session, cartera, monkeypatch) -> None:
    # posición mínima para que construir_contexto resuelva nombre/sector
    p = models.Posicion(cartera_id=cartera.id, isin="US_X", nombre="Acme", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.commit()
    import app.config as cfg
    import app.services.precios as precios
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"US_X": {"sector": "Technology"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_X": (Decimal("100"), "EUR")})

    a = svc.analizar_contexto(db, cartera.id, "US_X")
    assert a.nombre == "Acme"
    assert a.clasificacion in ("COYUNTURAL", "GRIS", "ESTRUCTURAL")  # mock → COYUNTURAL
    assert len(a.preguntas) == 5 and a.fuentes
    assert a.fecha == date.today().isoformat()
