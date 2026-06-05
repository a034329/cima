"""Tests de los endpoints /api/import/extractos (GET/DELETE) — Roadmap 1.9.

Los endpoints listan y borran los CSVs guardados por el storage. El test del
flujo POST end-to-end (subir extracto con `ejercicio` y que se persista en
disco) se cubre con smoke real usando CSVs de broker cuando llegue #9.
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import get_db, models
from app.db.base import Base
from app.main import app
from app.services import storage_extractos as st


@pytest.fixture()
def client_y_storage(tmp_path: Path, monkeypatch) -> Generator[tuple[TestClient, sessionmaker, Path], None, None]:
    """TestClient + BD :memory: compartida + storage_dir aislado."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, SessionLocal, tmp_path
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_cartera(SL: sessionmaker) -> str:
    s: Session = SL()
    try:
        u = models.User(email="t@cima.local", modo="owner"); s.add(u); s.flush()
        c = models.Cartera(user_id=u.id, nombre="T"); s.add(c); s.flush()
        s.commit()
        return c.id
    finally:
        s.close()


def _seed_extracto(SL: sessionmaker, cartera_id: str, ejercicio: int, kind: str,
                   contenido: bytes = b"col1,col2\n1,2\n") -> str:
    s: Session = SL()
    try:
        info = st.guardar_extracto(
            s, cartera_id=cartera_id, ejercicio=ejercicio, kind=kind,
            filename_original=f"{kind}_{ejercicio}.csv", contenido=contenido,
        )
        s.commit()
        return info.id
    finally:
        s.close()


def test_listar_extractos_sin_cartera_404(client_y_storage) -> None:
    client, _, _ = client_y_storage
    r = client.get("/api/import/extractos")
    assert r.status_code == 404


def test_listar_extractos_vacio(client_y_storage) -> None:
    client, SL, _ = client_y_storage
    _seed_cartera(SL)
    r = client.get("/api/import/extractos")
    assert r.status_code == 200
    assert r.json() == []


def test_listar_extractos_con_filtro_ejercicio(client_y_storage) -> None:
    client, SL, _ = client_y_storage
    cid = _seed_cartera(SL)
    _seed_extracto(SL, cid, 2024, "ibkr")
    _seed_extracto(SL, cid, 2025, "ibkr")
    _seed_extracto(SL, cid, 2025, "tr")

    todos = client.get("/api/import/extractos").json()
    assert len(todos) == 3
    assert {(e["ejercicio"], e["kind"]) for e in todos} == {
        (2024, "ibkr"), (2025, "ibkr"), (2025, "tr"),
    }

    solo_2025 = client.get("/api/import/extractos?ejercicio=2025").json()
    assert len(solo_2025) == 2
    assert all(e["ejercicio"] == 2025 for e in solo_2025)


def test_eliminar_extracto_204_y_borra_disco(client_y_storage) -> None:
    client, SL, tmp = client_y_storage
    cid = _seed_cartera(SL)
    extracto_id = _seed_extracto(SL, cid, 2025, "tr")
    # El fichero existe en disco antes
    s = SL()
    try:
        fila = s.get(models.ExtractoArchivo, extracto_id)
        fpath = st.ruta_absoluta(fila.ruta_storage)
        assert fpath.exists()
    finally:
        s.close()

    r = client.delete(f"/api/import/extractos/{extracto_id}")
    assert r.status_code == 204

    # Fichero borrado, fila también
    assert not fpath.exists()
    s = SL()
    try:
        assert s.get(models.ExtractoArchivo, extracto_id) is None
    finally:
        s.close()


def test_eliminar_extracto_inexistente_404(client_y_storage) -> None:
    client, SL, _ = client_y_storage
    _seed_cartera(SL)
    r = client.delete("/api/import/extractos/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
