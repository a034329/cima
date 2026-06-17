"""Fase A de auth (ADR-003): passwords (scrypt), tokens (JWT HS256 stdlib),
y endpoints signup/login/me con puente owner. No toca los 38 endpoints (eso
es Fase B), así que el resto de la suite no se ve afectado."""
from __future__ import annotations

import time
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Mode, settings
from app.db import get_db, models
from app.db.base import Base
from app.main import app


# ── passwords (scrypt stdlib) ────────────────────────────────────────────────

def test_password_roundtrip():
    from app.auth.passwords import hash_password, verify_password
    h = hash_password("correct horse battery staple")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("otra", h)


def test_password_hash_no_determinista():
    from app.auth.passwords import hash_password
    assert hash_password("misma") != hash_password("misma")  # sal aleatoria


def test_verify_con_hash_invalido_o_none():
    from app.auth.passwords import verify_password
    assert not verify_password("x", None)        # owner sin contraseña
    assert not verify_password("x", "")
    assert not verify_password("x", "formato-roto")
    assert not verify_password("", "scrypt$1$1$1$AAAA$BBBB")


# ── tokens (JWT HS256 stdlib) ────────────────────────────────────────────────

def test_token_roundtrip():
    from app.auth.tokens import create_access_token, decode_token
    tok = create_access_token(sub="u1", email="a@b.com")
    claims = decode_token(tok)
    assert claims["sub"] == "u1" and claims["email"] == "a@b.com"


def test_token_firma_manipulada():
    from app.auth.tokens import create_access_token, decode_token, TokenError
    tok = create_access_token(sub="u1", email="a@b.com")
    h, p, _ = tok.split(".")
    falso = f"{h}.{p}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(TokenError):
        decode_token(falso)


def test_token_alg_none_rechazado():
    import base64, json
    from app.auth.tokens import decode_token, TokenError
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    falso = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'u1', 'exp': 9999999999})}."
    with pytest.raises(TokenError):
        decode_token(falso)


def test_token_expirado():
    from app.auth.tokens import create_access_token, decode_token, TokenError
    tok = create_access_token(sub="u1", email="a@b.com", ttl_min=0)
    time.sleep(0.01)
    with pytest.raises(TokenError):
        decode_token(tok)


# ── endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture()
def client_y_db() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
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
        yield c, SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_signup_crea_usuario_y_token(client_y_db):
    client, SessionLocal = client_y_db
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"]
    # provisionó cartera + brokers
    with SessionLocal() as s:
        u = s.get(models.User, body["user_id"])
        assert u and u.password_hash and u.password_hash.startswith("scrypt$")
        assert len(u.carteras) == 1


def test_signup_duplicado_409(client_y_db):
    client, _ = client_y_db
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"})
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"})
    assert r.status_code == 409


def test_login_ok_y_credenciales_malas(client_y_db):
    client, _ = client_y_db
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"})
    assert client.post("/api/auth/login", json={"email": "a@b.com", "password": "12345678"}).status_code == 200
    # contraseña incorrecta y usuario inexistente → mismo 401
    assert client.post("/api/auth/login", json={"email": "a@b.com", "password": "xxxxxxxx"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "no@b.com", "password": "12345678"}).status_code == 401


def test_me_requiere_token_en_saas(client_y_db):
    client, _ = client_y_db
    assert client.get("/api/auth/me").status_code == 401   # saas, sin token
    tok = client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"}).json()["access_token"]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["email"] == "a@b.com"


def test_me_owner_mode_sin_token(client_y_db, monkeypatch):
    """En modo owner /me devuelve el usuario owner sin token (puente)."""
    client, _ = client_y_db
    monkeypatch.setattr(settings, "mode", Mode.OWNER)
    monkeypatch.setattr(settings, "owner_email", "duenyo@cima.local")
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == "duenyo@cima.local" and r.json()["modo"] == "owner"


# ── Fase D: endurecimiento ───────────────────────────────────────────────────

def test_login_lockout_tras_n_fallos(client_y_db):
    """Tras `login_max_fails` fallos, el (IP, email) queda bloqueado (429)."""
    from app.routers.auth import _login_limiter
    from app.config import settings as st
    _login_limiter.clear()
    client, _ = client_y_db
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "12345678"})

    # Agota los intentos con contraseña incorrecta → 401 cada uno.
    for _ in range(st.login_max_fails):
        r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "malamala"})
        assert r.status_code == 401
    # El siguiente, aun con contraseña CORRECTA, está bloqueado → 429 + Retry-After.
    r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "12345678"})
    assert r.status_code == 429
    assert int(r.headers.get("Retry-After", "0")) > 0
    _login_limiter.clear()


def test_login_ok_resetea_contador(client_y_db):
    from app.routers.auth import _login_limiter
    _login_limiter.clear()
    client, _ = client_y_db
    client.post("/api/auth/signup", json={"email": "c@b.com", "password": "12345678"})
    client.post("/api/auth/login", json={"email": "c@b.com", "password": "malamala"})  # 1 fallo
    assert client.post("/api/auth/login", json={"email": "c@b.com", "password": "12345678"}).status_code == 200
    # Tras el éxito el contador se limpia: nuevos fallos parten de cero (no bloquea ya).
    r = client.post("/api/auth/login", json={"email": "c@b.com", "password": "malamala"})
    assert r.status_code == 401
    _login_limiter.clear()


def test_signup_reclama_cuenta_sin_password(client_y_db, monkeypatch):
    """Una cuenta provisionada por /bootstrap (password_hash=None) se reclama en
    el signup fijando la contraseña, en vez de devolver 409 (cierra squatting)."""
    monkeypatch.setattr(settings, "mode", Mode.OWNER)  # bootstrap funciona
    client, SessionLocal = client_y_db
    # bootstrap crea el user sin contraseña
    client.post("/api/bootstrap", json={"email": "squat@b.com"})
    with SessionLocal() as s:
        u = s.execute(
            __import__("sqlalchemy").select(models.User).where(models.User.email == "squat@b.com")
        ).scalar_one()
        assert u.password_hash is None
    # signup con ese email NO da 409: lo reclama y emite token
    monkeypatch.setattr(settings, "mode", Mode.SAAS)
    r = client.post("/api/auth/signup", json={"email": "squat@b.com", "password": "12345678"})
    assert r.status_code == 201, r.text
    # y ahora puede hacer login
    assert client.post("/api/auth/login", json={"email": "squat@b.com", "password": "12345678"}).status_code == 200


def test_refresh_emite_token_nuevo(client_y_db):
    client, _ = client_y_db
    tok = client.post("/api/auth/signup", json={"email": "r@b.com", "password": "12345678"}).json()["access_token"]
    r = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["access_token"]
    # sin token (saas) → 401
    assert client.post("/api/auth/refresh").status_code == 401
