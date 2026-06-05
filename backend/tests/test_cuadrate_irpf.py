"""Tests del orquestador IRPF (Roadmap 1.9 — CSV approach + subprocess + ZIP).

Smoke real: usa un CSV de DEGIRO de prueba para verificar que
`generar_irpf_zip` materializa los inputs, invoca `generar_irpf.py` en
subprocess y devuelve un ZIP con XLSX + informes + sidecars.
"""
from __future__ import annotations

import zipfile
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
from app.services import cuadrate_irpf as svc
from app.services import storage_extractos as st


# CSV de muestra incluido en el repo (Cuádrate de Angel).
_CSV_DEGIRO_2025 = Path("/app/720/720/DeGiro_Transacciones_2025.csv")


@pytest.fixture()
def client_y_storage(tmp_path: Path, monkeypatch) -> Generator[tuple[TestClient, sessionmaker, Path], None, None]:
    """TestClient + BD :memory: + storage_dir aislado."""
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


def _seed_extracto_degiro_real(SL: sessionmaker, cartera_id: str, ejercicio: int) -> None:
    """Persiste el CSV real de DEGIRO 2025 del repo como extracto del usuario."""
    s: Session = SL()
    try:
        contenido = _CSV_DEGIRO_2025.read_bytes()
        st.guardar_extracto(
            s, cartera_id=cartera_id, ejercicio=ejercicio,
            kind="degiro_transacciones",
            filename_original=_CSV_DEGIRO_2025.name, contenido=contenido,
        )
        s.commit()
    finally:
        s.close()


# ── Servicio: generar_irpf_zip ─────────────────────────────────────────────


@pytest.mark.skipif(not _CSV_DEGIRO_2025.exists(),
                    reason="CSV de muestra no disponible en el entorno")
def test_generar_zip_con_csv_real_degiro(tmp_path, monkeypatch) -> None:
    """End-to-end: CSV real → subprocess → ZIP con XLSX + informes + sidecars."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(bind=engine)
    SL = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    cid = _seed_cartera(SL)
    _seed_extracto_degiro_real(SL, cid, 2025)

    s = SL()
    try:
        resultado = svc.generar_irpf_zip(s, cid, 2025)
    finally:
        s.close()

    try:
        assert resultado.zip_path.exists()
        assert resultado.zip_path.stat().st_size > 5_000
        assert resultado.kinds_usados == ["degiro_transacciones"]

        # Contenido del ZIP — al menos el XLSX maestro
        with zipfile.ZipFile(resultado.zip_path) as z:
            nombres = z.namelist()
        assert any(n.endswith(".xlsx") for n in nombres), nombres
        assert f"cartera_valores_irpf_2025.xlsx" in nombres
        # Debe incluir también algún informe .txt (corporativas/opciones suelen
        # aparecer con datos de Angel)
        assert any(n.startswith("informe_") and n.endswith(".txt") for n in nombres), nombres
        # Y el PDF fiscal (informe_fiscal_{ej}.pdf) generado por pdf_generator.
        assert "informe_fiscal_2025.pdf" in nombres, nombres
    finally:
        svc.limpiar(resultado)
        engine.dispose()


def test_sin_extractos_levanta_sin_extractos_error(
    client_y_storage,
) -> None:
    """Sin CSVs guardados → SinExtractosError (no crea ZIP)."""
    _, SL, _ = client_y_storage
    cid = _seed_cartera(SL)
    s = SL()
    try:
        with pytest.raises(svc.SinExtractosError):
            svc.generar_irpf_zip(s, cid, 2025)
    finally:
        s.close()


# ── Endpoint /api/cuadrate/irpf/{ejercicio}.zip ────────────────────────────


@pytest.mark.skipif(not _CSV_DEGIRO_2025.exists(),
                    reason="CSV de muestra no disponible en el entorno")
def test_endpoint_devuelve_zip_con_csv_real(client_y_storage) -> None:
    client, SL, _ = client_y_storage
    cid = _seed_cartera(SL)
    _seed_extracto_degiro_real(SL, cid, 2025)

    r = client.get("/api/cuadrate/irpf/2025.zip")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    # Cabecera ZIP local file header
    assert r.content[:2] == b"PK"
    assert len(r.content) > 5_000
    # Headers de diagnóstico
    assert "degiro_transacciones" in r.headers.get("X-Cima-Irpf-Kinds", "")
    ficheros = r.headers.get("X-Cima-Irpf-Ficheros", "")
    assert "cartera_valores_irpf_2025.xlsx" in ficheros


def test_endpoint_ejercicio_fuera_de_rango(client_y_storage) -> None:
    client, SL, _ = client_y_storage
    _seed_cartera(SL)
    r = client.get("/api/cuadrate/irpf/1999.zip")
    assert r.status_code == 400
    r = client.get("/api/cuadrate/irpf/2999.zip")
    assert r.status_code == 400


def test_endpoint_sin_cartera_404(client_y_storage) -> None:
    client, _, _ = client_y_storage
    r = client.get("/api/cuadrate/irpf/2025.zip")
    assert r.status_code == 404


def test_endpoint_sin_extractos_422(client_y_storage) -> None:
    """Cartera existe pero no hay CSVs guardados para 2025 → 422 (Unprocessable)."""
    client, SL, _ = client_y_storage
    _seed_cartera(SL)
    r = client.get("/api/cuadrate/irpf/2025.zip")
    assert r.status_code == 422
    assert "extractos" in r.json()["detail"].lower()
