"""Tests del régimen macro (derivación, persistencia, calibración, router)."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services import regimen as svc


# ── derivación ───────────────────────────────────────────────────────────────

def test_derivar_mayoria() -> None:
    # 2 verdes + 1 amarilla + 1 roja → VERDE
    assert svc.derivar_regimen(
        {"ciclo": "VERDE", "inflacion": "VERDE", "geopolitica": "AMARILLA", "mercado": "ROJA"}
    ) == "VERDE"


def test_derivar_empate_va_al_mas_cauto() -> None:
    # 2 verdes vs 2 rojas → empate → el más cauto (ROJO)
    assert svc.derivar_regimen(
        {"ciclo": "VERDE", "inflacion": "VERDE", "geopolitica": "ROJA", "mercado": "ROJA"}
    ) == "ROJO"


def test_derivar_ejemplo_doctrina() -> None:
    # 1 roja + 2 amarillas + 1 verde → AMARILLO (caso del CLAUDE.md)
    assert svc.derivar_regimen(
        {"ciclo": "AMARILLA", "inflacion": "ROJA", "geopolitica": "AMARILLA", "mercado": "VERDE"}
    ) == "AMARILLO"


def test_tramos_para() -> None:
    estado = svc._estado(
        {"ciclo": "AMARILLA", "inflacion": "AMARILLA", "geopolitica": "AMARILLA", "mercado": "AMARILLA"},
        None)
    assert estado.regimen == "AMARILLO" and (estado.tramo_min, estado.tramo_max) == (500, 1000)
    # déficit 4.200 € → ceil(4200/1000)=5 .. ceil(4200/500)=9
    assert svc.tramos_para(Decimal("4200"), estado) == (5, 9)
    assert svc.tramos_para(Decimal("0"), estado) is None
    assert svc.tramos_para(None, estado) is None


# ── persistencia ─────────────────────────────────────────────────────────────

def test_estado_default_amarillo(db: Session, cartera) -> None:
    e = svc.estado_regimen(db, cartera.id)        # sin JSON guardado
    assert e.regimen == "AMARILLO"
    assert all(v == "AMARILLA" for v in e.indicadores.values())
    assert e.actualizado is None


def test_guardar_y_releer(db: Session, cartera) -> None:
    e = svc.guardar_regimen(db, cartera.id, {
        "ciclo": "VERDE", "inflacion": "VERDE", "geopolitica": "VERDE", "mercado": "AMARILLA"})
    assert e.regimen == "VERDE" and (e.tramo_min, e.tramo_max) == (1000, 2000)
    assert e.actualizado is not None
    re = svc.estado_regimen(db, cartera.id)       # vuelve a leerse de BD
    assert re.regimen == "VERDE" and re.indicadores["mercado"] == "AMARILLA"


# ── router ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    # Sin red en los tests: el mercado lo inyectamos por test (default: sin datos).
    import app.services.precios as precios
    monkeypatch.setattr(precios, "mercado_correccion", lambda: None)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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
    s.add(models.Cartera(user_id=u.id, nombre="Principal")); s.commit(); s.close()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_router_put_y_get(client) -> None:
    r = client.put("/api/regimen", json={
        "ciclo": "AMARILLA", "inflacion": "ROJA", "geopolitica": "AMARILLA", "mercado": "VERDE"})
    assert r.status_code == 200
    body = r.json()
    assert body["regimen"] == "AMARILLO" and body["tramo_min"] == 500 and body["tramo_max"] == 1000
    g = client.get("/api/regimen").json()
    assert g["regimen"] == "AMARILLO" and g["indicadores"]["inflacion"] == "ROJA"


def test_router_rechaza_senal_invalida(client) -> None:
    r = client.put("/api/regimen", json={
        "ciclo": "AZUL", "inflacion": "ROJA", "geopolitica": "AMARILLA", "mercado": "VERDE"})
    assert r.status_code == 422


# ── regla del −14% (evaluar_correccion, pura) ────────────────────────────────

def _estado(ciclo="AMARILLA", resto="AMARILLA"):
    ind = {"ciclo": ciclo, "inflacion": resto, "geopolitica": resto, "mercado": resto}
    return svc._estado(ind, None)


def test_correccion_activa_escala_tramo() -> None:
    # S&P −13%, VIX 22, ciclo no recesivo, régimen AMARILLO → escala a 1.000–1.500
    c = svc.evaluar_correccion(_estado(), {"sp_drawdown": -0.13, "vix": 22.0})
    assert c.activa and (c.escalado_min, c.escalado_max) == (1000, 1500)


def test_correccion_bloqueada_por_ciclo_roja() -> None:
    # Ciclo en ROJA → bear market probable, NO escalar aunque haya caída
    c = svc.evaluar_correccion(_estado(ciclo="ROJA"), {"sp_drawdown": -0.13, "vix": 18.0})
    assert not c.activa and "bear market" in c.nota.lower()


def test_correccion_bloqueada_por_vix_panico() -> None:
    c = svc.evaluar_correccion(_estado(), {"sp_drawdown": -0.13, "vix": 40.0})
    assert not c.activa and "pánico" in c.nota.lower()


def test_correccion_sin_caida_suficiente() -> None:
    c = svc.evaluar_correccion(_estado(), {"sp_drawdown": -0.05, "vix": 15.0})
    assert not c.activa and "sin corrección" in c.nota.lower()


def test_correccion_caida_profunda_no_escala() -> None:
    c = svc.evaluar_correccion(_estado(), {"sp_drawdown": -0.28, "vix": 20.0})
    assert not c.activa and "peligro" in c.nota.lower()


def test_correccion_sin_datos_mercado() -> None:
    c = svc.evaluar_correccion(_estado(), None)
    assert not c.activa and "no disponibles" in c.nota.lower()


def test_router_expone_correccion(client, monkeypatch) -> None:
    import app.services.precios as precios
    monkeypatch.setattr(precios, "mercado_correccion", lambda: {"sp_drawdown": -0.14, "vix": 20.0})
    client.put("/api/regimen", json={
        "ciclo": "AMARILLA", "inflacion": "AMARILLA", "geopolitica": "VERDE", "mercado": "AMARILLA"})
    corr = client.get("/api/regimen").json()["correccion"]
    assert corr["activa"] is True and corr["escalado_min"] == 1000
