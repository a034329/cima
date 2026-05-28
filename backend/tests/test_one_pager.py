"""Tests del one-pager (parse tolerante + generar/persistir con IA fake, sin red)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import models
from app.services import one_pager as svc

_JSON = (
    'Aquí tienes:\n```json\n'
    '{"que_hace": "Software empresarial.", "tesis": "Crece a doble dígito.", '
    '"riesgos": "Competencia en cloud.", "valoracion": "Por debajo de su PER histórico.", '
    '"encaje": "Núcleo Compounder.", "veredicto": "Mantener y reforzar en correcciones.", '
    '"clasificacion": "coyuntural", "fuentes": ["https://x.com/a", ""]}\n```\n'
)


class _FakeIA:
    def investigar(self, system: str, user: str) -> str:
        return _JSON


def test_parse_extrae_secciones_y_fuentes() -> None:
    op = svc.parse(_JSON, "Acme", "US_X")
    assert op.que_hace.startswith("Software")
    assert op.clasificacion == "COYUNTURAL"
    assert op.fuentes == ["https://x.com/a"]          # vacío filtrado
    assert op.veredicto.startswith("Mantener")


def test_parse_fallback_si_no_json() -> None:
    op = svc.parse("no pude generar JSON pero aquí va el análisis", "Acme", "US_X")
    assert op.clasificacion == ""                      # sin clasificación válida
    assert "no pude generar" in op.veredicto           # texto crudo al veredicto


def test_generar_persiste_y_relee(db: Session, cartera, monkeypatch) -> None:
    p = models.Posicion(cartera_id=cartera.id, isin="US_X", nombre="Acme", divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("10"), cantidad_restante=Decimal("10"),
                      coste_unit_eur=Decimal("100"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_X", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("20"),
                            metrica_base_4y=Decimal("10")))
    db.commit()
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"US_X": {"sector": "Technology"}})
    monkeypatch.setattr(precios, "precios_nativos", lambda db, cid: {"US_X": (Decimal("100"), "EUR")})
    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _FakeIA())

    assert svc.guardado(db, cartera.id, "US_X") is None      # nada guardado aún

    op = svc.generar(db, cartera.id, "US_X")
    assert op.nombre == "Acme" and op.clasificacion == "COYUNTURAL" and op.fuentes
    assert op.fecha == date.today().isoformat()

    re = svc.guardado(db, cartera.id, "US_X")                 # ahora SÍ está persistido
    assert re is not None and re.veredicto == op.veredicto and re.que_hace == op.que_hace
