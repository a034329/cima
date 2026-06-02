"""Tests de la auto-clasificación del régimen macro (clasificador numérico,
parseo de la IA, propuesta firmable, endpoints + ciclo POST/GET/firmar)."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.ia.base import (
    BloqueOpcion, ContextoEmpresa, SugerenciaBloque,
)
from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services import regimen as svc_regimen
from app.services import regimen_auto as ra


# ── clasificador numérico (puro) ────────────────────────────────────────────

def test_clasificacion_numerica_verde() -> None:
    d = {"sp_drawdown": -0.02, "vix": 15.0, "brent_usd": 70.0,
         "yield_curve_spread_pp": 1.2}
    pre = ra._clasificacion_numerica(d)
    assert pre["mercado"].senal == "VERDE"
    assert pre["geopolitica"].senal == "VERDE"
    assert pre["ciclo"].senal == "VERDE"
    # razón con números del feed
    assert "S&P" in pre["mercado"].razon and "VIX 15" in pre["mercado"].razon


def test_clasificacion_numerica_mercado_peor_color_manda() -> None:
    # DD bajo (VERDE) pero VIX alto (ROJA) → ROJA: el peor color manda.
    pre = ra._clasificacion_numerica({"sp_drawdown": -0.03, "vix": 30.0})
    assert pre["mercado"].senal == "ROJA"


def test_clasificacion_numerica_ciclo_curva_invertida() -> None:
    pre = ra._clasificacion_numerica({"yield_curve_spread_pp": -0.35})
    assert pre["ciclo"].senal == "ROJA"
    assert "invertida" in pre["ciclo"].razon


def test_clasificacion_numerica_sin_datos() -> None:
    assert ra._clasificacion_numerica(None) == {}
    assert ra._clasificacion_numerica({}) == {}


# ── parser JSON ─────────────────────────────────────────────────────────────

def _payload_ia_valido() -> str:
    return json.dumps({
        "ciclo":       {"senal": "AMARILLA", "razon": "Paro 4,4%, GDP 1,8%",
                        "fuentes": ["BLS 2026-05"]},
        "inflacion":   {"senal": "ROJA", "razon": "PCE 2,9% y Fed sin margen",
                        "fuentes": ["fed.gov 2026-05"]},
        "geopolitica": {"senal": "AMARILLA", "razon": "Brent 90, tensión Ormuz",
                        "fuentes": ["Reuters 2026-05-28"]},
        "mercado":     {"senal": "VERDE", "razon": "VIX bajo, SP500 cerca máximos",
                        "fuentes": []},
    })


def test_parser_acepta_json_puro() -> None:
    out = ra._parse_respuesta(_payload_ia_valido())
    assert set(out.keys()) == set(svc_regimen.INDICADORES)
    assert out["inflacion"].senal == "ROJA"
    assert out["geopolitica"].fuentes == ["Reuters 2026-05-28"]


def test_parser_tolera_markdown_y_preambulo() -> None:
    raw = "Aquí tienes:\n```json\n" + _payload_ia_valido() + "\n```\nUn saludo."
    out = ra._parse_respuesta(raw)
    assert out["mercado"].senal == "VERDE"


def test_parser_falla_si_falta_indicador() -> None:
    raw = json.dumps({"ciclo": {"senal": "VERDE", "razon": ""}})
    with pytest.raises(ValueError):
        ra._parse_respuesta(raw)


def test_parser_falla_si_senal_invalida() -> None:
    raw = json.dumps({k: {"senal": "AZUL", "razon": ""} for k in svc_regimen.INDICADORES})
    with pytest.raises(ValueError):
        ra._parse_respuesta(raw)


# ── proponer / firmar (servicio, con IA y feed mockeados) ──────────────────

class _IAFalsa:
    """Cliente de IA que devuelve un JSON fijo y no toca red."""
    proveedor = "test"
    modelo = "fake-1"

    def __init__(self, payload: str | None = None) -> None:
        self.payload = payload or _payload_ia_valido()
        self.system_visto: str | None = None
        self.user_visto: str | None = None

    def investigar(self, system: str, user: str, timeout_s: int | None = None) -> str:
        self.system_visto, self.user_visto = system, user
        return self.payload

    # Métodos sobrantes del protocolo (no se invocan aquí).
    def clasificar(self, ctx: ContextoEmpresa, catalogo, ejemplos=None) -> SugerenciaBloque:  # noqa: ARG002
        raise NotImplementedError

    def clasificar_lote(self, empresas, catalogo) -> list[SugerenciaBloque]:  # noqa: ARG002
        raise NotImplementedError

    def completar(self, system: str, user: str, timeout_s: int | None = None) -> str:  # noqa: ARG002
        return self.payload


@pytest.fixture()
def cartera(db) -> models.Cartera:
    u = models.User(email="a@a.a", modo="owner")
    db.add(u); db.flush()
    c = models.Cartera(user_id=u.id, nombre="Principal")
    db.add(c); db.commit()
    return c


def _feed_amarillo() -> dict:
    return {"sp_drawdown": -0.07, "vix": 22.0, "brent_usd": 90.0,
            "wti_usd": 86.0, "yield_curve_spread_pp": 0.2}


def test_proponer_persiste_y_devuelve_propuesta(monkeypatch, db, cartera) -> None:
    monkeypatch.setattr(ra, "datos_macro_objetivos", _feed_amarillo)
    ia = _IAFalsa()
    p = ra.proponer(db, cartera.id, ia=ia)
    assert p.regimen == "AMARILLO"        # 1V + 2A + 1R
    assert p.indicadores["inflacion"].senal == "ROJA"
    # El user prompt debe incluir las cifras y la pre-clasificación
    assert "Brent: 90" in ia.user_visto
    assert "Pre-clasificación numérica" in ia.user_visto
    # El feed numérico se funde dentro de cada indicador clasificable
    assert p.indicadores["mercado"].datos.get("vix") == 22.0
    assert p.indicadores["geopolitica"].datos.get("brent_usd") == 90.0

    # Releer desde BD
    p2 = ra.cargar_propuesta(db, cartera.id)
    assert p2 is not None
    assert p2.regimen == "AMARILLO"
    assert p2.indicadores["mercado"].senal == "VERDE"


def test_proponer_es_upsert_no_duplica(monkeypatch, db, cartera) -> None:
    monkeypatch.setattr(ra, "datos_macro_objetivos", _feed_amarillo)
    ra.proponer(db, cartera.id, ia=_IAFalsa())
    time.sleep(0.01)
    ra.proponer(db, cartera.id, ia=_IAFalsa())
    n = db.query(models.PropuestaRegimen).filter_by(cartera_id=cartera.id).count()
    assert n == 1


def test_firmar_aplica_la_propuesta_al_regimen(monkeypatch, db, cartera) -> None:
    monkeypatch.setattr(ra, "datos_macro_objetivos", _feed_amarillo)
    ra.proponer(db, cartera.id, ia=_IAFalsa())
    estado = ra.firmar(db, cartera.id)
    assert estado.regimen == "AMARILLO"
    assert estado.indicadores["inflacion"] == "ROJA"
    # Persistido en Cartera.regimen_macro_json
    c = db.get(models.Cartera, cartera.id)
    assert c.regimen_macro_json
    payload = json.loads(c.regimen_macro_json)
    assert payload["inflacion"] == "ROJA"


def test_firmar_sin_propuesta_falla(db, cartera) -> None:
    with pytest.raises(ValueError):
        ra.firmar(db, cartera.id)


def test_descartar_borra_la_propuesta(monkeypatch, db, cartera) -> None:
    monkeypatch.setattr(ra, "datos_macro_objetivos", _feed_amarillo)
    ra.proponer(db, cartera.id, ia=_IAFalsa())
    assert ra.cargar_propuesta(db, cartera.id) is not None
    ra.descartar_propuesta(db, cartera.id)
    assert ra.cargar_propuesta(db, cartera.id) is None


# ── router (POST /auto → GET /auto → POST /firmar) ──────────────────────────

@pytest.fixture()
def client(monkeypatch):
    import app.services.precios as precios
    from app.services import jobs as jobs_mod
    monkeypatch.setattr(precios, "mercado_correccion", lambda: None)
    monkeypatch.setattr(ra, "datos_macro_objetivos", _feed_amarillo)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    TS = sessionmaker(bind=eng)

    def override():
        s = TS()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    s = TS()
    u = models.User(email="a@a.a", modo="owner"); s.add(u); s.flush()
    s.add(models.Cartera(user_id=u.id, nombre="P")); s.commit(); s.close()

    # Adaptador IA fake (sin red) + ejecución síncrona contra el mismo engine
    # del cliente (el worker real abre SessionLocal de prod, que no comparte BD
    # con la in-memory del test).
    monkeypatch.setattr(ra, "get_clasificador", lambda *a, **k: _IAFalsa())

    def lanzar_sync(db, cartera_id, isin, tipo, fn):  # noqa: ARG001
        jobs_mod._ejecutar(cartera_id, isin, tipo, fn, session_factory=TS)

    monkeypatch.setattr(jobs_mod, "lanzar", lanzar_sync)
    # El router importa `jobs` desde su propio módulo — reapuntar también ahí.
    from app.routers import regimen as router_mod
    monkeypatch.setattr(router_mod.jobs, "lanzar", lanzar_sync)

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_router_ciclo_completo(client) -> None:
    # 1) GET inicial → ninguno
    r = client.get("/api/regimen/auto")
    assert r.status_code == 200 and r.json()["estado"] == "ninguno"

    # 2) POST → lanza el "job" (síncrono via monkeypatch). Lectura directa.
    r = client.post("/api/regimen/auto")
    assert r.status_code == 202
    estado = client.get("/api/regimen/auto").json()
    assert estado["estado"] == "ok"
    assert estado["propuesta"]["regimen"] == "AMARILLO"
    assert estado["propuesta"]["indicadores"]["inflacion"]["senal"] == "ROJA"

    # 3) Firmar aplica al régimen vigente
    r = client.post("/api/regimen/firmar")
    assert r.status_code == 200
    body = r.json()
    assert body["regimen"] == "AMARILLO"
    assert body["indicadores"]["inflacion"] == "ROJA"

    # 4) DELETE descarta la propuesta (queda solo el régimen ya aplicado)
    r = client.delete("/api/regimen/auto")
    assert r.status_code == 204


def test_router_firmar_sin_propuesta_409(client) -> None:
    r = client.post("/api/regimen/firmar")
    assert r.status_code == 409
