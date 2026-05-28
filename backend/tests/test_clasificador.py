"""Tests del clasificador IA de bloques (Roadmap 1.6) — offline (proveedor mock)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.adapters.ia import ClasificadorError, get_clasificador
from app.adapters.ia.base import BloqueOpcion, ContextoEmpresa
from app.adapters.ia.mock import MockClasificador
from app.adapters.ia.prompt import (
    build_mensajes,
    build_mensajes_lote,
    parse_respuesta,
    parse_respuesta_lote,
)
from app.db import models
from app.services import clasificador as svc


def _pos(db, cartera, isin, nombre, qty, coste, precio) -> models.Posicion:
    p = models.Posicion(cartera_id=cartera.id, isin=isin, nombre=nombre,
                        divisa_local="USD", precio_manual_eur=Decimal(str(precio)))
    db.add(p); db.flush()
    db.add(models.Lot(
        posicion_id=p.id, fecha_compra=date(2024, 1, 1),
        cantidad_inicial=Decimal(str(qty)), cantidad_restante=Decimal(str(qty)),
        coste_unit_eur=Decimal(str(coste)) / Decimal(str(qty)),
        coste_total_eur=Decimal(str(coste)), gastos_eur=Decimal("0"),
    ))
    db.flush()
    return p


def _bloque(db, cartera, nombre, cat) -> models.Bloque:
    b = models.Bloque(cartera_id=cartera.id, nombre=nombre, categoria_base=cat,
                      orden=0, es_base=True)
    db.add(b); db.flush()
    return b


# ── prompt: parser ──────────────────────────────────────────────────────────

def _cat(c) -> list[BloqueOpcion]:
    return c


def test_parse_respuesta_tolera_envoltura_y_valida() -> None:
    cat = [BloqueOpcion(id="B-GROW", nombre="Growth", categoria_base="growth", rol="")]
    # JSON entre prosa y ```fences```.
    texto = 'Claro:\n```json\n{"categoria_base":"growth","bloque_id":"B-GROW",' \
            '"razonamiento":"Compounder","confianza":0.9}\n```\nEspero que ayude.'
    s = parse_respuesta(texto, cat, "m", "claude_cli")
    assert s.categoria_base == "growth"
    assert s.bloque_id == "B-GROW"
    assert s.confianza == 0.9
    assert s.proveedor == "claude_cli"


def test_parse_respuesta_mapea_bloque_por_categoria_si_falta_o_invalido() -> None:
    cat = [BloqueOpcion(id="B-INC", nombre="Income", categoria_base="income", rol="")]
    # bloque_id null + confianza fuera de rango (se acota).
    s = parse_respuesta('{"categoria_base":"income","bloque_id":null,'
                        '"razonamiento":"x","confianza":5}', cat, "m", "mock")
    assert s.bloque_id == "B-INC"          # mapeado por categoría
    assert s.confianza == 1.0              # acotado a [0,1]


def test_parse_respuesta_recupera_de_comillas_sin_escapar() -> None:
    """JSON roto por comillas internas (caso real de opus): se recuperan
    categoría y confianza por regex en vez de fallar."""
    cat = [BloqueOpcion(id="G", nombre="G", categoria_base="growth", rol="")]
    roto = ('{"categoria_base": "growth", "bloque_id": "growth", "razonamiento": '
            '"S&P Global es un "compounder" de calidad", "confianza": 0.9}')
    s = parse_respuesta(roto, cat, "opus", "exp")
    assert s.categoria_base == "growth"
    assert s.bloque_id == "G"
    assert s.confianza == 0.9


def test_parse_respuesta_lote_recupera_objetos_rotos() -> None:
    emps = [ContextoEmpresa(isin="A", nombre="A"), ContextoEmpresa(isin="B", nombre="B")]
    cat = [BloqueOpcion(id="G", nombre="G", categoria_base="growth", rol=""),
           BloqueOpcion(id="D", nombre="D", categoria_base="defensivo", rol="")]
    # Array con comillas sin escapar en un razonamiento → fallback regex por objeto.
    roto = ('[{"isin": "A", "categoria_base": "growth", "razonamiento": "un "moat" claro", "confianza": 0.8},'
            '{"isin": "B", "categoria_base": "defensivo", "confianza": 0.7}]')
    out = parse_respuesta_lote(roto, emps, cat, "opus", "exp")
    assert {s.isin: s.categoria_base for s in out} == {"A": "growth", "B": "defensivo"}


def test_parse_respuesta_categoria_invalida_o_no_json() -> None:
    with pytest.raises(ClasificadorError):
        parse_respuesta('{"categoria_base":"inventada"}', [], "m", "mock")
    with pytest.raises(ClasificadorError):
        parse_respuesta("no hay json aquí", [], "m", "mock")


def test_build_mensajes_incluye_contexto_y_catalogo() -> None:
    ctx = ContextoEmpresa(isin="US1", nombre="Acme", sector="Tech",
                          yield_pct=0.012, cagr4_div_pct=0.18, beta=0.85, roe=0.31)
    cat = [BloqueOpcion(id="B-GROW", nombre="Growth", categoria_base="growth", rol="")]
    system, user = build_mensajes(ctx, cat)
    assert "growth" in system and "JSON" in system
    assert "Acme" in user and "Tech" in user and "B-GROW" in user
    assert "1.2%" in user                  # yield formateado
    assert "0.85" in user and "31.0%" in user   # beta + ROE en el contexto


# ── mock + factory ────────────────────────────────────────────────────────

def test_mock_determinista_por_yield_y_crecimiento() -> None:
    m = MockClasificador()
    cat = [
        BloqueOpcion(id="G", nombre="Growth", categoria_base="growth", rol=""),
        BloqueOpcion(id="A", nombre="Acel", categoria_base="aggressive", rol=""),
    ]
    grow = m.clasificar(ContextoEmpresa(isin="x", nombre="x", yield_pct=0.005,
                                        crecimiento_eps_pct=0.20), cat)
    assert grow.categoria_base == "growth" and grow.bloque_id == "G"
    acel = m.clasificar(ContextoEmpresa(isin="y", nombre="y", yield_pct=0.09), cat)
    assert acel.categoria_base == "aggressive" and acel.bloque_id == "A"


def test_factory_segun_proveedor(monkeypatch) -> None:
    assert isinstance(get_clasificador("mock"), MockClasificador)
    from app.adapters.ia.claude_cli import ClaudeCliClasificador
    assert isinstance(get_clasificador("claude_cli"), ClaudeCliClasificador)
    with pytest.raises(ClasificadorError):
        get_clasificador("inexistente")


# ── servicio: contexto + sugerir (no muta) ──────────────────────────────────

def _mock_feed(monkeypatch, isin, precio, sector="Technology") -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {isin: {"sector": sector, "industry": "Software",
                                                "pe": 30.0, "dividend": 0.0,
                                                "beta": 1.1, "roe": 0.42}})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {isin: (Decimal(str(precio)), "USD")})


def test_construir_contexto_desde_feed(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_MSFT", "Microsoft", 10, 1000, 100)
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_MSFT", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("30"),
                            metrica_base_4y=Decimal("10")))
    db.commit()
    _mock_feed(monkeypatch, "US_MSFT", 100)

    ctx = svc.construir_contexto(db, cartera.id, "US_MSFT")
    assert ctx.nombre == "Microsoft"
    assert ctx.sector == "Technology"
    assert ctx.per == 30.0
    assert ctx.beta == 1.1 and ctx.roe == 0.42       # beta + ROE llegan al contexto
    assert ctx.cagr4_div_pct is not None and ctx.cagr4_div_pct > 0   # 30×10=300 vs 100


def test_sugerir_no_muta_posicion(db: Session, cartera, monkeypatch) -> None:
    p = _pos(db, cartera, "US_MSFT", "Microsoft", 10, 1000, 100)
    bloque = _bloque(db, cartera, "Growth", "growth")
    db.add(models.Estimacion(cartera_id=cartera.id, isin="US_MSFT", tipo_val="PER",
                            eps_actual=Decimal("5"), multiplo_objetivo=Decimal("30"),
                            metrica_base_4y=Decimal("12")))
    db.commit()
    _mock_feed(monkeypatch, "US_MSFT", 100)

    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")

    s = svc.sugerir(db, cartera.id, "US_MSFT")
    assert s.categoria_base == "growth"      # yield 0, crecimiento alto
    assert s.bloque_id == bloque.id
    db.refresh(p)
    assert p.bloque_id is None               # la sugerencia NO asigna


def test_sugerir_posicion_inexistente(db: Session, cartera, monkeypatch) -> None:
    from fastapi import HTTPException
    _mock_feed(monkeypatch, "US_X", 10)
    with pytest.raises(HTTPException):
        svc.construir_contexto(db, cartera.id, "NO_EXISTE")


# ── lote (autoclasificar) ────────────────────────────────────────────────────

def test_parse_respuesta_lote_mapea_por_isin_y_omite_faltantes() -> None:
    emps = [ContextoEmpresa(isin="A", nombre="A"),
            ContextoEmpresa(isin="B", nombre="B"),
            ContextoEmpresa(isin="C", nombre="C")]
    cat = [BloqueOpcion(id="G", nombre="G", categoria_base="growth", rol=""),
           BloqueOpcion(id="I", nombre="I", categoria_base="income", rol="")]
    # B trae categoría inválida (se omite), C no aparece (se omite).
    texto = ('[{"isin":"A","categoria_base":"growth","confianza":0.8},'
             '{"isin":"B","categoria_base":"inventada","confianza":0.9}]')
    out = parse_respuesta_lote(texto, emps, cat, "m", "claude_cli")
    assert [s.isin for s in out] == ["A"]
    assert out[0].categoria_base == "growth" and out[0].bloque_id == "G"


def test_build_mensajes_lote_compacto() -> None:
    emps = [ContextoEmpresa(isin="US1", nombre="Acme", sector="Tech", yield_pct=0.02)]
    cat = [BloqueOpcion(id="G", nombre="Growth", categoria_base="growth", rol="")]
    system, user = build_mensajes_lote(emps, cat)
    assert "array JSON" in system
    assert "US1" in user and "Acme" in user and "growth" in user


def test_mock_lote(db) -> None:
    m = MockClasificador()
    emps = [ContextoEmpresa(isin="A", nombre="A", yield_pct=0.09),
            ContextoEmpresa(isin="B", nombre="B", crecimiento_eps_pct=0.20)]
    cat = [BloqueOpcion(id="AG", nombre="Acel", categoria_base="aggressive", rol=""),
           BloqueOpcion(id="GR", nombre="Grow", categoria_base="growth", rol="")]
    out = m.clasificar_lote(emps, cat)
    assert [s.isin for s in out] == ["A", "B"]
    assert out[0].categoria_base == "aggressive" and out[1].categoria_base == "growth"


def test_autoclasificar_solo_sin_clasificar_no_muta(db: Session, cartera, monkeypatch) -> None:
    sinclas = _pos(db, cartera, "US_A", "Alpha", 10, 1000, 100)
    bloque = _bloque(db, cartera, "Growth", "growth")
    ya = _pos(db, cartera, "US_B", "Beta", 10, 1000, 50)
    ya.bloque_id = bloque.id                     # ya clasificada → se excluye
    db.commit()

    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {"US_A": {"sector": "Tech"}, "US_B": {"sector": "Tech"}})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {"US_A": (Decimal("100"), "USD"),
                                         "US_B": (Decimal("50"), "USD")})
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")

    sugs = svc.autoclasificar(db, cartera.id, solo_sin_clasificar=True)
    assert [s.isin for s in sugs] == ["US_A"]    # solo la no clasificada
    db.refresh(sinclas)
    assert sinclas.bloque_id is None             # NO asigna, solo sugiere


def test_autoclasificar_isines_explicitos(db: Session, cartera, monkeypatch) -> None:
    """Con `isines` dado, clasifica solo ese batch (chunking dirigido por el front)."""
    _pos(db, cartera, "US_A", "Alpha", 10, 1000, 100)
    _pos(db, cartera, "US_B", "Beta", 10, 1000, 50)
    _pos(db, cartera, "US_C", "Gamma", 10, 1000, 50)
    _bloque(db, cartera, "Growth", "growth")
    db.commit()

    import app.services.precios as precios
    monkeypatch.setattr(precios, "fundamentales_por_isin",
                        lambda db, cid: {i: {"sector": "Tech"} for i in ("US_A", "US_B", "US_C")})
    monkeypatch.setattr(precios, "precios_nativos",
                        lambda db, cid: {i: (Decimal("50"), "USD") for i in ("US_A", "US_B", "US_C")})
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")

    sugs = svc.autoclasificar(db, cartera.id, isines=["US_A", "US_C"])
    assert sorted(s.isin for s in sugs) == ["US_A", "US_C"]   # B excluida del batch

    candidatos = svc.isines_autoclasificables(db, cartera.id)
    assert sorted(candidatos) == ["US_A", "US_B", "US_C"]     # todas sin clasificar


# ── compuertas deterministas (pregate) + few-shot + distribución ────────────

def test_pregate_compuertas_deterministas() -> None:
    cripto = ContextoEmpresa(isin="XF000BTC0001", nombre="Bitcoin", tipo_activo="CRYPTO")
    assert svc.pregate(cripto) == "satelite"                  # sin bloque Cripto
    assert svc.pregate(cripto, cripto_disponible=True) == "cripto"  # con bloque Cripto
    amplio = ContextoEmpresa(isin="IE0001", nombre="iShares MSCI World", tipo_activo="ETF")
    assert svc.pregate(amplio) == "indice"
    tema = ContextoEmpresa(isin="IE0002", nombre="SPDR MSCI USA Tech", tipo_activo="ETF")
    assert svc.pregate(tema) == "satelite"
    hy = ContextoEmpresa(isin="US0001", nombre="Un REIT", tipo_activo="STOCK", yield_pct=0.09)
    assert svc.pregate(hy) == "aggressive"
    normal = ContextoEmpresa(isin="US0002", nombre="Microsoft", tipo_activo="STOCK",
                             yield_pct=0.01)
    assert svc.pregate(normal) is None


def test_sugerir_cripto_por_regla_sin_ia(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "XF000BTC0001", "Bitcoin", 1, 1000, 30000)
    _bloque(db, cartera, "Satélite", "satelite")
    db.commit()
    _mock_feed(monkeypatch, "XF000BTC0001", 30000)
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "ia_provider", "mock")
    # Si la IA se invocara, el mock daría 'defensivo' (yield 0); la regla da 'satelite'.
    s = svc.sugerir(db, cartera.id, "XF000BTC0001")
    assert s.proveedor == "regla"
    assert s.categoria_base == "satelite"
    assert s.distribucion == [{"categoria": "satelite", "prob": 1.0}]


def test_sugerir_inyecta_overrides_como_fewshot(db: Session, cartera, monkeypatch) -> None:
    _pos(db, cartera, "US_MSFT", "Microsoft", 10, 1000, 100)   # acción normal, sin gate
    _bloque(db, cartera, "Compounders", "growth")
    db.commit()
    _mock_feed(monkeypatch, "US_MSFT", 100)
    from app.services import bloques as bsvc
    bsvc.registrar_override(db, cartera.id, "DK_NOVO", "Novo", "growth", "defensivo",
                            0.8, "lo siento defensivo")
    db.commit()

    capturado: dict = {}

    class _Fake:
        def clasificar(self, ctx, catalogo, ejemplos=None):  # type: ignore[no-untyped-def]
            capturado["ejemplos"] = ejemplos
            from app.adapters.ia.base import SugerenciaBloque
            return SugerenciaBloque("growth", None, "r", 0.5, "fake", "fake")

    monkeypatch.setattr(svc, "get_clasificador", lambda *a, **k: _Fake())
    svc.sugerir(db, cartera.id, "US_MSFT")
    assert capturado["ejemplos"]
    assert capturado["ejemplos"][0]["categoria_elegida"] == "defensivo"
    assert capturado["ejemplos"][0]["razon"] == "lo siento defensivo"


def test_construir_contexto_desde_seguimiento(db: Session, cartera, monkeypatch) -> None:
    """Una empresa del watchlist (sin posición) también se puede clasificar."""
    db.add(models.Seguimiento(cartera_id=cartera.id, isin="US_NVDA", ticker="NVDA",
                              nombre="Nvidia", divisa="USD"))
    db.commit()
    _mock_feed(monkeypatch, "US_NVDA", 120, sector="Technology")
    ctx = svc.construir_contexto(db, cartera.id, "US_NVDA")
    assert ctx.nombre == "Nvidia"
    assert ctx.sector == "Technology"
    assert ctx.tipo_activo == "STOCK"


def test_parse_respuesta_distribucion_opcional() -> None:
    cat = [BloqueOpcion(id="G", nombre="G", categoria_base="growth", rol="")]
    con = parse_respuesta(
        '{"categoria_base":"growth","bloque_id":"G","razonamiento":"x",'
        '"confianza":0.7,"distribucion":[{"categoria":"growth","prob":0.7},'
        '{"categoria":"income","prob":0.3},{"categoria":"xx","prob":0.1}]}',
        cat, "m", "mock")
    assert con.distribucion == [{"categoria": "growth", "prob": 0.7},
                                {"categoria": "income", "prob": 0.3}]   # 'xx' filtrado
    sin = parse_respuesta('{"categoria_base":"growth","bloque_id":"G",'
                          '"razonamiento":"x","confianza":0.7}', cat, "m", "mock")
    assert sin.distribucion is None
