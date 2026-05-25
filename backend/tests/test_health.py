"""Tests del endpoint /api/health."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_returns_200_y_estructura_correcta() -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "cima-api"
    assert "version" in data
    assert data["mode"] in ("saas", "owner")
    assert data["environment"] in ("dev", "staging", "production")
    assert "timestamp" in data


def test_root_returns_metadata() -> None:
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "cima-api"
    assert data["docs"] == "/docs"


def test_cartera_mock_returns_estructura_final() -> None:
    """El endpoint /api/cartera devuelve la estructura que usará el frontend
    desde Fase 1 — actualmente con datos mock."""
    r = client.get("/api/cartera")
    assert r.status_code == 200
    data = r.json()
    assert "cartera_id" in data
    assert "capital_total_eur" in data
    assert "bloques" in data
    assert "posiciones" in data
    # Estructura de bloques
    for b in data["bloques"]:
        assert "categoria_base" in b
        assert b["categoria_base"] in (
            "defensivo", "income", "growth", "aggressive", "colchon", "sin_clasificar"
        )
        assert "peso_objetivo" in b and "peso_actual" in b
    # Estructura de posiciones
    for p in data["posiciones"]:
        assert "isin" in p
        assert "pm_real_eur" in p
        assert "pm_fiscal_es_eur" in p
        assert "pm_opciones_total_eur" in p
        assert "divisa_local" in p
