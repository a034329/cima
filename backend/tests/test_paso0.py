"""Tests de PASO 0 (parse tolerante + analizar_contexto con IA mock, sin red)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
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
    # mock devuelve COYUNTURAL sin keywords problemáticas → no requiere 0B
    assert a.requiere_0b is False
    assert a.motivo_0b == ""


# ── PASO 0B: disparadores y 2ª búsqueda ──────────────────────────────────


@pytest.mark.parametrize("clasif", ["ESTRUCTURAL", "GRIS"])
def test_disparador_0b_activo_por_clasificacion(clasif: str) -> None:
    req, motivo = svc.detectar_disparador_0b(clasif, "resumen neutro", "riesgo genérico")
    assert req is True
    assert clasif in motivo


def test_disparador_0b_activo_por_keyword_geopolitica() -> None:
    req, motivo = svc.detectar_disparador_0b(
        "COYUNTURAL",
        "Caída del 12% por tensiones geopolíticas en Oriente Medio",
        "Conflicto activo en la región",
    )
    assert req is True
    assert "geopolític" in motivo or "conflicto" in motivo


def test_disparador_0b_activo_por_keyword_reembolsos() -> None:
    req, motivo = svc.detectar_disparador_0b(
        "GRIS",
        "Cierre temporal de ventanas de reembolsos en el fondo principal.",
        "Riesgo de liquidez",
    )
    assert req is True
    # Clasificación GRIS ya es disparador; motivo debería citarla.
    assert "GRIS" in motivo


def test_disparador_0b_activo_por_keyword_legal() -> None:
    req, motivo = svc.detectar_disparador_0b(
        "COYUNTURAL",
        "La empresa enfrenta una demanda colectiva por prácticas comerciales.",
        "Posible multa regulatoria",
    )
    assert req is True
    assert any(k in motivo.lower() for k in ("demanda", "multa", "regulator"))


def test_disparador_0b_inactivo_si_coyuntural_sin_keywords() -> None:
    req, motivo = svc.detectar_disparador_0b(
        "COYUNTURAL",
        "Compresión temporal de márgenes por costes energéticos.",
        "Sin riesgo cualificado, ruido de corto plazo.",
    )
    assert req is False
    assert motivo == ""


def test_parse_0b_extrae_json_con_segmentos() -> None:
    texto = (
        '```json\n'
        '{"causa_exacta": "Demanda por publicidad engañosa anunciada 2026-03-15.",'
        ' "profundidad": "media",'
        ' "horizonte_resolucion": "6-12 meses",'
        ' "segmentos_afectados": [{"nombre": "Publicidad", "peso_pct": 12, "impacto": "Reducción de ingresos"}],'
        ' "evidencias": ["SEC filing 8-K 2026-03-15", "Caída 7% intradía"],'
        ' "conclusion": "REFUERZA: el evento confirma la lectura gris.",'
        ' "nueva_clasificacion": "gris",'
        ' "fuentes": ["https://x.com/a", ""]}\n```'
    )
    d = svc.parse_0b(texto)
    assert d["profundidad"] == "MEDIA"           # normalizado a mayúsculas
    assert d["nueva_clasificacion"] == "GRIS"
    assert len(d["segmentos_afectados"]) == 1
    assert len(d["evidencias"]) == 2
    assert d["fuentes"] == ["https://x.com/a"]    # vacío filtrado


def test_parse_0b_profundidad_invalida_a_sin_datos() -> None:
    d = svc.parse_0b('{"profundidad": "INVENTADA", "causa_exacta": "x"}')
    assert d["profundidad"] == "SIN_DATOS"
    assert d["nueva_clasificacion"] == ""          # ausente → vacío (mantiene)


def test_parse_0b_fallback_si_no_hay_json() -> None:
    d = svc.parse_0b("no encontré causa raíz")
    assert d["profundidad"] == "SIN_DATOS"
    assert "no encontré" in d["causa_exacta"]


def test_analizar_causa_raiz_mock(db: Session, cartera, monkeypatch) -> None:
    """0B con IA mockeada: la 2ª búsqueda devuelve causa exacta + profundidad."""
    p = models.Posicion(cartera_id=cartera.id, isin="US_Y", nombre="Beta Corp",
                        divisa_local="EUR")
    db.add(p); db.flush()
    db.add(models.Lot(posicion_id=p.id, fecha_compra=date(2024, 1, 1),
                      cantidad_inicial=Decimal("5"), cantidad_restante=Decimal("5"),
                      coste_unit_eur=Decimal("200"), coste_total_eur=Decimal("1000"),
                      gastos_eur=Decimal("0")))
    db.commit()

    import app.config as cfg
    import app.services.precios as precios
    from app.adapters.ia import mock as mock_mod
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"US_Y": {"sector": "Financials"}})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_Y": (Decimal("200"), "EUR")})

    # Forzamos respuesta 0B específica (el mock genérico devuelve respuesta del 0).
    respuesta_0b = (
        '{"causa_exacta": "Apertura de investigación regulatoria en Q1 2026.",'
        ' "profundidad": "GRAVE",'
        ' "horizonte_resolucion": "abierto",'
        ' "segmentos_afectados": [{"nombre": "Asset Management", "peso_pct": 65, "impacto": "Salida de inversores"}],'
        ' "evidencias": ["Comunicado regulador 2026-04-02"],'
        ' "conclusion": "CAMBIA a ESTRUCTURAL: moat de confianza atacado.",'
        ' "nueva_clasificacion": "ESTRUCTURAL",'
        ' "fuentes": ["https://regulador.example/comunicado"]}'
    )
    monkeypatch.setattr(
        mock_mod.MockClasificador, "investigar",
        lambda self, system, user, timeout_s=None: respuesta_0b,
    )

    a = svc.analizar_causa_raiz(
        db, cartera.id, "US_Y",
        contexto_previo={"resumen": "Caída fuerte",
                         "clasificacion": "GRIS",
                         "riesgo_principal": "Investigación regulatoria"},
    )
    assert a.nombre == "Beta Corp"
    assert a.profundidad == "GRAVE"
    assert a.nueva_clasificacion == "ESTRUCTURAL"
    assert len(a.segmentos_afectados) == 1
    assert a.segmentos_afectados[0]["peso_pct"] == 65
    assert a.fuentes and a.fecha == date.today().isoformat()


def test_build_prompt_0b_inyecta_contexto_previo() -> None:
    """El prompt del 0B debe incluir el resumen del 0 para no rehacer la 1ª pasada."""
    system, user = svc.build_prompt_0b(
        "Acme", "Tech",
        {"resumen": "Compresión de márgenes", "clasificacion": "GRIS",
         "riesgo_principal": "Litigio antimonopolio"},
    )
    assert "GRIS" in user
    assert "Compresión" in user
    assert "Litigio" in user
    # El system debe pedir disección del negocio (PASO 0A doctrina).
    assert "SEGMENTO" in system or "segmento" in system


def test_build_prompt_0b_sin_contexto_previo() -> None:
    system, user = svc.build_prompt_0b("Acme", None, None)
    assert "Acme" in user
    # Sin contexto previo, no debe haber sección "Contexto inicial"
    assert "Contexto inicial" not in user
